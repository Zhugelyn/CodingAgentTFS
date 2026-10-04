"""Этап developer на настоящем git-репозитории; «агент» — сценарий, работающий через инструменты."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from ario.agents.backend import AgentRequest, AgentResult
from ario.agents.dev_tools import DeveloperToolset
from ario.agents.developer import make_developer_stage
from ario.config import Settings
from ario.errors import GuardViolation, LimitExceeded
from ario.guardrails import Guardrails
from ario.orchestrator import Orchestrator
from ario.tools import ProcResult
from tests.conftest import REPO_ROOT, TICKET_TEXT
from tests.test_tools_git import git

ANALYSIS = {
    "decision": "suitable",
    "plan": [{"step": "Заменить 1 на 2", "files": ["src/Mod/F.cs"]}],
    "risk": "low",
    "risk_reason": "",
}
F_CS = "class F\r\n{\r\n    int X = 1;\r\n}\r\n"


class ScriptedBackend:
    """Вместо модели выполняет сценарий над инструментами developer."""

    def __init__(self, script: Callable[[DeveloperToolset], str]) -> None:
        self._script = script
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        assert isinstance(request.toolset, DeveloperToolset)
        return AgentResult(self._script(request.toolset), 100, 20, 4)


class FakeDotnet:
    """Подмена `dotnet build`: успех или заготовленная ошибка; git уходит в настоящий процесс."""

    def __init__(self) -> None:
        self.ok = True
        self.builds = 0

    def __call__(
        self, args: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> ProcResult:
        if args[0] != "dotnet":
            from ario.tools.proc import run_process

            return run_process(args, cwd, timeout, env)
        self.builds += 1
        if self.ok:
            return ProcResult(0, "Build succeeded.")
        return ProcResult(1, "F.cs(3,9): error CS1002: ; expected [P.csproj]")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "rx"
    (root / "src" / "Mod").mkdir(parents=True)
    (root / "src" / "Mod" / "F.cs").write_bytes(F_CS.encode())
    (root / "src" / "Mod" / "Mod.csproj").write_text("<Project/>", encoding="utf-8")
    git(root, "init", "-b", "master")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    return root


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "agents" / "developer").mkdir(parents=True)
    (root / "AGENTS.md").write_text("ОБЩИЕ ПРАВИЛА", encoding="utf-8")
    (root / "agents" / "developer" / "AGENTS.md").write_text("РОЛЬ DEVELOPER", encoding="utf-8")
    (root / "agents" / "developer" / "tools.yaml").write_text(
        "tools: [edit_file, build]\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def dev_settings(settings: Settings, repo: Path, project: Path) -> Settings:
    return replace(settings, rx_path=repo, project_dir=project)


def prepare_run(settings: Settings, *, analysis: bool = True) -> str:
    """Тикет в inbox и артефакт analyst в runs/: так выглядит прогон после analyst."""
    inbox = settings.tickets_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "43031.md").write_text(TICKET_TEXT, encoding="utf-8")
    if analysis:
        run_dir = settings.runs_dir / "43031"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "analysis.json").write_text(json.dumps(ANALYSIS, ensure_ascii=False), "utf-8")
    return "43031"


def run_stage(
    settings: Settings,
    backend: ScriptedBackend,
    dotnet: FakeDotnet,
    *,
    attempt: int = 1,
    preflight: Callable[[], None] | None = None,
) -> str:
    guardrails = Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml")
    stage = make_developer_stage(backend, attempt=attempt, runner=dotnet)
    return Orchestrator(settings, [stage], guardrails, preflight=preflight or (lambda: None)).run(
        "43031"
    )


def good_script(toolset: DeveloperToolset) -> str:
    toolset.edit("src/Mod/F.cs", "int X = 1;", "int X = 2;")
    toolset.build("src/Mod/F.cs")
    return "Заменил 1 на 2 в F.cs; сборка успешна."


def state_reason(settings: Settings) -> str:
    state = json.loads((settings.runs_dir / "43031" / "state.json").read_text("utf-8"))
    return str(state["reason"])


# --- успешный сценарий ---
def test_success_creates_branch_keeps_changes_uncommitted_and_writes_report(
    dev_settings: Settings, repo: Path
) -> None:
    prepare_run(dev_settings)
    dotnet = FakeDotnet()
    backend = ScriptedBackend(good_script)

    assert run_stage(dev_settings, backend, dotnet) == "done"

    assert git(repo, "branch", "--show-current").strip() == "ai/43031"
    assert (repo / "src/Mod/F.cs").read_bytes() == F_CS.replace(
        "X = 1", "X = 2"
    ).encode()  # CRLF цел
    assert git(repo, "log", "--oneline").count("\n") == 1  # коммитов нет
    assert git(repo, "status", "--short").strip() == "M src/Mod/F.cs"
    assert git(repo, "rev-parse", "master") == git(repo, "rev-parse", "HEAD")

    run_dir = dev_settings.runs_dir / "43031"
    report = json.loads((run_dir / "development-1.json").read_text("utf-8"))
    assert report["build_ok"] is True
    assert report["files_edited"] == ["src/Mod/F.cs"]
    assert report["branch"] == "ai/43031"
    assert report["side_effect_files"] == []
    assert "Сборка: успешна" in (run_dir / "development-1.md").read_text("utf-8")
    comments = (dev_settings.tickets_dir / "done" / "43031.comments.md").read_text("utf-8")
    assert "[developer]" in comments
    assert dotnet.builds == 1


def test_request_has_plan_ticket_rules_and_limits(dev_settings: Settings) -> None:
    prepare_run(dev_settings)
    backend = ScriptedBackend(good_script)
    run_stage(dev_settings, backend, FakeDotnet())

    (request,) = backend.requests
    assert request.role == "developer"
    assert request.max_steps == 60
    assert request.tool_names == ["edit_file", "build"]
    assert request.instructions.startswith("ОБЩИЕ ПРАВИЛА")
    assert "<plan>" in request.task
    assert "Заменить 1 на 2" in request.task  # план analyst
    assert "Решили оставить как есть." in request.task  # тикет
    assert "<review>" not in request.task  # первая попытка без замечаний


# --- проблемы с результатом ---
def test_no_edits_means_needs_human(dev_settings: Settings, repo: Path) -> None:
    prepare_run(dev_settings)
    backend = ScriptedBackend(lambda t: "Не вижу, что менять")
    assert run_stage(dev_settings, backend, FakeDotnet()) == "needs-human"
    assert "не внёс изменений" in state_reason(dev_settings)
    assert "Не вижу, что менять" in state_reason(dev_settings)  # резюме агента сохраняется
    assert git(repo, "status", "--short").strip() == ""


def test_failed_last_build_means_needs_human_but_keeps_branch_and_artifacts(
    dev_settings: Settings, repo: Path
) -> None:
    prepare_run(dev_settings)
    dotnet = FakeDotnet()
    dotnet.ok = False
    assert run_stage(dev_settings, ScriptedBackend(good_script), dotnet) == "needs-human"
    assert "последняя сборка не удалась" in state_reason(dev_settings)
    assert git(repo, "branch", "--show-current").strip() == "ai/43031"  # ветка остаётся
    assert (dev_settings.runs_dir / "43031" / "development-1.md").exists()


def test_never_built_and_edit_after_build_are_problems(dev_settings: Settings) -> None:
    prepare_run(dev_settings)

    def no_build(t: DeveloperToolset) -> str:
        t.edit("src/Mod/F.cs", "int X = 1;", "int X = 2;")
        return "готово"

    assert run_stage(dev_settings, ScriptedBackend(no_build), FakeDotnet()) == "needs-human"
    assert "сборка ни разу не запускалась" in state_reason(dev_settings)


def test_edit_after_green_build_requires_rebuild(dev_settings: Settings, tmp_path: Path) -> None:
    prepare_run(dev_settings)

    def late_edit(t: DeveloperToolset) -> str:
        t.edit("src/Mod/F.cs", "int X = 1;", "int X = 2;")
        t.build("src/Mod/F.cs")
        t.edit("src/Mod/F.cs", "int X = 2;", "int X = 3;")
        return "готово"

    assert run_stage(dev_settings, ScriptedBackend(late_edit), FakeDotnet()) == "needs-human"
    assert "после последней сборки были правки" in state_reason(dev_settings)


def test_side_effect_changes_are_reported_not_fatal(dev_settings: Settings, repo: Path) -> None:
    prepare_run(dev_settings)

    def script(t: DeveloperToolset) -> str:
        t.edit("src/Mod/F.cs", "int X = 1;", "int X = 2;")
        (repo / "src/Mod/Mod.csproj").write_text("<Project>changed by build</Project>", "utf-8")
        t.build("src/Mod/F.cs")
        return "готово"

    assert run_stage(dev_settings, ScriptedBackend(script), FakeDotnet()) == "done"
    report = json.loads((dev_settings.runs_dir / "43031" / "development-1.json").read_text("utf-8"))
    assert report["side_effect_files"] == ["src/Mod/Mod.csproj"]


def test_agent_cannot_touch_protected_or_new_files(dev_settings: Settings, repo: Path) -> None:
    prepare_run(dev_settings)
    errors: list[str] = []

    def script(t: DeveloperToolset) -> str:
        for path in ("src/Mod/Gen.g.cs", "../outside.cs", "src/Mod/New.cs"):
            try:
                t.edit(path, "a", "b")
            except (GuardViolation, ValueError) as e:
                errors.append(str(e))
        return "ничего не вышло"

    run_stage(dev_settings, ScriptedBackend(script), FakeDotnet())
    assert len(errors) == 3
    assert not (repo / "src/Mod/New.cs").exists()


# --- ветка ---
def test_dirty_tree_before_branch_is_not_taken_over(dev_settings: Settings, repo: Path) -> None:
    prepare_run(dev_settings)
    (repo / "src/Mod/F.cs").write_bytes(b"user's uncommitted work\n")
    ran: list[str] = []

    def script(t: DeveloperToolset) -> str:
        ran.append("x")
        return ""

    backend = ScriptedBackend(script)
    assert run_stage(dev_settings, backend, FakeDotnet()) == "needs-human"
    assert ran == []
    assert "незакоммиченные" in state_reason(dev_settings)
    assert git(repo, "branch", "--show-current").strip() == "master"  # ветка не создавалась


def test_existing_branch_is_not_overwritten(dev_settings: Settings, repo: Path) -> None:
    prepare_run(dev_settings)
    git(repo, "branch", "ai/43031")
    assert run_stage(dev_settings, ScriptedBackend(good_script), FakeDotnet()) == "needs-human"
    assert "уже существует" in state_reason(dev_settings)


def test_resume_reuses_current_ticket_branch_with_partial_work(
    dev_settings: Settings, repo: Path
) -> None:
    prepare_run(dev_settings)
    git(repo, "switch", "-c", "ai/43031")
    (repo / "src/Mod/F.cs").write_bytes(
        F_CS.replace("X = 1", "X = 5").encode()
    )  # работа до падения
    backend = ScriptedBackend(
        lambda t: (
            t.edit("src/Mod/F.cs", "int X = 5;", "int X = 2;"),
            t.build("src/Mod/F.cs"),
            "ok",
        )[2]
    )
    assert run_stage(dev_settings, backend, FakeDotnet()) == "done"
    assert git(repo, "branch", "--show-current").strip() == "ai/43031"


def test_missing_analysis_is_clear_failure(dev_settings: Settings) -> None:
    prepare_run(dev_settings, analysis=False)
    assert run_stage(dev_settings, ScriptedBackend(good_script), FakeDotnet()) == "needs-human"
    assert "analysis.json" in state_reason(dev_settings)


def test_missing_rx_path(dev_settings: Settings) -> None:
    prepare_run(dev_settings)
    settings = replace(dev_settings, rx_path=None)
    assert run_stage(settings, ScriptedBackend(good_script), FakeDotnet()) == "needs-human"
    assert "RX_PATH" in state_reason(settings)


def test_limit_error_from_backend_is_reported(dev_settings: Settings) -> None:
    prepare_run(dev_settings)

    def boom(t: DeveloperToolset) -> str:
        raise LimitExceeded("Превышен лимит шагов агента developer (60)")

    assert run_stage(dev_settings, ScriptedBackend(boom), FakeDotnet()) == "needs-human"
    assert "лимит шагов" in state_reason(dev_settings)


# --- доработка ---
def test_rework_requires_review_and_passes_remarks_on_same_branch(
    dev_settings: Settings, repo: Path
) -> None:
    prepare_run(dev_settings)
    dotnet = FakeDotnet()
    assert run_stage(dev_settings, ScriptedBackend(good_script), dotnet) == "done"

    # человек (в S06 — оркестратор) возвращает тикет в работу для второй попытки
    for name in ("43031.md", "43031.comments.md"):
        (dev_settings.tickets_dir / "done" / name).replace(
            dev_settings.tickets_dir / "inbox" / name
        )
    run_dir = dev_settings.runs_dir / "43031"
    (run_dir / "state.json").unlink()
    backend = ScriptedBackend(
        lambda t: (
            t.edit("src/Mod/F.cs", "int X = 2;", "int X = 9;"),
            t.build("src/Mod/F.cs"),
            "v2",
        )[2]
    )
    # нет замечаний: понятная ошибка
    assert run_stage(dev_settings, backend, dotnet, attempt=2) == "needs-human"
    assert "review-1.md" in state_reason(dev_settings)


def test_rework_with_review_file(dev_settings: Settings, repo: Path) -> None:
    prepare_run(dev_settings)
    git(repo, "switch", "-c", "ai/43031")
    run_dir = dev_settings.runs_dir / "43031"
    (run_dir / "review-1.md").write_text("# Ревью: отклонено\n- major: нет проверки null", "utf-8")
    backend = ScriptedBackend(
        lambda t: (
            t.edit("src/Mod/F.cs", "int X = 1;", "int X = 9;"),
            t.build("src/Mod/F.cs"),
            "v2",
        )[2]
    )
    assert run_stage(dev_settings, backend, FakeDotnet(), attempt=2) == "done"
    assert "<review>" in backend.requests[0].task
    assert "нет проверки null" in backend.requests[0].task
    assert (run_dir / "development-2.json").exists()
    state = json.loads((run_dir / "state.json").read_text("utf-8"))
    assert "development-2" in state["stages"]


def test_rework_on_wrong_branch_fails(dev_settings: Settings) -> None:
    prepare_run(dev_settings)
    (dev_settings.runs_dir / "43031" / "review-1.md").write_text("замечания", "utf-8")
    assert (
        run_stage(dev_settings, ScriptedBackend(good_script), FakeDotnet(), attempt=2)
        == "needs-human"
    )
    assert "ожидалась ветка ai/43031" in state_reason(dev_settings)


def test_rx_path_inside_repo_subfolder_has_no_false_side_effects(
    dev_settings: Settings, repo: Path
) -> None:
    """RX_PATH = <repo>/src: git показывает пути от корня репозитория, агент — от RX_PATH."""
    prepare_run(dev_settings)
    sub = replace(dev_settings, rx_path=repo / "src")

    def script(t: DeveloperToolset) -> str:
        t.edit("Mod/F.cs", "int X = 1;", "int X = 2;")
        t.build("Mod/F.cs")
        return "готово"

    assert run_stage(sub, ScriptedBackend(script), FakeDotnet()) == "done"
    report = json.loads((sub.runs_dir / "43031" / "development-1.json").read_text("utf-8"))
    assert report["files_edited"] == ["Mod/F.cs"]
    assert report["side_effect_files"] == []


def test_corrupt_analysis_file_is_a_clear_failure(dev_settings: Settings) -> None:
    """Процесс убит посреди записи (или файл правили руками): понятная причина, не traceback."""
    prepare_run(dev_settings)
    (dev_settings.runs_dir / "43031" / "analysis.json").write_text('{"decision": "suit', "utf-8")
    assert run_stage(dev_settings, ScriptedBackend(good_script), FakeDotnet()) == "needs-human"
    assert "analysis.json повреждён" in state_reason(dev_settings)
