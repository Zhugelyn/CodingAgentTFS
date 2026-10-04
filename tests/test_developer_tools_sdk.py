"""Инструменты developer через настоящие Agent и Conversation (ответы модели заготовлены)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ario.agents.backend import AgentRequest, OpenHandsBackend
from ario.agents.dev_tools import DeveloperToolset
from ario.guardrails import Guardrails
from ario.tools import BuildTool, GitRepo
from ario.trace import TraceWriter
from tests.conftest import REPO_ROOT
from tests.llm_stub import finish, response
from tests.test_developer_stage import F_CS, FakeDotnet
from tests.test_tools_git import git


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "rx"
    (root / "src" / "Mod").mkdir(parents=True)
    (root / "src" / "Mod" / "F.cs").write_bytes(F_CS.encode())
    (root / "src" / "Mod" / "Mod.csproj").write_text("<Project/>", encoding="utf-8")
    git(root, "init", "-b", "master")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    git(root, "switch", "-c", "ai/1")
    return root


def make(repo: Path, tmp_path: Path, dotnet: FakeDotnet) -> tuple[AgentRequest, DeveloperToolset]:
    guardrails = Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml")
    guard = guardrails.path_guard(repo)
    env = guardrails.config.env
    trace = TraceWriter(tmp_path / "traces", "1", "t")
    toolset = DeveloperToolset(
        repo,
        guard,
        GitRepo(repo, env, runner=dotnet),
        BuildTool(repo, guard, env, runner=dotnet),
        trace,
        "development",
    )
    request = AgentRequest(
        role="developer",
        root=repo,
        toolset=toolset,
        instructions="РОЛЬ",
        task="Сделай правку",
        tool_names=["find_files", "read_file", "edit_file", "git_status", "git_diff", "build"],
        skill_paths=[],
        max_steps=10,
        token_budget=guardrails.token_budget(),
        deadline=guardrails.deadline(),
        scanner=guardrails.scanner,
        trace=trace,
        stage="development",
        model_retries=1,
    )
    return request, toolset


def run(request: AgentRequest) -> Any:
    return OpenHandsBackend("openai/stub-model", "sk-test").run(request)


def test_model_edits_builds_and_checks_diff_through_sdk_tools(
    repo: Path, tmp_path: Path, script: Callable[..., list[dict[str, Any]]]
) -> None:
    dotnet = FakeDotnet()
    request, toolset = make(repo, tmp_path, dotnet)
    calls = script(
        response("read_file", {"path": "src/Mod/F.cs"}),
        response(
            "edit_file",
            {"path": "src/Mod/F.cs", "old_text": "int X = 1;", "new_text": "int X = 2;"},
        ),
        response("build", {"project": "src/Mod/F.cs"}),
        response("git_diff", {}),
        finish("Заменил 1 на 2; сборка успешна"),
    )

    result = run(request)

    assert result.final_message.startswith("Заменил")
    assert toolset.build_is_green
    assert (repo / "src/Mod/F.cs").read_bytes() == F_CS.replace("X = 1", "X = 2").encode()
    names = {t["function"]["name"] for t in calls[0]["tools"]}
    assert {
        "find_files",
        "read_file",
        "edit_file",
        "git_status",
        "git_diff",
        "build",
        "finish",
    } <= names
    assert not names & {"terminal", "file_editor", "create_branch"}
    # модель увидела результат правки, сборки и diff
    later = json.dumps(calls[-1]["messages"], ensure_ascii=False)
    assert "изменён: строки 3-3" in later
    assert "Сборка успешна" in later
    assert "+    int X = 2;" in later
    kinds = [
        json.loads(line)["kind"]
        for line in (tmp_path / "traces" / "1" / "events.jsonl").read_text("utf-8").splitlines()
    ]
    assert "build_output" in kinds


def test_failed_build_text_reaches_model_without_warnings(
    repo: Path, tmp_path: Path, script: Callable[..., list[dict[str, Any]]]
) -> None:
    dotnet = FakeDotnet()
    dotnet.ok = False
    request, toolset = make(repo, tmp_path, dotnet)
    calls = script(response("build", {"project": "src/Mod/Mod.csproj"}), finish("не собралось"))
    run(request)
    text = json.dumps(calls[1]["messages"], ensure_ascii=False)
    assert "Сборка не удалась. Ошибок: 1." in text
    assert "CS1002" in text
    assert toolset.last_build_ok is False


def test_protected_and_new_files_are_tool_errors_not_crashes(
    repo: Path, tmp_path: Path, script: Callable[..., list[dict[str, Any]]]
) -> None:
    request, toolset = make(repo, tmp_path, FakeDotnet())
    calls = script(
        response("edit_file", {"path": "src/Mod/Gen.g.cs", "old_text": "a", "new_text": "b"}),
        response("edit_file", {"path": "src/Mod/New.cs", "old_text": "a", "new_text": "b"}),
        response("edit_file", {"path": "../out.cs", "old_text": "a", "new_text": "b"}),
        finish("не смог"),
    )
    result = run(request)
    assert result.steps == 4
    text = json.dumps(calls[3]["messages"], ensure_ascii=False)
    assert "запрещено правилом" in text
    assert "Создавать новые файлы нельзя" in text
    assert "вне репозитория" in text
    assert toolset.edited == {}
