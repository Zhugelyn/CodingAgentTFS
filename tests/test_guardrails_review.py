"""Регрессионные тесты по замечаниям /code-review этапа S02."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ario.config import Settings
from ario.errors import GuardViolation
from ario.guardrails import Deadline, InjectionScanner, PathGuard, is_retryable
from ario.orchestrator import Orchestrator, Stage, StageContext
from ario.state import RunState, StateStore
from tests.conftest import REPO_ROOT, TICKET_TEXT
from tests.test_guardrails import CONFIG


@pytest.mark.parametrize("path", ["BIN/x.dll", "Obj/y.cs", ".GIT/config", "src/Foo.G.CS"])
def test_write_deny_is_case_insensitive(tmp_path: Path, path: str) -> None:
    with pytest.raises(GuardViolation):
        PathGuard(tmp_path, CONFIG.paths).check(path, "write")


@pytest.mark.parametrize(
    "text",
    [
        "The service should act as a proxy between the modules",
        "Сервис должен работать как прокси; теперь ты видишь список",
    ],
)
def test_role_override_no_false_positive(text: str) -> None:
    assert InjectionScanner(CONFIG.injection).scan(text, "тест") == []


def test_timeout_408_is_retryable() -> None:
    class Timeout(Exception):
        status_code = 408

    assert is_retryable(Timeout())


def test_deadline_accounts_for_elapsed() -> None:
    d = Deadline(1800, clock=lambda: 0.0, elapsed=1801)
    assert d.remaining() < 0


def test_resumed_ticket_keeps_original_start_time(settings: Settings) -> None:
    """Повторный запуск после падения не даёт тикету новые 30 минут."""
    in_progress = settings.tickets_dir / "in-progress"
    in_progress.mkdir(parents=True)
    (in_progress / "43031.md").write_text(TICKET_TEXT, encoding="utf-8")
    old_start = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    StateStore(settings.runs_dir).save(
        RunState(ticket_id="43031", started_at=old_start, active_seconds=31 * 60)
    )
    ran: list[str] = []

    def stage(ctx: StageContext) -> None:
        ran.append("x")

    assert Orchestrator(settings, [Stage("s", stage)]).run("43031") == "needs-human"
    assert ran == []
    trace = settings.traces_dir / "43031" / "events.jsonl"
    assert "timeout" in trace.read_text(encoding="utf-8")
    assert json.loads(trace.read_text(encoding="utf-8").splitlines()[0])["kind"] == "run_started"
    assert REPO_ROOT.exists()


def test_clean_env_keeps_nuget_credentials_but_drops_other_secrets() -> None:
    from ario.guardrails import clean_env

    env = {
        "PATH": "/bin",
        "NuGetPackageSourceCredentials_rx-feed": "u;p",
        "OPENROUTER_API_KEY": "k",
        "MY_CREDENTIAL": "x",
    }
    assert clean_env(env, CONFIG.env) == {
        "PATH": "/bin",
        "NuGetPackageSourceCredentials_rx-feed": "u;p",
    }
