"""Границы инструментов: пути, команды, окружение."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from ario.errors import GuardViolation
from ario.guardrails.config import CommandsConfig, EnvConfig, PathsConfig

Mode = Literal["read", "write"]

_SHELL_META = re.compile(r"[;&|<>`$\r\n]")


def _denied(rel: Path, rules: list[str]) -> str | None:
    # casefold: на Windows/macOS `BIN/` и `Bin/` — та же папка, что `bin/`
    parts = tuple(part.casefold() for part in rel.parts)
    for rule in rules:
        if rule.endswith("/"):
            if rule[:-1].casefold() in parts:
                return rule
        elif parts and fnmatch.fnmatchcase(parts[-1], rule.casefold()):
            return rule
    return None


class PathGuard:
    """Разрешает пути только внутри корня репозитория и применяет deny-правила."""

    def __init__(self, root: Path, config: PathsConfig) -> None:
        self._root = root.resolve()
        self._config = config

    def check(self, path: str | Path, mode: Mode) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve()  # схлопывает `..` и символические ссылки
        try:
            rel = resolved.relative_to(self._root)
        except ValueError:
            raise GuardViolation(f"Путь вне репозитория запрещён: {path}") from None
        rules = self._config.write_deny if mode == "write" else self._config.read_deny
        rule = _denied(rel, rules)
        if rule is not None:
            verb = "Запись в" if mode == "write" else "Чтение"
            raise GuardViolation(f"{verb} {rel.as_posix()} запрещено правилом {rule!r}")
        return resolved


class CommandPolicy:
    """Белый список команд терминала."""

    def __init__(self, config: CommandsConfig) -> None:
        self._allow = [re.compile(p) for p in config.allow]
        self._deny = [re.compile(p) for p in config.deny]

    def check(self, command: str) -> str:
        cmd = command.strip()
        if not cmd:
            raise GuardViolation("Пустая команда")
        if _SHELL_META.search(cmd):
            raise GuardViolation("Команда содержит запрещённые символы (; & | < > ` $ или перенос)")
        for rx in self._deny:
            if rx.search(cmd):
                raise GuardViolation(f"Команда запрещена правилом {rx.pattern!r}: {cmd}")
        if not any(rx.fullmatch(cmd) for rx in self._allow):
            raise GuardViolation(f"Команда не входит в белый список: {cmd}")
        return cmd


def clean_env(environ: Mapping[str, str], config: EnvConfig) -> dict[str, str]:
    """Копия окружения без переменных, похожих на секреты."""
    secret = re.compile(config.secret_name_pattern)
    return {k: v for k, v in environ.items() if not secret.search(k)}
