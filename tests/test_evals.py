# ruff: noqa: F811, F401  (фикстуры других модулей подключаются импортом)
"""Evals: набор тикетов с ожиданиями, метрики и отчёт."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ario.agents.backend import OpenHandsBackend
from ario.config import Settings
from ario.evals import CaseResult, EvalSet, collect, evaluate, render_report, run_evals
from tests.conftest import TICKET_TEXT
from tests.fakes import RoleBackend
from tests.llm_stub import finish
from tests.test_developer_stage import ANALYSIS, FakeDotnet
from tests.test_pipeline import APPROVE, dev, project, repo

UNSUITABLE = json.dumps({"decision": "unsuitable", "reason": "Решено оставить как есть"})
SUITABLE = json.dumps(ANALYSIS, ensure_ascii=False)


def ticket(case_id: str) -> str:
    return TICKET_TEXT.replace("id: 43031", f"id: {case_id}")


@pytest.fixture
def eval_settings(tmp_path: Path, repo: Path, project: Path) -> Settings:
    from tests.conftest import REPO_ROOT

    return Settings(
        data_dir=tmp_path / "data",
        rx_path=repo,
        llm_model="stub",
        openrouter_api_key=None,
        guardrails_path=REPO_ROOT / "config" / "guardrails.yaml",
        project_dir=project,
    )


def make_evals(root: Path, expected: str, *case_ids: str) -> Path:
    (root / "cases").mkdir(parents=True)
    for case_id in case_ids:
        (root / "cases" / f"{case_id}.md").write_text(ticket(case_id), encoding="utf-8")
    (root / "expected.yaml").write_text(expected, encoding="utf-8")
    return root


EXPECTED = """
cases:
  "101":
    outcome: needs-human
    analysis: unsuitable
    edited: false
  "102":
    outcome: done
    analysis: suitable
    review: approve
    build_ok: true
    edited: true
"""


def test_passing_set_with_reports(eval_settings: Settings, tmp_path: Path) -> None:
    evals = make_evals(tmp_path / "evals", EXPECTED, "101", "102")
    backend = RoleBackend(
        analyst=[UNSUITABLE, SUITABLE],
        developer=[dev("int X = 1;", "int X = 2;", "Заменил на 2")],
        reviewer=[APPROVE],
    )
    seen: list[str] = []
    run_dir, results = run_evals(
        eval_settings,
        evals,
        lambda s: backend,
        runner=FakeDotnet(),
        on_result=lambda r: seen.append(r.case_id),
    )
    assert seen == ["101", "102"]  # порядок из expected.yaml
    assert [r.passed for r in results] == [True, True]
    second = results[1]
    assert second.analysis == "suitable"
    assert second.reviews == ["approve"]
    assert second.build_ok is True
    assert second.files_edited == ["src/Mod/F.cs"]
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Кейсов: 2, пройдено: 2, провалено: 0" in report
    assert "PASS 3/3" in report
    saved = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert [r["case_id"] for r in saved] == ["101", "102"]
    assert (run_dir / "102" / "runs" / "102" / "review-1.md").exists()  # изоляция по кейсам


def test_failed_expectation_is_reported(eval_settings: Settings, tmp_path: Path) -> None:
    evals = make_evals(
        tmp_path / "evals",
        'cases:\n  "101":\n    outcome: done\n    analysis: suitable\n',
        "101",
    )
    backend = RoleBackend(analyst=[UNSUITABLE])
    run_dir, (result,) = run_evals(eval_settings, evals, lambda s: backend, runner=FakeDotnet())
    assert not result.passed
    assert [(c.name, c.ok) for c in result.checks] == [("итог", False), ("решение analyst", False)]
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "FAIL 0/2" in report
    assert "ожидалось done, получено needs-human" in report
    assert "Решено оставить как есть" in report  # причина видна в отчёте


def test_second_code_case_is_not_taken_on_dirty_tree(
    eval_settings: Settings, tmp_path: Path
) -> None:
    """После правок первого кейса RX не чистый: второй честно помечается not-taken."""
    expected = 'cases:\n  "101":\n    outcome: done\n  "102":\n    outcome: done\n'
    evals = make_evals(tmp_path / "evals", expected, "101", "102")
    backend = RoleBackend(
        analyst=[SUITABLE],
        developer=[dev("int X = 1;", "int X = 2;", "ок")],
        reviewer=[APPROVE],
    )
    _, results = run_evals(eval_settings, evals, lambda s: backend, runner=FakeDotnet())
    assert [r.outcome for r in results] == ["done", "not-taken"]
    assert "незакоммиченные" in (results[1].reason or "")
    assert not results[1].passed


def test_metrics_come_from_the_trace_of_a_real_agent_run(
    eval_settings: Settings, tmp_path: Path, script: Any
) -> None:
    script(finish(UNSUITABLE, prompt=3000))
    evals = make_evals(
        tmp_path / "evals",
        'cases:\n  "101":\n    outcome: needs-human\n    max_steps: 3\n'
        "    max_prompt_tokens: 2000\n",
        "101",
    )
    _, (result,) = run_evals(
        eval_settings,
        evals,
        lambda s: OpenHandsBackend("openai/stub-model", "sk-test", retry_wait=(0, 0)),
        runner=FakeDotnet(),
    )
    assert (result.steps, result.prompt_tokens, result.completion_tokens) == (1, 3000, 50)
    assert result.max_context == 3050
    assert result.seconds > 0
    by_name = {c.name: c for c in result.checks}
    assert by_name["итог"].ok
    assert by_name["потолок steps"].ok  # 1 <= 3
    assert not by_name["потолок prompt tokens"].ok  # 3000 > 2000: метрика вышла за бюджет
    assert not result.passed


def test_evaluate_checks_each_expectation() -> None:
    result = CaseResult(
        "1",
        "done",
        analysis="suitable",
        reviews=["reject", "approve"],
        build_ok=True,
        files_edited=["a.cs"],
        seconds=100,
        steps=10,
        prompt_tokens=5000,
    )
    checks = evaluate(
        result,
        {
            "outcome": "done",
            "analysis": "suitable",
            "review": "approve",  # берётся последний вердикт
            "build_ok": True,
            "edited": True,
            "max_seconds": 100,
            "max_steps": 9,
            "max_prompt_tokens": 5000,
        },
    )
    assert {c.name: c.ok for c in checks} == {
        "итог": True,
        "решение analyst": True,
        "вердикт reviewer": True,
        "сборка успешна": True,
        "были правки": True,
        "потолок seconds": True,
        "потолок steps": False,
        "потолок prompt tokens": True,
    }


def test_collect_survives_missing_artifacts(tmp_path: Path) -> None:
    result = collect("7", "error", "сбой", tmp_path)
    assert (result.steps, result.analysis, result.reviews, result.files_edited) == (0, None, [], [])
    assert result.build_ok is None


def test_render_report_totals() -> None:
    results = [
        CaseResult("1", "done", seconds=60, steps=5, prompt_tokens=2000, completion_tokens=100),
        CaseResult("2", "needs-human", seconds=30, steps=2, prompt_tokens=500, llm_retries=2),
    ]
    text = render_report(results)
    assert "Шаги агентов: 7" in text
    assert "запросы 2.5k" in text
    assert "Повторы запросов к модели: 2" in text
    assert "1 мин 30 с" in text


# --- загрузка набора ---
def test_bad_sets_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        EvalSet.load(tmp_path / "нет")
    empty = make_evals(tmp_path / "e1", "cases: {}\n", "1")
    with pytest.raises(ValueError, match="непустой"):
        EvalSet.load(empty)
    missing = make_evals(tmp_path / "e2", 'cases:\n  "5":\n    outcome: done\n', "1")
    with pytest.raises(ValueError, match="Нет файла тикета"):
        EvalSet.load(missing)


def test_unknown_case_in_only_is_rejected(eval_settings: Settings, tmp_path: Path) -> None:
    evals = make_evals(tmp_path / "evals", EXPECTED, "101", "102")
    with pytest.raises(ValueError, match="999"):
        run_evals(eval_settings, evals, lambda s: RoleBackend(), only=["999"])


def test_only_runs_selected_cases(eval_settings: Settings, tmp_path: Path) -> None:
    evals = make_evals(tmp_path / "evals", EXPECTED, "101", "102")
    backend = RoleBackend(analyst=[UNSUITABLE])
    _, results = run_evals(
        eval_settings, evals, lambda s: backend, only=["101"], runner=FakeDotnet()
    )
    assert [r.case_id for r in results] == ["101"]


# --- команда ario eval ---
def test_cli_eval_exit_codes(
    eval_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ario.cli as cli

    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARIO_DATA_DIR", str(eval_settings.data_dir))
    monkeypatch.setenv("RX_PATH", str(eval_settings.rx_path))
    monkeypatch.setenv("ARIO_PROJECT_DIR", str(eval_settings.project_dir))
    monkeypatch.setenv("ARIO_GUARDRAILS", str(eval_settings.guardrails_path))
    base = ["--env-file", str(empty_env), "eval"]
    evals = make_evals(
        tmp_path / "evals",
        'cases:\n  "101":\n    outcome: needs-human\n    analysis: unsuitable\n',
        "101",
    )
    monkeypatch.setattr(cli, "OpenHandsBackend", lambda *a, **k: RoleBackend(analyst=[UNSUITABLE]))
    assert cli.main([*base, "--dir", str(evals)]) == 0
    out = capsys.readouterr().out
    assert "[PASS] 101: needs-human" in out
    assert "report.md" in out
    # провал ожидания: код выхода 1
    failing = make_evals(tmp_path / "evals2", 'cases:\n  "101":\n    outcome: done\n', "101")
    monkeypatch.setattr(cli, "OpenHandsBackend", lambda *a, **k: RoleBackend(analyst=[UNSUITABLE]))
    assert cli.main([*base, "--dir", str(failing)]) == 1
    assert "[FAIL] 101" in capsys.readouterr().out
    # неверный набор: код выхода 2
    assert cli.main([*base, "--dir", str(tmp_path / "нет")]) == 2
