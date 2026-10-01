from __future__ import annotations

import json
from pathlib import Path

import pytest

from ario.config import Settings
from ario.orchestrator import (
    Orchestrator,
    Stage,
    StageContext,
    StageFailed,
    TicketAlreadyFinishedError,
)
from ario.state import StateStore
from ario.store import TicketNotFoundError


def _events(settings: Settings, ticket_id: str) -> list[dict[str, object]]:
    path = settings.traces_dir / ticket_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_stub_flow_moves_ticket_to_done_and_writes_trace(
    settings: Settings, inbox_ticket: str
) -> None:
    assert Orchestrator(settings).run(inbox_ticket) == "done"

    assert (settings.tickets_dir / "done" / "43031.md").exists()
    assert not (settings.tickets_dir / "inbox" / "43031.md").exists()
    state = StateStore(settings.runs_dir).load(inbox_ticket)
    assert state is not None
    assert state.status == "done"
    assert state.stages["stub"].status == "done"
    events = _events(settings, inbox_ticket)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert len({e["trace_id"] for e in events}) == 1


def test_failed_stage_goes_to_needs_human_with_comment(
    settings: Settings, inbox_ticket: str
) -> None:
    def fail(ctx: StageContext) -> None:
        raise StageFailed("не хватает данных")

    assert Orchestrator(settings, [Stage("analysis", fail)]).run(inbox_ticket) == "needs-human"

    comments = (settings.tickets_dir / "needs-human" / "43031.comments.md").read_text(
        encoding="utf-8"
    )
    assert "[orchestrator]" in comments
    assert "не хватает данных" in comments
    state = StateStore(settings.runs_dir).load(inbox_ticket)
    assert state is not None
    assert state.status == "needs_human"
    assert state.reason == "не хватает данных"
    assert state.stages["analysis"].status == "failed"


def test_unexpected_exception_does_not_leave_ticket_in_progress(
    settings: Settings, inbox_ticket: str
) -> None:
    def boom(ctx: StageContext) -> None:
        raise RuntimeError("модель недоступна")

    assert Orchestrator(settings, [Stage("analysis", boom)]).run(inbox_ticket) == "needs-human"
    assert not (settings.tickets_dir / "in-progress" / "43031.md").exists()


def test_invalid_ticket_file_goes_to_needs_human(settings: Settings) -> None:
    inbox = settings.tickets_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "7.md").write_text("не тикет", encoding="utf-8")

    assert Orchestrator(settings).run("7") == "needs-human"
    assert "Некорректный файл тикета" in (
        settings.tickets_dir / "needs-human" / "7.comments.md"
    ).read_text(encoding="utf-8")


def test_resume_skips_finished_stages(settings: Settings, inbox_ticket: str) -> None:
    calls: list[str] = []

    def first(ctx: StageContext) -> None:
        calls.append("first")

    crash = {"on": True}

    def second(ctx: StageContext) -> None:
        if crash["on"]:
            raise KeyboardInterrupt  # имитация убитого процесса: BaseException не перехватывается
        calls.append("second")

    stages = [Stage("first", first), Stage("second", second)]
    with pytest.raises(KeyboardInterrupt):
        Orchestrator(settings, stages).run(inbox_ticket)
    assert (settings.tickets_dir / "in-progress" / "43031.md").exists()

    crash["on"] = False
    assert Orchestrator(settings, stages).run(inbox_ticket) == "done"
    assert calls == ["first", "second"]  # first не повторялся


def test_finished_and_missing_tickets_are_rejected(settings: Settings, inbox_ticket: str) -> None:
    Orchestrator(settings).run(inbox_ticket)
    with pytest.raises(TicketAlreadyFinishedError):
        Orchestrator(settings).run(inbox_ticket)
    with pytest.raises(TicketNotFoundError):
        Orchestrator(settings).run("999")


def test_cli_run(
    settings: Settings,
    inbox_ticket: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ario.cli import main

    monkeypatch.setenv("ARIO_DATA_DIR", str(settings.data_dir))
    assert main(["run", inbox_ticket]) == 0
    assert "done" in capsys.readouterr().out
    assert main(["run", "999"]) == 2
    assert Path(settings.tickets_dir / "done" / "43031.md").exists()


def test_invalid_id_is_rejected(settings: Settings) -> None:
    from ario.store import InvalidTicketIdError

    with pytest.raises(InvalidTicketIdError):
        Orchestrator(settings).run("../x")


def test_duplicate_in_done_is_rejected_without_overwrite(
    settings: Settings, inbox_ticket: str
) -> None:
    Orchestrator(settings).run(inbox_ticket)
    done = settings.tickets_dir / "done" / "43031.md"
    original = done.read_text(encoding="utf-8")
    (settings.tickets_dir / "inbox" / "43031.md").write_text("другой", encoding="utf-8")

    with pytest.raises(TicketAlreadyFinishedError):
        Orchestrator(settings).run(inbox_ticket)
    assert done.read_text(encoding="utf-8") == original


def test_resubmitted_ticket_gets_fresh_state(settings: Settings, inbox_ticket: str) -> None:
    def fail(ctx: StageContext) -> None:
        raise StageFailed("причина")

    Orchestrator(settings, [Stage("s", fail)]).run(inbox_ticket)
    # человек вернул тикет в inbox для повторного прогона
    nh = settings.tickets_dir / "needs-human"
    (settings.tickets_dir / "inbox").mkdir(exist_ok=True)
    (nh / "43031.md").replace(settings.tickets_dir / "inbox" / "43031.md")
    (nh / "43031.comments.md").unlink()

    assert Orchestrator(settings, [Stage("s", lambda c: None)]).run(inbox_ticket) == "done"
    state = StateStore(settings.runs_dir).load(inbox_ticket)
    assert state is not None
    assert state.reason is None
