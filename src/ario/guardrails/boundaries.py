"""Границы инструментов: пути и окружение дочерних процессов."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from ario.errors import GuardViolation
from ario.guardrails.config import EnvConfig, PathsConfig

Mode = Literal["read", "write"]

_BAD_PATH_CHARS = re.compile(r'[<>"|?*\x00-\x1f]')
_DRIVE = re.compile(r"^[A-Za-z]:")
_WINDOWS_DEVICES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


def _reject_suspicious(raw: str) -> None:
    """Отсекает опасные формы пути ДО `resolve()`: на Windows `resolve()` UNC-пути идёт по сети
    (утечка NTLM-хэша), а `:` и хвостовые точки/пробелы дают альтернативные потоки и обход имён."""
    if _BAD_PATH_CHARS.search(raw):
        raise GuardViolation("Недопустимые символы в пути")
    normalized = raw.replace("\\", "/")
    if normalized.startswith("//"):
        raise GuardViolation("Сетевые и служебные пути (UNC, \\\\?\\) запрещены")
    rest = normalized[2:] if _DRIVE.match(normalized) else normalized
    if ":" in rest:
        raise GuardViolation("Двоеточие в пути запрещено (альтернативные потоки NTFS)")
    for part in rest.split("/"):
        if part in ("", ".", ".."):
            continue
        if part.endswith((".", " ")):
            raise GuardViolation("Имя с точкой или пробелом на конце запрещено")
        if part.split(".")[0].casefold() in _WINDOWS_DEVICES:
            raise GuardViolation("Зарезервированные имена устройств Windows запрещены")


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
        _reject_suspicious(str(path))
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        try:
            resolved = candidate.resolve()  # схлопывает `..` и символические ссылки
        except (OSError, ValueError, RuntimeError) as e:
            raise GuardViolation(f"Некорректный путь: {path} ({type(e).__name__})") from None
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

    def readable(self, resolved: Path) -> bool:
        """Можно ли читать найденный обходом файл (`read_deny`: ключи, `.env` и т.п.)."""
        try:
            rel = resolved.relative_to(self._root)
        except ValueError:
            return False
        return _denied(rel, self._config.read_deny) is None

    def visible(self, resolved: Path) -> bool:
        """Попадает ли файл в результаты поиска: читаем и не скрыт правилом `hide`.

        Скрытый файл остаётся доступным по явному пути (`check`), в `find_files` и `search_text`
        его нет: сгенерированный код не засоряет выдачу и не тратит токены.
        """
        try:
            rel = resolved.relative_to(self._root)
        except ValueError:
            return False
        return (
            _denied(rel, self._config.read_deny) is None and _denied(rel, self._config.hide) is None
        )


def clean_env(environ: Mapping[str, str], config: EnvConfig) -> dict[str, str]:
    """Копия окружения без переменных, похожих на секреты."""
    secret = re.compile(config.secret_name_pattern)
    keep = re.compile(config.keep_name_pattern) if config.keep_name_pattern else None
    return {
        k: v
        for k, v in environ.items()
        if not secret.search(k) or (keep is not None and keep.search(k))
    }
