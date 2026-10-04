"""Трассировка: формат, реестр событий, маскирование секретов, разбор `ario trace`."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from ario.config import Settings
from ario.guardrails import Guardrails
from ario.orchestrator import Orchestrator, Stage, StageContext
from ario.trace import EVENT_KINDS, MASK, Masker, TraceWriter
from ario.trace_report import load_events, render_steps, render_summary, summarize
from tests.conftest import REPO_ROOT
from tests.test_guardrails import CONFIG

KEY = "sk-fake-test-key-not-real-0123456789"


# --- маскирование ---
def test_known_secret_values_are_masked() -> None:
    masker = Masker([KEY, "ab"])  # короткие значения не маскируются
    assert masker.mask_text(f"ключ {KEY} и ab") == f"ключ {MASK} и ab"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abcdefghijklmnop1234", f"Authorization: Bearer {MASK}"),
        ('{"api_key": "supersecretvalue1"}', f'{{"api_key": "{MASK}"}}'),
        ("password=hunter2hunter2 next", f"password={MASK} next"),
        ("TOKEN: abcdef123456", f"TOKEN: {MASK}"),
        ("https://user:pa55word@host/path", f"https://user:{MASK}@host/path"),
        ("ключ sk-proj-AbCdEfGhIjKlMnOpQrSt", f"ключ {MASK}"),
        ("обычный текст про token без значения", "обычный текст про token без значения"),
        # обычный код не портится
        ("var token = CancellationToken.None;", "var token = CancellationToken.None;"),
        ("Password: Resources.PasswordLabel,", "Password: Resources.PasswordLabel,"),
        ("password=abc", "password=abc"),
    ],
)
def test_typical_secret_forms_are_masked(text: str, expected: str) -> None:
    assert Masker().mask_text(text) == expected


def test_mask_is_recursive_and_keeps_structure() -> None:
    value = {"a": [f"x {KEY}", {"b": f"y {KEY}"}], "n": 5, "t": (f"{KEY}",)}
    assert Masker([KEY]).mask(value) == {
        "a": [f"x {MASK}", {"b": f"y {MASK}"}],
        "n": 5,
        "t": [MASK],
    }


def test_from_environment_takes_secret_named_variables_and_extras() -> None:
    environ = {"PATH": "/bin", "MY_API_KEY": "valuevalue1", "GITHUB_TOKEN": "tok123456"}
    masker = Masker.from_environment(environ, CONFIG.env, extra=["extra-secret-1", None])
    text = masker.mask_text("valuevalue1 tok123456 extra-secret-1 /bin")
    assert text == f"{MASK} {MASK} {MASK} /bin"


def test_writer_masks_all_event_fields(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path, "1", "t", Masker([KEY]))
    writer.event("stub", stage=f"s-{KEY}", note=f"запрос с {KEY}", nested={"k": [KEY]})
    raw = writer.path.read_text(encoding="utf-8")
    assert KEY not in raw
    record = json.loads(raw)
    assert record["data"]["note"] == f"запрос с {MASK}"
    assert record["data"]["nested"] == {"k": [MASK]}


def test_secret_from_settings_never_reaches_trace_of_a_run(
    settings: Settings, inbox_ticket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    monkeypatch.setenv("SOME_SECRET_VALUE", "environment-secret-123")
    with_key = replace(settings, openrouter_api_key=KEY)

    def leaky(ctx: StageContext) -> None:
        ctx.trace.event("stub", stage="leaky", text=f"ключ {KEY} и environment-secret-123")
        raise RuntimeError(f"упало с ключом {KEY}")

    guardrails = Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml")
    orchestrator = Orchestrator(with_key, [Stage("leaky", leaky)], guardrails)
    assert orchestrator.run(inbox_ticket) == "needs-human"
    raw = (settings.traces_dir / "43031" / "events.jsonl").read_text(encoding="utf-8")
    assert KEY not in raw
    assert "environment-secret-123" not in raw
    assert MASK in raw


# --- реестр видов событий ---
def test_every_emitted_event_kind_is_registered_and_vice_versa() -> None:
    emitted: set[str] = set()
    for path in (REPO_ROOT / "src" / "ario").rglob("*.py"):
        emitted |= set(re.findall(r"\.event\(\s*\"([a-z_]+)\"", path.read_text(encoding="utf-8")))
    assert emitted - set(EVENT_KINDS) == set(), "событие не описано в EVENT_KINDS"
    assert set(EVENT_KINDS) - emitted == set(), "в EVENT_KINDS осталось неиспользуемое событие"


def test_stage_and_run_events_carry_durations(settings: Settings, inbox_ticket: str) -> None:
    guardrails = Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml")
    Orchestrator(settings, [Stage("a", lambda ctx: None)], guardrails).run(inbox_ticket)
    events, _ = load_events(settings.traces_dir / "43031" / "events.jsonl")
    by_kind = {e["kind"]: e["data"] for e in events}
    assert by_kind["stage_finished"]["seconds"] >= 0
    assert by_kind["run_finished"]["total_seconds"] >= 0
    assert by_kind["run_finished"]["outcome"] == "done"


# --- разбор трассы ---
def ev(
    kind: str, stage: str | None = None, ts: str = "2026-10-04T10:00:00.000+00:00", **data: object
) -> dict[str, Any]:
    return {
        "ts": ts,
        "trace_id": "t",
        "span_id": "s",
        "ticket": "7",
        "stage": stage,
        "agent": None,
        "kind": kind,
        "data": data,
    }


SYNTHETIC = [
    ev("run_started", resumed=False),
    ev("stage_started", "analysis"),
    ev(
        "agent_started",
        "analysis",
        agent="analyst",
        model="m",
        tools=["read_file"],
        skills=[],
        task="Т",
    ),
    ev(
        "agent_action",
        "analysis",
        tool="read_file",
        arguments='{"path": "a.cs"}',
        context_tokens=9000,
        thought="думаю",
    ),
    ev("agent_observation", "analysis", tool="read_file", text="1 class A"),
    ev("agent_error", "analysis", tool="search_text", error="Extra inputs are not permitted"),
    ev("llm_retry", "analysis", attempt=1, max_attempts=3, error="RateLimitError: 429"),
    ev("agent_action", "analysis", tool="finish", arguments="{}", context_tokens=12000),
    ev("agent_finished", "analysis", steps=2, prompt_tokens=21000, completion_tokens=500),
    ev("analysis_verdict", "analysis", decision="suitable", risk="low"),
    ev("stage_finished", "analysis", seconds=75.0),
    ev("stage_started", "development"),
    ev("guardrail_blocked", "development", reason="Превышен лимит шагов агента developer (60)"),
    ev(
        "stage_failed",
        "development",
        reason="Превышен лимит шагов агента developer (60)",
        seconds=300.0,
    ),
    ev(
        "run_finished",
        outcome="needs-human",
        reason="Превышен лимит шагов агента developer (60)",
        total_seconds=380.0,
    ),
]


def test_summary_of_a_failed_run() -> None:
    summary = summarize(SYNTHETIC)
    text = render_summary(summary)
    assert "нужен человек (needs-human)" in text
    assert "6 мин 20 с" in text
    assert "лимит шагов" in text
    analysis = next(s for s in summary.stages if s.name == "analysis")
    assert (analysis.steps, analysis.prompt_tokens, analysis.max_context) == (2, 21000, 12000)
    assert analysis.tools["read_file"] == 1
    assert "ошибка инструмента search_text" in text
    assert "повтор запроса к модели 1/3" in text
    assert "вердикт analyst: suitable" in text
    assert "1 мин 15 с" in text
    assert "development: отказ" in text


def test_interrupted_and_resumed_runs() -> None:
    events = [
        ev("run_started", resumed=False),
        ev("stage_started", "a"),
        ev("stage_finished", "a", seconds=1.0),
        ev("stage_started", "b"),  # процесс убит здесь
        ev("run_started", resumed=True),
        ev("stage_skipped", "a"),
        ev("stage_started", "b"),
    ]
    summary = summarize(events)
    text = render_summary(summary)
    assert (summary.runs, summary.resumed) == (2, 1)
    assert "возобновлений: 1" in text
    assert "прогон не завершён" in text
    assert next(s for s in summary.stages if s.name == "b").status == "interrupted"
    assert next(s for s in summary.stages if s.name == "a").status == "skipped"


def test_truncated_last_line_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    good = json.dumps(ev("run_started", resumed=False), ensure_ascii=False)
    path.write_text(good + "\n" + '{"ts": "2026-10-04T10:00', encoding="utf-8")  # убитый процесс
    events, skipped = load_events(path)
    assert len(events) == 1
    assert skipped == 1
    assert "повреждённых строк трассы: 1" in render_summary(summarize(events, skipped))


def test_steps_timeline_pairs_actions_with_results_and_filters_by_stage() -> None:
    text = render_steps(SYNTHETIC, "analysis")
    assert "#1 read_file" in text
    assert "-> 1 class A" in text
    assert "контекст 9.0k" in text
    assert "ожидание модели, повтор 1/3" in text
    assert "ошибка инструмента" in text
    assert "development" not in text  # другой этап скрыт
    full = render_steps(SYNTHETIC, "analysis", full=True)
    assert "мысль: думаю" in full
    assert "задача: Т" in full
    assert render_steps(SYNTHETIC, "нет-такого") == "Нет событий для показа.\n"


# --- команда ario trace ---
@pytest.fixture
def trace_cli(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # type: ignore[no-untyped-def]
    import ario.cli as cli

    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARIO_DATA_DIR", str(settings.data_dir))
    directory = settings.traces_dir / "7"
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in SYNTHETIC) + "\n", encoding="utf-8"
    )
    return lambda *args: cli.main(["--env-file", str(empty_env), "trace", *args])


def test_cli_trace_summary_and_steps(trace_cli, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    assert trace_cli("7") == 0
    assert "Тикет 7" in capsys.readouterr().out
    assert trace_cli("7", "--steps", "--stage", "analysis") == 0
    assert "#1 read_file" in capsys.readouterr().out


def test_cli_trace_errors(trace_cli, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    assert trace_cli("999") == 2
    assert "нет трассы" in capsys.readouterr().err
    assert trace_cli("../x") == 2


def test_resumed_writer_does_not_glue_event_to_a_truncated_line(tmp_path: Path) -> None:
    """Процесс убит посреди записи: следующее событие должно остаться читаемым."""
    first = TraceWriter(tmp_path, "7", "t")
    first.event("run_started", resumed=False)
    with first.path.open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-10-04T10:00')  # обрыв без перевода строки
    second = TraceWriter(tmp_path, "7", "t")  # возобновлённый прогон
    second.event("run_started", resumed=True)
    events, skipped = load_events(second.path)
    assert [e["data"]["resumed"] for e in events] == [False, True]  # событие возобновления цело
    assert skipped == 1  # битая строка одна и учтена
