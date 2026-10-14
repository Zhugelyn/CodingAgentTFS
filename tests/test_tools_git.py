from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ario.config import Settings
from ario.orchestrator import Orchestrator, Stage, StageContext, TicketNotTakenError
from ario.tools import BranchExistsError, DirtyTreeError, GitRepo, ToolError
from tests.conftest import TICKET_TEXT
from tests.test_guardrails import CONFIG


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    root = tmp_path / "rx"
    root.mkdir()
    git(root, "init", "-b", "master")
    (root / ".gitignore").write_text("bin/\n", encoding="utf-8")
    (root / "A.cs").write_text("class A {}\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    return root


@pytest.fixture
def repo(repo_path: Path) -> GitRepo:
    return GitRepo(repo_path, CONFIG.env)


def test_clean_tree_passes(repo: GitRepo) -> None:
    assert repo.changed_files() == []
    repo.ensure_clean()


def test_ignored_files_do_not_count(repo: GitRepo, repo_path: Path) -> None:
    (repo_path / "bin").mkdir()
    (repo_path / "bin" / "x.dll").write_text("x", encoding="utf-8")
    repo.ensure_clean()


def test_modified_and_untracked_files_block(repo: GitRepo, repo_path: Path) -> None:
    (repo_path / "A.cs").write_text("class A { int x; }\n", encoding="utf-8")
    (repo_path / "новый файл.cs").write_text("x", encoding="utf-8")
    assert sorted(repo.changed_files()) == ["A.cs", "новый файл.cs"]
    with pytest.raises(DirtyTreeError, match="2 файлов") as e:
        repo.ensure_clean()
    assert "A.cs" in e.value.reason
    assert repo.changed_files(include_untracked=False) == ["A.cs"]


def test_renamed_file_listed_once(repo: GitRepo, repo_path: Path) -> None:
    git(repo_path, "mv", "A.cs", "B.cs")
    assert repo.changed_files() == ["B.cs"]


def test_create_ticket_branch_from_master_even_when_on_other_branch(
    repo: GitRepo, repo_path: Path
) -> None:
    git(repo_path, "switch", "-c", "other")
    (repo_path / "other.txt").write_text("o", encoding="utf-8")
    git(repo_path, "add", ".")
    git(repo_path, "commit", "-m", "other")

    assert repo.create_ticket_branch("43031") == "ai/43031"

    assert repo.current_branch() == "ai/43031"
    assert not (repo_path / "other.txt").exists()  # ветка от master, не от other
    repo.ensure_on_ticket_branch("43031")
    assert git(repo_path, "rev-parse", "master") == git(repo_path, "rev-parse", "ai/43031")


def test_existing_branch_is_not_overwritten(repo: GitRepo, repo_path: Path) -> None:
    git(repo_path, "branch", "ai/1")
    before = git(repo_path, "rev-parse", "ai/1")
    with pytest.raises(BranchExistsError, match="уже существует"):
        repo.create_ticket_branch("1")
    assert git(repo_path, "rev-parse", "ai/1") == before
    assert repo.current_branch() == "master"


def test_missing_base_branch(repo_path: Path) -> None:
    repo = GitRepo(repo_path, CONFIG.env, base_branch="nope")
    with pytest.raises(ToolError, match="Базовая ветка"):
        repo.create_ticket_branch("1")


def test_wrong_branch_detected(repo: GitRepo) -> None:
    with pytest.raises(ToolError, match="Ожидалась ветка ai/5"):
        repo.ensure_on_ticket_branch("5")


def test_read_helpers(repo: GitRepo, repo_path: Path) -> None:
    repo.create_ticket_branch("7")
    (repo_path / "A.cs").write_text("class A { int y; }\n", encoding="utf-8")
    assert "## ai/7" in repo.status()
    assert "+class A { int y; }" in repo.diff()
    assert "init" in repo.log()


def test_diff_rejects_option_injection(repo: GitRepo) -> None:
    with pytest.raises(ToolError, match="Недопустимое имя"):
        repo.diff("--output=evil.txt")


def test_no_dangerous_operations_exposed() -> None:
    public = {n for n in dir(GitRepo) if not n.startswith("_")}
    assert not public & {"commit", "push", "reset", "checkout", "merge", "rebase", "add"}


def test_output_is_truncated(repo: GitRepo, repo_path: Path) -> None:
    (repo_path / "A.cs").write_text("x\n" * 30_000, encoding="utf-8")
    assert "вывод обрезан" in repo.diff()


# --- интеграция с оркестратором ---
def test_dirty_tree_blocks_ticket_and_keeps_it_in_inbox(
    settings: Settings, inbox_ticket: str, repo_path: Path
) -> None:
    (repo_path / "A.cs").write_text("грязь\n", encoding="utf-8")
    dirty = Settings(**{**settings.__dict__, "rx_path": repo_path})
    ran: list[str] = []

    def stage(ctx: StageContext) -> None:
        ran.append("x")

    with pytest.raises(TicketNotTakenError, match="незакоммиченные"):
        Orchestrator(dirty, [Stage("s", stage)]).run(inbox_ticket)

    assert ran == []
    assert (dirty.tickets_dir / "inbox" / "43031.md").exists()
    comments = (dirty.tickets_dir / "inbox" / "43031.comments.md").read_text(encoding="utf-8")
    assert "not_taken" in comments
    assert "A.cs" in comments
    assert not (dirty.runs_dir / "43031" / "state.json").exists()


def test_clean_tree_lets_ticket_through(settings: Settings, repo_path: Path) -> None:
    clean = Settings(**{**settings.__dict__, "rx_path": repo_path})
    inbox = clean.tickets_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "43031.md").write_text(TICKET_TEXT, encoding="utf-8")
    assert Orchestrator(clean).run("43031") == "done"


def test_resume_does_not_recheck_tree(settings: Settings, repo_path: Path) -> None:
    """При возобновлении дерево уже содержит правки самого агента: проверка не выполняется."""
    s = Settings(**{**settings.__dict__, "rx_path": repo_path})
    in_progress = s.tickets_dir / "in-progress"
    in_progress.mkdir(parents=True)
    (in_progress / "43031.md").write_text(TICKET_TEXT, encoding="utf-8")
    (repo_path / "A.cs").write_text("правки агента\n", encoding="utf-8")
    assert Orchestrator(s).run("43031") == "done"


# --- регрессия по /code-review S04 ---
def test_diff_includes_new_untracked_files(repo: GitRepo, repo_path: Path) -> None:
    repo.create_ticket_branch("8")
    (repo_path / "New.cs").write_text("class NewOne {}\n", encoding="utf-8")
    diff = repo.diff()
    assert "New.cs" in diff
    assert "+class NewOne {}" in diff
    assert git(repo_path, "log", "--oneline").count("\n") == 1  # коммитов не появилось


def test_diff_works_when_file_named_like_branch(repo: GitRepo, repo_path: Path) -> None:
    (repo_path / "master").write_text("файл с именем ветки\n", encoding="utf-8")
    assert "master" in repo.diff()


def test_broken_rx_path_means_ticket_not_taken(settings: Settings, inbox_ticket: str) -> None:
    broken = Settings(**{**settings.__dict__, "rx_path": settings.data_dir / "нет-такой-папки"})
    with pytest.raises(TicketNotTakenError):
        Orchestrator(broken, [Stage("s", lambda c: None)]).run(inbox_ticket)
    assert (broken.tickets_dir / "inbox" / "43031.md").exists()
    assert (
        "rx"
        in (broken.tickets_dir / "inbox" / "43031.comments.md").read_text(encoding="utf-8").lower()
    )


def test_diff_can_be_limited_to_one_path_when_the_whole_diff_is_truncated(
    repo: GitRepo, repo_path: Path
) -> None:
    (repo_path / "B.cs").write_text("class B {}\n", encoding="utf-8")
    git(repo_path, "add", "B.cs")
    git(repo_path, "commit", "-m", "b")
    git(repo_path, "switch", "-c", "ai/9")
    (repo_path / "A.cs").write_text("x\n" * 30_000, encoding="utf-8")  # огромный diff
    (repo_path / "B.cs").write_text("class B { int Tail; }\n", encoding="utf-8")
    full = repo.diff()
    assert "вывод обрезан" in full
    assert "int Tail" not in full  # хвост потерян в общем diff
    only_b = repo.diff(path="B.cs")
    assert "int Tail" in only_b
    assert "A.cs" not in only_b
