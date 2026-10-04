from __future__ import annotations

from pathlib import Path

import pytest

from ario.agents.spec import SpecError, load_agent_spec


def make_project(tmp_path: Path, role_md: str, tools: str = "tools: [read_file]\n") -> Path:
    (tmp_path / "AGENTS.md").write_text("ОБЩЕЕ", encoding="utf-8")
    role = tmp_path / "agents" / "analyst"
    role.mkdir(parents=True)
    (role / "AGENTS.md").write_text(role_md, encoding="utf-8")
    (role / "tools.yaml").write_text(tools, encoding="utf-8")
    return tmp_path


def add_skill(project: Path, name: str) -> None:
    folder = project / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\nтело", encoding="utf-8"
    )


def test_loads_instructions_tools_and_skills(tmp_path: Path) -> None:
    project = make_project(
        tmp_path,
        "РОЛЬ\n## Навыки\n- [A](../../skills/rx-arch/SKILL.md)\n"
        "- [B](../../skills/rx-code/SKILL.md)\n"
        "- [A снова](../../skills/rx-arch/SKILL.md)\n",
        "tools:\n  - find_files\n  - read_file\n",
    )
    add_skill(project, "rx-arch")
    add_skill(project, "rx-code")
    spec = load_agent_spec(project, "analyst")
    assert spec.role == "analyst"
    assert spec.tools == ["find_files", "read_file"]
    assert [p.parent.name for p in spec.skill_paths] == ["rx-arch", "rx-code"]  # без дублей
    assert spec.instructions.index("ОБЩЕЕ") < spec.instructions.index("РОЛЬ")


def test_no_skills_is_fine(tmp_path: Path) -> None:
    assert load_agent_spec(make_project(tmp_path, "РОЛЬ"), "analyst").skill_paths == []


def test_missing_skill_file_is_error(tmp_path: Path) -> None:
    project = make_project(
        tmp_path, "[x](../../skills/нет-такого/SKILL.md)".replace("нет-такого", "ghost")
    )
    with pytest.raises(SpecError, match="ghost"):
        load_agent_spec(project, "analyst")


@pytest.mark.parametrize("tools", ["tools: []\n", "tools: 5\n", "- find_files\n", "other: 1\n"])
def test_bad_tools_yaml(tmp_path: Path, tools: str) -> None:
    with pytest.raises(SpecError, match="tools"):
        load_agent_spec(make_project(tmp_path, "РОЛЬ", tools), "analyst")


def test_missing_files(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="Нет файла"):
        load_agent_spec(tmp_path, "analyst")


def test_skill_with_inline_command_is_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path, "[x](../../skills/danger/SKILL.md)")
    folder = project / "skills" / "danger"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: danger\ndescription: d\n---\nТекущая ветка: !`git branch`", encoding="utf-8"
    )
    with pytest.raises(SpecError, match="!`"):
        load_agent_spec(project, "analyst")
