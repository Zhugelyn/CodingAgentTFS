"""Analyst видит код базовой ветки, а не той, что случайно выбрана в клоне."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ario.agents.analyst import make_analyst_stage
from ario.agents.backend import AgentRequest, AgentResult
from ario.config import Settings
from ario.guardrails import Guardrails
from ario.orchestrator import Orchestrator
from ario.repos import RepoProfile, RepoRegistry
from tests.conftest import REPO_ROOT, TICKET_TEXT
from tests.test_tools_git import git

BASE = "antares/base"
TICKET_BRANCH = "ai/43031"


class Recorder:
    """Запоминает, на какой ветке стоял клон в момент запуска агента."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.branches: list[str] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.branches.append(git(self.repo, "branch", "--show-current").strip())
        return AgentResult(json.dumps({"decision": "unsuitable", "reason": "тест"}), 1, 1, 1)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    root = tmp_path / "rx"
    root.mkdir()
    git(root, "init", "-b", "master")
    (root / "A.cs").write_text("class A {}\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    git(root, "switch", "-c", BASE)
    (root / "A.cs").write_text("class A { /* есть метод */ }\n", encoding="utf-8")
    git(root, "commit", "-am", "base")
    git(root, "switch", "-c", "antares/001-test")  # случайно выбранная в клоне ветка
    (root / "A.cs").write_text("class A { /* нет метода */ }\n", encoding="utf-8")
    git(root, "commit", "-am", "other")
    return root


def run_analysis(settings: Settings, clone: Path, recorder: Recorder) -> str:
    inbox = settings.tickets_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "43031.md").write_text(TICKET_TEXT, encoding="utf-8")
    profile = RepoProfile(
        name="rx",
        path=clone,
        base_branch=BASE,
        branch_template="ai/{id}",
        knowledge_dir=REPO_ROOT,
    )
    return Orchestrator(
        replace(settings, project_dir=REPO_ROOT),
        [make_analyst_stage(recorder)],
        Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml"),
        preflight=lambda: None,
        repos=RepoRegistry({"rx": profile}, "rx"),
    ).run("43031")


def events(settings: Settings) -> str:
    return (settings.traces_dir / "43031" / "events.jsonl").read_text(encoding="utf-8")


def test_wrong_branch_is_switched_to_the_base_before_the_agent_starts(
    settings: Settings, clone: Path
) -> None:
    recorder = Recorder(clone)
    run_analysis(settings, clone, recorder)
    assert recorder.branches == [BASE]  # агент видит код базы
    assert "есть метод" in (clone / "A.cs").read_text(encoding="utf-8")
    assert '"kind": "branch_switched"' in events(settings)


def test_base_and_ticket_branches_are_left_as_they_are(settings: Settings, clone: Path) -> None:
    git(clone, "switch", BASE)
    recorder = Recorder(clone)
    run_analysis(settings, clone, recorder)
    assert recorder.branches == [BASE] and '"kind": "branch_switched"' not in events(settings)


def test_ticket_branch_is_not_switched_away(settings: Settings, clone: Path) -> None:
    git(clone, "switch", "-c", TICKET_BRANCH, BASE)  # повторный запуск: ветка тикета уже есть
    recorder = Recorder(clone)
    run_analysis(settings, clone, recorder)
    assert recorder.branches == [TICKET_BRANCH]


def test_dirty_tree_on_the_wrong_branch_is_refused_not_switched(
    settings: Settings, clone: Path
) -> None:
    (clone / "A.cs").write_text("чужие правки\n", encoding="utf-8")
    recorder = Recorder(clone)
    assert run_analysis(settings, clone, recorder) == "needs-human"
    assert recorder.branches == []  # агент не запускался
    comment = (settings.tickets_dir / "needs-human" / "43031.comments.md").read_text("utf-8")
    assert "незакоммиченные" in comment
    assert git(clone, "branch", "--show-current").strip() == "antares/001-test"  # не тронуто


def test_a_missing_base_branch_is_a_clear_refusal(settings: Settings, clone: Path) -> None:
    git(clone, "branch", "-D", BASE)
    recorder = Recorder(clone)
    assert run_analysis(settings, clone, recorder) == "needs-human"
    comment = (settings.tickets_dir / "needs-human" / "43031.comments.md").read_text("utf-8")
    assert BASE in comment and "не найдена" in comment
