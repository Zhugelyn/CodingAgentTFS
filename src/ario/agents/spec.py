"""Спецификация агента: AGENTS.md (корневой и роли), tools.yaml, ссылки на навыки (D02)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# `skills/<имя>/SKILL.md` или с одной папкой-группой: `skills/<группа>/<имя>/SKILL.md`
_SKILL_LINK = re.compile(r"skills/((?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)/SKILL\.md")
_INLINE_COMMAND = re.compile(r"!`")  # встроенное выполнение команд в навыках SDK


class SpecError(ValueError):
    """Спецификация агента неполная или противоречивая."""


@dataclass(frozen=True)
class AgentSpec:
    role: str
    instructions: str  # корневой AGENTS.md + AGENTS.md роли
    tools: list[str]
    skill_paths: list[Path]


def load_agent_spec(project_dir: Path, role: str) -> AgentSpec:
    root_md = project_dir / "AGENTS.md"
    role_dir = project_dir / "agents" / role
    role_md = role_dir / "AGENTS.md"
    tools_yaml = role_dir / "tools.yaml"
    for required in (root_md, role_md, tools_yaml):
        if not required.is_file():
            raise SpecError(f"Нет файла спецификации агента {role}: {required}")

    role_text = role_md.read_text(encoding="utf-8")
    data = yaml.safe_load(tools_yaml.read_text(encoding="utf-8"))
    tools = data.get("tools") if isinstance(data, dict) else None
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools) or not tools:
        raise SpecError(f"{tools_yaml}: ожидается `tools: [имя, ...]`")

    skill_paths: list[Path] = []
    for name in dict.fromkeys(_SKILL_LINK.findall(role_text)):  # без дублей, порядок сохраняется
        if any(set(part) <= {"."} for part in name.split("/")):  # `skills/../` выводит за папку
            raise SpecError(f"Недопустимое имя навыка: {name!r}")
        path = project_dir / "skills" / name / "SKILL.md"
        if not path.is_file():
            raise SpecError(f"AGENTS.md роли {role} ссылается на отсутствующий навык: {path}")
        if _INLINE_COMMAND.search(path.read_text(encoding="utf-8")):
            raise SpecError(
                f"Навык {name} содержит конструкцию !`команда`: SDK выполнит её при вызове навыка. "
                "Уберите или экранируйте восклицательный знак перед обратной кавычкой."
            )
        skill_paths.append(path)

    instructions = root_md.read_text(encoding="utf-8").strip() + "\n\n---\n\n" + role_text.strip()
    return AgentSpec(role, instructions, tools, skill_paths)
