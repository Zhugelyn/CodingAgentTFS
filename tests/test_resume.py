"""Память прогона: возобновление после убитого процесса, повторный запуск из needs-human."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ario.config import Settings
from ario.errors import StageFailed
from ario.fsutil import RunInProgressError
from ario.guardrails import Guardrails
from ario.orchestrator import (
    Orchestrator,
    Stage,
    StageContext,
    TicketNotReopenableError,
)
from ario.state import StateStore
from tests.conftest import REPO_ROOT, TICKET_TEXT

WORKER = Path(__file__).parent / "resume_worker.py"
GUARDRAILS = REPO_ROOT / "config" / "guardrails.yaml"


def read_calls(data_dir: Path) -> list[str]:
    path = data_dir / "calls.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def start_worker(data_dir: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, str(WORKER), str(data_dir), str(GUARDRAILS)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_killed_process_resumes_without_repeating_finished_stages(
    settings: Settings, inbox_ticket: str
) -> None:
    data_dir = settings.data_dir
    (data_dir / "hang").write_text("", encoding="utf-8")

    first = start_worker(data_dir)
    deadline = time.monotonic() + 60
    while "B-hanging" not in read_calls(data_dir):
        assert first.poll() is None, "процесс завершился раньше времени"
        assert time.monotonic() < deadline, "этап B не стартовал"
        time.sleep(0.2)
    first.kill()  # жёсткое убийство посреди этапа B
    first.wait(timeout=30)

    # состояние после падения: тикет «в работе», этап A готов, B помечен как выполняющийся
    assert (settings.tickets_dir / "in-progress" / "43031.md").exists()
    state = StateStore(settings.runs_dir).load("43031")
    assert state is not None
    assert {k: v.status for k, v in state.stages.items()} == {"a": "done", "b": "running"}
    assert (settings.runs_dir / "43031" / "run.lock").exists()  # блокировка убитого процесса

    (data_dir / "hang").unlink()
    second = start_worker(data_dir)
    assert second.wait(timeout=120) == 0

    assert read_calls(data_dir) == [
        "A",
        "B-start",
        "B-hanging",
        "B-start",  # B повторён, поскольку не был завершён
        "B-done",
        "C",
        "OUTCOME done",
    ]  # A не повторялся
    assert (settings.tickets_dir / "done" / "43031.md").exists()
    assert not (settings.runs_dir / "43031" / "run.lock").exists()
    assert (settings.runs_dir / "43031" / "a.txt").read_text(encoding="utf-8") == "результат A"
    done = StateStore(settings.runs_dir).load("43031")
    assert done is not None
    assert done.status == "done"
    assert done.reason is None
    events = [
        json.loads(line)
        for line in (settings.traces_dir / "43031" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len({e["trace_id"] for e in events}) == 1  # одна трасса на оба запуска
    started = [e["data"]["resumed"] for e in events if e["kind"] == "run_started"]
    assert started == [False, True]


# --- блокировка ---
def lock_file(settings: Settings, pid: int, create_time: float | None = None) -> Path:
    import psutil

    run_dir = settings.runs_dir / "43031"
    run_dir.mkdir(parents=True, exist_ok=True)
    created = psutil.Process(pid).create_time() if create_time is None else create_time
    path = run_dir / "run.lock"
    path.write_text(json.dumps({"pid": pid, "create_time": created}), encoding="utf-8")
    return path


def make_orchestrator(settings: Settings, *stages: Stage) -> Orchestrator:
    guardrails = Guardrails.load(GUARDRAILS)
    return Orchestrator(settings, list(stages), guardrails, preflight=lambda: None)


def test_second_process_is_refused_while_first_is_alive(
    settings: Settings, inbox_ticket: str
) -> None:
    import os

    lock_file(settings, os.getpid())  # «другой» живой процесс (мы сами)
    ran: list[str] = []
    orchestrator = make_orchestrator(settings, Stage("s", lambda ctx: ran.append("x")))
    with pytest.raises(RunInProgressError, match="уже обрабатывается"):
        orchestrator.run(inbox_ticket)
    assert ran == []
    assert (settings.tickets_dir / "inbox" / "43031.md").exists()  # тикет не тронут


@pytest.mark.parametrize("kind", ["dead-pid", "reused-pid", "corrupt"])
def test_stale_lock_is_taken_over(settings: Settings, inbox_ticket: str, kind: str) -> None:
    import os

    if kind == "dead-pid":
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        lock_file(settings, proc.pid, 0.0)  # процесса больше нет
    elif kind == "reused-pid":
        lock_file(settings, os.getpid(), 12345.0)  # pid жив, но это другой процесс
    else:
        run_dir = settings.runs_dir / "43031"
        run_dir.mkdir(parents=True)
        (run_dir / "run.lock").write_text("не json", encoding="utf-8")
    assert make_orchestrator(settings, Stage("s", lambda ctx: None)).run(inbox_ticket) == "done"
    assert not (settings.runs_dir / "43031" / "run.lock").exists()


def test_lock_is_released_after_failure_and_interrupt(
    settings: Settings, inbox_ticket: str
) -> None:
    def interrupted(ctx: StageContext) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        make_orchestrator(settings, Stage("s", interrupted)).run(inbox_ticket)
    assert not (settings.runs_dir / "43031" / "run.lock").exists()


# --- повторный запуск из needs-human ---
class Flaky:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.broken = True

    def stages(self) -> list[Stage]:
        def a(ctx: StageContext) -> None:
            self.calls.append("a")

        def b(ctx: StageContext) -> None:
            self.calls.append("b")
            if self.broken:
                raise StageFailed("окружение недоступно")

        def c(ctx: StageContext) -> None:
            self.calls.append("c")

        return [Stage("a", a), Stage("b", b), Stage("c", c)]


def test_reopen_continues_from_failed_stage_with_fresh_time_budget(
    settings: Settings, inbox_ticket: str
) -> None:
    flaky = Flaky()
    orchestrator = make_orchestrator(settings, *flaky.stages())
    assert orchestrator.run(inbox_ticket) == "needs-human"
    assert flaky.calls == ["a", "b"]

    # человек починил окружение; лимит времени на тикет к этому моменту исчерпан
    states = StateStore(settings.runs_dir)
    state = states.load("43031")
    assert state is not None
    assert state.status == "needs_human"
    state.started_at = "2020-01-01T00:00:00.000+00:00"
    state.active_seconds = 99_999.0  # лимит давно исчерпан
    states.save(state)
    flaky.broken = False

    assert orchestrator.reopen("43031") == "done"
    assert flaky.calls == ["a", "b", "b", "c"]  # a не повторялся
    final = states.load("43031")
    assert final is not None
    assert (final.status, final.reason) == ("done", None)
    assert (settings.tickets_dir / "done" / "43031.md").exists()
    comments = (settings.tickets_dir / "done" / "43031.comments.md").read_text("utf-8")
    assert "окружение недоступно" in comments  # история отказа сохранена
    events = [
        json.loads(line)["kind"]
        for line in (settings.traces_dir / "43031" / "events.jsonl").read_text("utf-8").splitlines()
    ]
    assert "run_reopened" in events


def test_reopen_can_fail_again(settings: Settings, inbox_ticket: str) -> None:
    flaky = Flaky()
    orchestrator = make_orchestrator(settings, *flaky.stages())
    orchestrator.run(inbox_ticket)
    assert orchestrator.reopen("43031") == "needs-human"  # окружение всё ещё сломано
    assert flaky.calls == ["a", "b", "b"]


def test_reopen_preconditions(settings: Settings, inbox_ticket: str) -> None:
    orchestrator = make_orchestrator(settings, Stage("s", lambda ctx: None))
    with pytest.raises(TicketNotReopenableError, match="не в needs-human"):
        orchestrator.reopen(inbox_ticket)  # лежит в inbox
    # тикет в needs-human без state.json
    needs_human = settings.tickets_dir / "needs-human"
    needs_human.mkdir(parents=True, exist_ok=True)
    (needs_human / "7.md").write_text(TICKET_TEXT, encoding="utf-8")
    with pytest.raises(TicketNotReopenableError, match="state.json"):
        orchestrator.reopen("7")


def test_cli_resume(
    settings: Settings,
    inbox_ticket: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import ario.cli as cli

    flaky = Flaky()
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARIO_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("ARIO_GUARDRAILS", str(GUARDRAILS))
    monkeypatch.delenv("RX_PATH", raising=False)
    monkeypatch.setattr(cli, "build_pipeline", lambda backend: flaky.stages())
    base = ["--env-file", str(empty_env)]

    assert cli.main([*base, "run", inbox_ticket]) == 1
    flaky.broken = False
    assert cli.main([*base, "resume", inbox_ticket]) == 0
    assert "done" in capsys.readouterr().out
    assert cli.main([*base, "resume", inbox_ticket]) == 2  # уже не в needs-human


def test_downtime_between_runs_does_not_spend_the_ticket_time_budget(
    settings: Settings, inbox_ticket: str
) -> None:
    """Процесс убит вечером, команда запущена утром: простой не считается временем работы."""
    from ario.state import RunState

    inbox = settings.tickets_dir / "inbox" / "43031.md"
    in_progress = settings.tickets_dir / "in-progress"
    in_progress.mkdir(parents=True)
    inbox.replace(in_progress / "43031.md")
    StateStore(settings.runs_dir).save(
        RunState(ticket_id="43031", started_at="2020-01-01T00:00:00.000+00:00", active_seconds=60.0)
    )
    assert make_orchestrator(settings, Stage("s", lambda ctx: None)).run("43031") == "done"
    state = StateStore(settings.runs_dir).load("43031")
    assert state is not None
    assert 60.0 <= state.active_seconds < 120.0  # накоплено прежнее время плюс этот запуск


def test_active_time_is_accumulated_across_stages(settings: Settings, inbox_ticket: str) -> None:
    def slow(ctx: StageContext) -> None:
        time.sleep(0.3)

    make_orchestrator(settings, Stage("a", slow), Stage("b", slow)).run(inbox_ticket)
    state = StateStore(settings.runs_dir).load("43031")
    assert state is not None
    assert state.active_seconds >= 0.6
