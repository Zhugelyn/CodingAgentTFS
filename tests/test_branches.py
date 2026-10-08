# ruff: noqa: F811, F401  (фикстуры других модулей подключаются импортом)
"""Имя ветки тикета по шаблону BRANCH_TEMPLATE (например, antares/ai-{id})."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ario.branches import (
    DEFAULT_BRANCH_TEMPLATE,
    BranchTemplateError,
    branch_name,
    validate_branch_template,
)
from ario.config import ConfigError, Settings
from ario.guardrails import Guardrails
from ario.orchestrator import Orchestrator
from ario.pipeline import build_pipeline
from ario.tools import BranchExistsError, GitRepo
from tests.conftest import REPO_ROOT
from tests.fakes import RoleBackend
from tests.test_developer_stage import ANALYSIS, FakeDotnet
from tests.test_guardrails import CONFIG
from tests.test_pipeline import APPROVE, dev, pipe_settings, project, repo
from tests.test_tools_git import git, repo_path

TEMPLATE = "antares/ai-{id}"


# --- сам шаблон ---
def test_default_is_backward_compatible() -> None:
    assert DEFAULT_BRANCH_TEMPLATE == "ai/{id}"
    assert branch_name("43031") == "ai/43031"


@pytest.mark.parametrize(
    ("template", "ticket_type", "expected"),
    [
        (TEMPLATE, "Bug", "antares/ai-43031"),
        ("ai/{id}", None, "ai/43031"),
        ("antares/{type}/ai-{id}", "Bug", "antares/bug/ai-43031"),
        ("antares/{type}/ai-{id}", "User Story", "antares/user-story/ai-43031"),
        ("antares/{type}/ai-{id}", None, "antares/ticket/ai-43031"),
        ("{id}", "Bug", "43031"),
        ("feature/ai_{id}.x", "Bug", "feature/ai_43031.x"),
    ],
)
def test_branch_names(template: str, ticket_type: str | None, expected: str) -> None:
    validate_branch_template(template)
    assert branch_name("43031", template, ticket_type) == expected


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("antares/ai-", "{id}"),  # нет {id}
        ("{id}/{id}", "{id}"),  # два {id}
        ("antares/{name}-{id}", "неизвестные поля"),
        ("antares/{id", "скобки"),
        ("antares/{}-{id}", "неизвестные поля"),
        ("antares/ai {id}", "латинские"),  # пробел
        ("антарес/{id}", "латинские"),
        ("-bad/{id}", "латинские"),  # начинается с дефиса: похоже на опцию git
        ("a//b-{id}", "слэшей"),
        ("a/../b-{id}", "слэшей"),
        ("a/.hidden-{id}", "слэшей"),
        ("ai/{id}/", "слэшей"),
        ("ai/{id}.lock", "слэшей"),
        ("x" * 120 + "{id}", "длиннее"),
        ("a;rm -rf {id}", "латинские"),
    ],
)
def test_invalid_templates_are_rejected(template: str, message: str) -> None:
    with pytest.raises(BranchTemplateError, match=message):
        validate_branch_template(template)


# --- настройка ---
def test_settings_read_template_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "e.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARIO_GUARDRAILS", str(REPO_ROOT / "config" / "guardrails.yaml"))
    monkeypatch.setenv("BRANCH_TEMPLATE", TEMPLATE)
    assert Settings.load(empty).branch_template == TEMPLATE
    monkeypatch.setenv("BRANCH_TEMPLATE", "  ")
    assert Settings.load(empty).branch_template == DEFAULT_BRANCH_TEMPLATE
    monkeypatch.delenv("BRANCH_TEMPLATE")
    assert Settings.load(empty).branch_template == DEFAULT_BRANCH_TEMPLATE
    monkeypatch.setenv("BRANCH_TEMPLATE", "antares/ai")
    with pytest.raises(ConfigError, match="BRANCH_TEMPLATE"):
        Settings.load(empty)


def test_cli_reports_bad_template_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ario.cli as cli

    empty = tmp_path / "e.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("BRANCH_TEMPLATE", "bad template {id}")
    assert cli.main(["--env-file", str(empty), "run", "1"]) == 2
    assert "Ошибка настройки: BRANCH_TEMPLATE" in capsys.readouterr().err


# --- GitRepo ---
def test_git_creates_branch_named_by_template(repo_path: Path) -> None:
    repo = GitRepo(repo_path, CONFIG.env, branch_template=TEMPLATE)
    assert repo.create_ticket_branch("43031", "Bug") == "antares/ai-43031"
    assert repo.current_branch() == "antares/ai-43031"
    repo.ensure_on_ticket_branch("43031", "Bug")
    with pytest.raises(BranchExistsError, match="antares/ai-43031"):
        repo.create_ticket_branch("43031", "Bug")


def test_git_rejects_bad_template(repo_path: Path) -> None:
    with pytest.raises(BranchTemplateError):
        GitRepo(repo_path, CONFIG.env, branch_template="bad branch {id}")


# --- весь поток ---
def test_pipeline_works_on_the_configured_branch(
    pipe_settings: Settings,
    repo: Path,
) -> None:
    configured = replace(pipe_settings, branch_template=TEMPLATE)
    backend = RoleBackend(
        analyst=[json.dumps(ANALYSIS, ensure_ascii=False)],
        developer=[dev("int X = 1;", "int X = 2;", "Заменил на 2")],
        reviewer=[APPROVE],
    )
    guardrails = Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml")
    stages = build_pipeline(backend, runner=FakeDotnet())
    outcome = Orchestrator(configured, stages, guardrails, preflight=lambda: None).run("43031")

    assert outcome == "done"
    assert git(repo, "branch", "--show-current").strip() == "antares/ai-43031"
    assert git(repo, "branch", "--list", "ai/*").strip() == ""  # веток по умолчанию нет
    events = [
        json.loads(line)
        for line in (configured.traces_dir / "43031" / "events.jsonl")
        .read_text("utf-8")
        .splitlines()
    ]
    created = [e["data"]["branch"] for e in events if e["kind"] == "branch_created"]
    assert created == ["antares/ai-43031"]
    report = json.loads((configured.runs_dir / "43031" / "development-1.json").read_text("utf-8"))
    assert report["branch"] == "antares/ai-43031"


def test_type_placeholder_uses_the_ticket_type(pipe_settings: Settings, repo: Path) -> None:
    configured = replace(pipe_settings, branch_template="antares/{type}-ai-{id}")
    backend = RoleBackend(
        analyst=[json.dumps(ANALYSIS, ensure_ascii=False)],
        developer=[dev("int X = 1;", "int X = 2;", "ок")],
        reviewer=[APPROVE],
    )
    guardrails = Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml")
    stages = build_pipeline(backend, runner=FakeDotnet())
    Orchestrator(configured, stages, guardrails, preflight=lambda: None).run("43031")
    assert git(repo, "branch", "--show-current").strip() == "antares/bug-ai-43031"  # тикет: Bug
