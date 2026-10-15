# ruff: noqa: F811, F401  (фикстуры подключаются импортом из test_publish)
"""Публикация: что нельзя отправлять на сервер и в каком порядке идут проверки."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ario.config import Settings
from ario.publish import _limit, neutralize_refs
from tests.test_publish import Api, git_utf8, reason, run_stage, world
from tests.test_tools_git import git


def test_generated_file_changed_by_the_build_is_never_published(
    run_stage: Callable[..., tuple[str, Api, Settings]], world: dict[str, Path]
) -> None:
    (world["work"] / "Module.g.cs").write_text("// сгенерировано сборкой\n", encoding="utf-8")
    outcome, api, settings = run_stage()
    assert outcome == "needs-human" and "Module.g.cs" in reason(settings)
    assert api.requests == []
    assert git(world["work"], "diff", "--cached", "--name-only").strip() == ""  # индекс очищен
    assert "ai/43031" not in git(world["remote"], "branch", "--list")  # на сервер не ушло
    assert git(world["work"], "rev-list", "--count", "master..HEAD").strip() == "0"  # коммита нет


def test_secret_file_is_never_published(
    run_stage: Callable[..., tuple[str, Api, Settings]], world: dict[str, Path]
) -> None:
    (world["work"] / ".env").write_text("KEY=секрет\n", encoding="utf-8")
    outcome, api, settings = run_stage()
    assert outcome == "needs-human" and ".env" in reason(settings)
    assert (
        api.requests == []
        and git(world["work"], "rev-list", "--count", "master..HEAD").strip() == "0"
    )


def test_wrong_remote_stops_before_anything_is_committed(
    run_stage: Callable[..., tuple[str, Api, Settings]], world: dict[str, Path]
) -> None:
    git(
        world["work"],
        "remote",
        "set-url",
        "origin",
        "https://evil.example.test/c/p/_git/DirectumRX",
    )
    head = git(world["work"], "rev-parse", "HEAD").strip()
    outcome, api, settings = run_stage()
    assert outcome == "needs-human" and "push отменён" in reason(settings)
    assert git(world["work"], "rev-parse", "HEAD").strip() == head  # коммита не появилось
    assert "A.cs" in git(world["work"], "status", "--short")  # правка осталась в рабочем дереве


def test_pull_request_references_are_neutralized_too() -> None:
    assert neutralize_refs("см. !73651, #5, AB#7") == "см. №73651, №5, AB№7"
    assert neutralize_refs("x != 1, a!=b, цвет #fff, 100!") == "x != 1, a!=b, цвет #fff, 100!"


def test_long_description_is_cut_on_a_line_boundary() -> None:
    text = "\n".join(f"строка {i} " + "я" * 60 for i in range(200))
    cut = _limit(text)
    assert len(cut) <= 3900 and cut.splitlines()[-1] in text.splitlines()  # последняя строка целая
    assert _limit("коротко") == "коротко"
