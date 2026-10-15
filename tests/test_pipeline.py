"""Сквозной поток (схема B) на настоящем git-репозитории; роли заменены сценариями."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from ario.agents.dev_tools import DeveloperToolset
from ario.config import Settings
from ario.guardrails import Guardrails
from ario.orchestrator import Orchestrator
from ario.pipeline import build_pipeline
from ario.state import StateStore
from tests.conftest import REPO_ROOT, TICKET_TEXT
from tests.fakes import RoleBackend
from tests.test_developer_stage import ANALYSIS, F_CS, FakeDotnet
from tests.test_tools_git import git

ANALYSIS_JSON = json.dumps(ANALYSIS, ensure_ascii=False)
APPROVE = json.dumps(
    {"verdict": "approve", "summary": "Всё верно", "issues": []}, ensure_ascii=False
)
REJECT = json.dumps(
    {
        "verdict": "reject",
        "summary": "Нужна доработка",
        "issues": [
            {
                "severity": "major",
                "file": "src/Mod/F.cs",
                "line": 3,
                "description": "Значение должно быть 3",
            }
        ],
    },
    ensure_ascii=False,
)


def dev(old: str, new: str, summary: str) -> Callable[[DeveloperToolset], str]:
    def script(t: DeveloperToolset) -> str:
        t.edit("src/Mod/F.cs", old, new)
        t.build("src/Mod/F.cs")
        return summary

    return script


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
    root.mkdir()
    (root / "AGENTS.md").write_text("ОБЩИЕ ПРАВИЛА", encoding="utf-8")
    for role, tools in {
        "analyst": "[read_file]",
        "developer": "[edit_file, build]",
        "reviewer": "[read_file, git_diff]",
    }.items():
        folder = root / "agents" / role
        folder.mkdir(parents=True)
        (folder / "AGENTS.md").write_text(f"РОЛЬ {role.upper()}", encoding="utf-8")
        (folder / "tools.yaml").write_text(f"tools: {tools}\n", encoding="utf-8")
    return root


@pytest.fixture
def pipe_settings(settings: Settings, repo: Path, project: Path) -> Settings:
    inbox = settings.tickets_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "43031.md").write_text(TICKET_TEXT, encoding="utf-8")
    return replace(settings, rx_path=repo, project_dir=project)


def run_pipeline(settings: Settings, backend: RoleBackend) -> str:
    guardrails = Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml")
    stages = build_pipeline(backend, runner=FakeDotnet())
    return Orchestrator(settings, stages, guardrails, preflight=lambda: None).run("43031")


def stage_statuses(settings: Settings) -> dict[str, str]:
    state = StateStore(settings.runs_dir).load("43031")
    assert state is not None
    return {name: st.status for name, st in state.stages.items()}


def test_approve_on_first_review_skips_rework(pipe_settings: Settings, repo: Path) -> None:
    backend = RoleBackend(
        analyst=[ANALYSIS_JSON],
        developer=[dev("int X = 1;", "int X = 2;", "Заменил на 2")],
        reviewer=[APPROVE],
    )
    assert run_pipeline(pipe_settings, backend) == "done"

    assert stage_statuses(pipe_settings) == {
        "analysis": "done",
        "development": "done",
        "review": "done",
        "development-2": "skipped",
        "review-2": "skipped",
        "publish": "skipped",  # публикация выключена (нет ARIO_PUBLISH и блока publish)
    }
    assert [r.role for r in backend.requests] == ["analyst", "developer", "reviewer"]
    review_task = backend.requests[2].task
    assert "<diff>" in review_task
    assert "+    int X = 2;" in review_task  # diff попал к reviewer
    assert "Сборка: успешна" in review_task  # отчёт developer тоже
    review_md = (pipe_settings.runs_dir / "43031" / "review-1.md").read_text("utf-8")
    assert review_md.startswith("# Ревью: одобрено")
    comments = (pipe_settings.tickets_dir / "done" / "43031.comments.md").read_text("utf-8")
    for role in ("[analyst]", "[developer]", "[reviewer]"):
        assert role in comments
    assert git(repo, "branch", "--show-current").strip() == "ai/43031"
    assert git(repo, "log", "--oneline").count("\n") == 1  # коммитов нет


def test_reject_then_rework_then_approve(pipe_settings: Settings, repo: Path) -> None:
    backend = RoleBackend(
        analyst=[ANALYSIS_JSON],
        developer=[
            dev("int X = 1;", "int X = 2;", "первая версия"),
            dev("int X = 2;", "int X = 3;", "доработал до 3"),
        ],
        reviewer=[REJECT, APPROVE],
    )
    assert run_pipeline(pipe_settings, backend) == "done"

    assert stage_statuses(pipe_settings) == {
        "analysis": "done",
        "development": "done",
        "review": "done",
        "development-2": "done",
        "review-2": "done",
        "publish": "skipped",
    }
    rework_task = backend.requests[3].task
    assert "<review>" in rework_task
    assert "Значение должно быть 3" in rework_task  # замечания дошли до developer
    assert (repo / "src/Mod/F.cs").read_bytes() == F_CS.replace("X = 1", "X = 3").encode()
    second_review = backend.requests[4].task
    assert "Это ревью 2 из 2" in second_review
    assert "доработал до 3" in second_review
    run_dir = pipe_settings.runs_dir / "43031"
    for name in ("development-1", "development-2", "review-1", "review-2"):
        assert (run_dir / f"{name}.md").exists(), name


def test_two_rejections_go_to_needs_human_with_remarks(pipe_settings: Settings, repo: Path) -> None:
    backend = RoleBackend(
        analyst=[ANALYSIS_JSON],
        developer=[
            dev("int X = 1;", "int X = 2;", "v1"),
            dev("int X = 2;", "int X = 4;", "v2"),
        ],
        reviewer=[REJECT, REJECT],
    )
    assert run_pipeline(pipe_settings, backend) == "needs-human"

    comments = (pipe_settings.tickets_dir / "needs-human" / "43031.comments.md").read_text("utf-8")
    assert "Ревью отклонило реализацию и после доработки" in comments
    assert "Значение должно быть 3" in comments
    assert comments.count("Ревью: отклонено") == 2  # ревью 1 и итоговое, без дубля ревью 2
    assert git(repo, "branch", "--show-current").strip() == "ai/43031"  # ветка и правки остаются
    assert (repo / "src/Mod/F.cs").read_bytes() == F_CS.replace("X = 1", "X = 4").encode()
    assert stage_statuses(pipe_settings)["review-2"] == "failed"


def test_unsuitable_ticket_stops_before_development(pipe_settings: Settings) -> None:
    unsuitable = json.dumps({"decision": "unsuitable", "reason": "Решено оставить как есть"})
    backend = RoleBackend(analyst=[unsuitable])
    assert run_pipeline(pipe_settings, backend) == "needs-human"
    assert [r.role for r in backend.requests] == ["analyst"]


def test_failed_rework_goes_to_needs_human(pipe_settings: Settings) -> None:
    def broken(t: DeveloperToolset) -> str:
        return "ничего не изменил"  # нет правок и сборки

    backend = RoleBackend(
        analyst=[ANALYSIS_JSON],
        developer=[dev("int X = 1;", "int X = 2;", "v1"), broken],
        reviewer=[REJECT],
    )
    assert run_pipeline(pipe_settings, backend) == "needs-human"
    comments = (pipe_settings.tickets_dir / "needs-human" / "43031.comments.md").read_text("utf-8")
    assert "агент не внёс изменений" in comments
    statuses = stage_statuses(pipe_settings)
    assert statuses["development-2"] == "failed"
    assert "review-2" not in statuses  # после отказа поток остановлен


def test_resume_after_crash_during_rework_does_not_repeat_finished_stages(
    pipe_settings: Settings,
) -> None:
    first = RoleBackend(
        analyst=[ANALYSIS_JSON],
        developer=[dev("int X = 1;", "int X = 2;", "v1"), KeyboardInterrupt()],  # убитый процесс
        reviewer=[REJECT],
    )
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(pipe_settings, first)
    assert (pipe_settings.tickets_dir / "in-progress" / "43031.md").exists()

    second = RoleBackend(
        developer=[dev("int X = 2;", "int X = 3;", "v2")],
        reviewer=[APPROVE],
    )
    assert run_pipeline(pipe_settings, second) == "done"
    # analyst, development-1 и review-1 не повторялись
    assert [r.role for r in second.requests] == ["developer", "reviewer"]
