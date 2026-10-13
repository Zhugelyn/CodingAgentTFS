"""Evals: прогон набора тикетов с ожидаемыми исходами и метриками (`ario eval`).

Набор лежит в `evals/`: `cases/<id>.md` (тикеты в формате D01) и `expected.yaml` с
ожиданиями. Каждый тикет проходит весь поток в изолированной папке данных
`evals/runs/<время>/<id>/` (свои tickets/runs/traces), RX общий. Результат: таблица проверок
(ожидание против факта), метрики (время, шаги, токены, повторы запросов) и отчёты
`report.md` и `results.json` в папке прогона.

Ожидания в `expected.yaml` (все ключи необязательны):
  outcome: done | needs-human          итог тикета
  analysis: suitable | unsuitable | needs_info   решение analyst
  review: approve | reject             итоговый вердикт reviewer
  build_ok: true | false               последняя сборка успешна
  edited: true | false                 были ли правки файлов
  max_seconds / max_steps / max_prompt_tokens    потолки по метрикам
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ario.agents.backend import AgentBackend
from ario.config import Settings
from ario.fsutil import RunInProgressError, atomic_write_text
from ario.guardrails import Guardrails
from ario.orchestrator import (
    Orchestrator,
    TicketAlreadyFinishedError,
    TicketNotTakenError,
)
from ario.pipeline import build_pipeline
from ario.tools.git import Runner
from ario.tools.proc import run_process
from ario.trace_report import StageSummary, load_events, summarize

Expectation = dict[str, Any]
BackendFactory = Callable[[Settings], AgentBackend]


@dataclass
class Check:
    name: str
    expected: Any
    actual: Any
    ok: bool


@dataclass
class CaseResult:
    case_id: str
    outcome: str  # done | needs-human | not-taken | error
    reason: str | None = None
    seconds: float = 0.0
    steps: int = 0
    prompt_tokens: int = 0
    cost_usd: float = 0.0
    completion_tokens: int = 0
    max_context: int = 0
    llm_retries: int = 0
    tool_errors: int = 0
    analysis: str | None = None
    reviews: list[str] = field(default_factory=list)
    build_ok: bool | None = None
    files_edited: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks) and self.outcome in ("done", "needs-human")


@dataclass
class EvalSet:
    cases_dir: Path
    expectations: dict[str, Expectation]

    @classmethod
    def load(cls, root: Path) -> EvalSet:
        data = yaml.safe_load((root / "expected.yaml").read_text(encoding="utf-8")) or {}
        cases = data.get("cases")
        if not isinstance(cases, dict) or not cases:
            raise ValueError(f"{root / 'expected.yaml'}: ожидается непустой раздел `cases:`")
        expectations = {str(k): dict(v or {}) for k, v in cases.items()}
        for case_id in expectations:
            if not (root / "cases" / f"{case_id}.md").is_file():
                raise ValueError(f"Нет файла тикета для кейса {case_id}: cases/{case_id}.md")
        return cls(root / "cases", expectations)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def collect(case_id: str, outcome: str, reason: str | None, data_dir: Path) -> CaseResult:
    """Собирает факты прогона из артефактов и трассы (так же, как смотрит `ario trace`)."""
    result = CaseResult(case_id, outcome, reason)
    run_dir = data_dir / "runs" / case_id
    trace = data_dir / "traces" / case_id / "events.jsonl"
    if trace.is_file():
        events, _ = load_events(trace)
        summary = summarize(events)
        stages: list[StageSummary] = summary.stages
        result.seconds = summary.total_seconds or 0.0
        result.steps = sum(s.steps for s in stages)
        result.prompt_tokens = sum(s.prompt_tokens for s in stages)
        result.cost_usd = sum(s.cost_usd for s in stages)
        result.completion_tokens = sum(s.completion_tokens for s in stages)
        result.max_context = max((s.max_context for s in stages), default=0)
        result.llm_retries = sum(1 for e in events if e["kind"] == "llm_retry")
        result.tool_errors = sum(1 for e in events if e["kind"] == "agent_error")
        builds = [e["data"].get("ok") for e in events if e["kind"] == "build_output"]
        result.build_ok = bool(builds[-1]) if builds else None
    analysis = _read_json(run_dir / "analysis.json")
    result.analysis = str(analysis["decision"]) if analysis and "decision" in analysis else None
    for n in (1, 2):
        review = _read_json(run_dir / f"review-{n}.json")
        if review and "verdict" in review:
            result.reviews.append(str(review["verdict"]))
    edited: set[str] = set()
    for n in (1, 2):
        report = _read_json(run_dir / f"development-{n}.json")
        if report:
            edited |= set(report.get("files_edited") or [])
    result.files_edited = sorted(edited)
    return result


def evaluate(result: CaseResult, expected: Expectation) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, want: Any, got: Any, ok: bool | None = None) -> None:
        checks.append(Check(name, want, got, (want == got) if ok is None else ok))

    if "outcome" in expected:
        add("итог", expected["outcome"], result.outcome)
    if "analysis" in expected:
        add("решение analyst", expected["analysis"], result.analysis)
    if "review" in expected:
        add("вердикт reviewer", expected["review"], result.reviews[-1] if result.reviews else None)
    if "build_ok" in expected:
        add("сборка успешна", expected["build_ok"], result.build_ok)
    if "edited" in expected:
        add("были правки", expected["edited"], bool(result.files_edited))
    for key, got in (
        ("max_seconds", result.seconds),
        ("max_steps", result.steps),
        ("max_prompt_tokens", result.prompt_tokens),
    ):
        if key in expected:
            add(
                key.replace("max_", "потолок ").replace("_", " "),
                f"<= {expected[key]}",
                got,
                got <= expected[key],
            )
    return checks


def run_case(
    case_id: str,
    ticket_file: Path,
    settings: Settings,
    run_dir: Path,
    backend: AgentBackend,
    guardrails: Guardrails,
    runner: Runner = run_process,
) -> CaseResult:
    data_dir = run_dir / case_id
    (data_dir / "tickets" / "inbox").mkdir(parents=True, exist_ok=True)
    shutil.copy(ticket_file, data_dir / "tickets" / "inbox" / f"{case_id}.md")
    case_settings = replace(settings, data_dir=data_dir)
    orchestrator = Orchestrator(case_settings, build_pipeline(backend, runner), guardrails)
    started = time.monotonic()
    outcome: str
    reason: str | None = None
    try:
        outcome = orchestrator.run(case_id)
    except TicketNotTakenError as e:
        outcome, reason = "not-taken", e.reason
    except (RunInProgressError, TicketAlreadyFinishedError) as e:
        outcome, reason = "error", str(e)
    except Exception as e:  # сбой одного кейса не должен останавливать весь набор
        outcome, reason = "error", f"{type(e).__name__}: {e}"
    result = collect(case_id, outcome, reason, data_dir)
    if outcome == "needs-human":
        state = _read_json(data_dir / "runs" / case_id / "state.json")
        result.reason = str(state.get("reason")) if state else None
    if not result.seconds:
        result.seconds = round(time.monotonic() - started, 2)
    return result


def run_evals(
    settings: Settings,
    evals_dir: Path,
    backend_factory: BackendFactory,
    only: list[str] | None = None,
    guardrails: Guardrails | None = None,
    on_result: Callable[[CaseResult], None] | None = None,
    runner: Runner = run_process,
) -> tuple[Path, list[CaseResult]]:
    eval_set = EvalSet.load(evals_dir)
    unknown = set(only or []) - set(eval_set.expectations)
    if unknown:
        raise ValueError(f"Нет таких кейсов в expected.yaml: {', '.join(sorted(unknown))}")
    guardrails = guardrails or Guardrails.load(settings.guardrails_path)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = evals_dir / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    backend = backend_factory(settings)
    results: list[CaseResult] = []
    for case_id, expected in eval_set.expectations.items():
        if only and case_id not in only:
            continue
        result = run_case(
            case_id,
            eval_set.cases_dir / f"{case_id}.md",
            settings,
            run_dir,
            backend,
            guardrails,
            runner,
        )
        result.checks = evaluate(result, expected)
        results.append(result)
        if on_result:
            on_result(result)
    atomic_write_text(
        run_dir / "results.json",
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
    )
    atomic_write_text(run_dir / "report.md", render_report(results))
    return run_dir, results


# --- отчёт ----------------------------------------------------------------------------------
def _fmt_time(seconds: float) -> str:
    minutes, sec = divmod(int(round(seconds)), 60)
    return f"{minutes} мин {sec:02d} с" if minutes else f"{sec} с"


def _fmt_k(value: int) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def render_report(results: list[CaseResult]) -> str:
    lines = ["# Evals: результат прогона", ""]
    passed = sum(r.passed for r in results)
    lines.append(f"Кейсов: {len(results)}, пройдено: {passed}, провалено: {len(results) - passed}")
    lines += ["", "## Кейсы", ""]
    lines.append(
        "| Кейс | Итог | Проверки | Время | Шаги | Токены (запросы+ответы) | Макс. контекст "
        "| Цена | Повторы | Ошибки инстр. |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        ok = sum(c.ok for c in r.checks)
        mark = "PASS" if r.passed else "FAIL"
        lines.append(
            f"| {r.case_id} | {r.outcome} | {mark} {ok}/{len(r.checks)} | {_fmt_time(r.seconds)} | "
            f"{r.steps} | {_fmt_k(r.prompt_tokens)}+{_fmt_k(r.completion_tokens)} | "
            f"{_fmt_k(r.max_context)} | ${r.cost_usd:.4f} | {r.llm_retries} | {r.tool_errors} |"
        )
    for r in results:
        lines += ["", f"### {r.case_id}"]
        if r.reason:
            lines.append(f"Причина: {' '.join(str(r.reason).split())[:400]}")
        facts = [f"решение analyst: {r.analysis}", f"ревью: {', '.join(r.reviews) or 'нет'}"]
        facts.append(f"сборка: {r.build_ok}")
        facts.append(f"файлы: {', '.join(r.files_edited) or 'нет'}")
        lines.append("; ".join(facts))
        for c in r.checks:
            lines.append(
                f"- {'PASS' if c.ok else 'FAIL'} {c.name}: "
                f"ожидалось {c.expected}, получено {c.actual}"
            )
    if results:
        total_seconds = sum(r.seconds for r in results)
        lines += [
            "",
            "## Итого по метрикам",
            f"- Время: {_fmt_time(total_seconds)}, "
            f"в среднем на кейс {_fmt_time(total_seconds / len(results))}",
            f"- Шаги агентов: {sum(r.steps for r in results)}",
            f"- Токены: запросы {_fmt_k(sum(r.prompt_tokens for r in results))}, "
            f"ответы {_fmt_k(sum(r.completion_tokens for r in results))}",
            f"- Цена по данным провайдера: ${sum(r.cost_usd for r in results):.4f}",
            f"- Повторы запросов к модели: {sum(r.llm_retries for r in results)}, "
            f"ошибки инструментов: {sum(r.tool_errors for r in results)}",
        ]
    return "\n".join(lines) + "\n"
