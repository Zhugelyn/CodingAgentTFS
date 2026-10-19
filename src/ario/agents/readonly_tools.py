"""Инструменты чтения для агентов: `find_files`, `search_text`, `read_file`.

Свои, а не из SDK: все пути проходят через `PathGuard`, нет зависимости от ripgrep
(на Windows его обычно нет), поведение одинаково на Windows и Linux.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Literal, Self

import regex
from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)
from pydantic import Field, model_validator

from ario.errors import GuardViolation
from ario.guardrails.boundaries import PathGuard

SKIP_DIRS = frozenset({"bin", "obj", ".git", "node_modules", ".vs"})
MAX_FIND_RESULTS = 100
MAX_SEARCH_MATCHES = 50
MAX_FILE_BYTES = 2_000_000
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 300
MAX_READ_CHARS = 20_000
DEFAULT_READ_LINES = 300
SEARCH_TIME_LIMIT = 20.0
MAX_PATTERN_CHARS = 300
MAX_SEARCH_LINE_CHARS = 2000  # длиннее строки не ищем: защита от катастрофического бэктрекинга
LINE_REGEX_TIMEOUT = 0.25  # секунд на одну строку
OUTLINE_MIN_LINES = 250  # у файла длиннее показывается оглавление (классы и методы)
MAX_OUTLINE_ENTRIES = 80
OUTLINE_LINE_CHARS = 110
_OUTLINE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*"
    r"(?:(?:public|private|protected|internal|static|sealed|abstract|partial|override|virtual|"
    r"async|new)\s+)*"
    r"(?:(?:class|interface|struct|enum|record)\s+\w+"
    r"|(?:public|private|protected|internal)\b[^;=]*\(.*)"
)


META_FIELDS = ("security_risk", "summary")


def repair_argument_names(data: Any, fields: set[str]) -> Any:
    """Чинит искажённые имена аргументов слабой модели: `pathsrc` → `path`, `patternx` → `pattern`.

    Qwen иногда склеивает имя аргумента с началом значения (`"pathsrc": "Packages/X"`), а
    строгая проверка SDK отклоняет такой вызов, и модель повторяет его по кругу, тратя шаги.
    Неизвестный ключ, начинающийся с имени поля, трактуется как это поле; нераспознанные
    ключи по-прежнему приводят к ошибке валидации.
    """
    if not isinstance(data, dict):
        return data
    fixed = dict(data)
    for key in [k for k in data if k not in fields]:
        if key.startswith(META_FIELDS):
            fixed.pop(key)  # служебные поля SDK (`security_risk_low`): они не влияют на действие
            continue
        owner = next((f for f in sorted(fields, key=len, reverse=True) if key.startswith(f)), None)
        if owner is not None and owner not in fixed:
            fixed[owner] = fixed.pop(key)
    return fixed


class LenientAction(Action):
    """Действие, устойчивое к искажённым именам аргументов."""

    @model_validator(mode="before")
    @classmethod
    def _repair_keys(cls, data: Any) -> Any:
        return repair_argument_names(data, set(cls.model_fields) - {"kind"})


class TextObservation(Observation):
    """Текстовый ответ инструмента."""


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`**` — любая вложенность, `*` и `?` — внутри одного сегмента, `{a,b}` — варианты."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "{" and "}" in pattern[i:]:
            end = pattern.index("}", i)
            alternatives = pattern[i + 1 : end].split(",")
            out.append("(?:" + "|".join(re.escape(a) for a in alternatives) + ")")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE)


def _walk_files(base: Path, root: Path, guard: PathGuard | None = None) -> Iterator[Path]:
    """Файлы под `base`; символические ссылки за пределами `root`, файлы под `read_deny`
    (ключи, `.env` и т.п.) и скрытые правилом `hide` (сгенерированные) пропускаются."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d.casefold() not in SKIP_DIRS)
        for name in sorted(filenames):
            file = Path(dirpath) / name
            if file.is_symlink() and not file.resolve().is_relative_to(root):
                continue
            if guard is not None and not guard.visible(file):
                continue
            yield file


def _outline(lines: list[str]) -> str:
    """Оглавление C#-файла: строки с объявлениями типов и методов (по регулярному выражению)."""
    entries: list[str] = []
    for number, line in enumerate(lines, 1):
        if _OUTLINE.match(line) and not line.lstrip().startswith(("//", "*")):
            if len(entries) >= MAX_OUTLINE_ENTRIES:
                entries.append("…")
                break
            entries.append(f"{number}: {line.strip()[:OUTLINE_LINE_CHARS]}")
    return "\n".join(entries)


def _covered(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
    """Целиком ли диапазон строк `start..end` уже входит в прочитанные раньше."""
    position = start
    for low, high in sorted(ranges):
        if low > position:
            break
        position = max(position, high + 1)
        if position > end:
            return True
    return False


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


# --- find_files --------------------------------------------------------------------------------
class FindFilesAction(LenientAction):
    pattern: str = Field(
        description='Glob-шаблон, например "**/*.cs" или "Sungero.Docflow.Server/**/*Functions.cs"'
    )
    path: str | None = Field(
        default=None,
        description="Папка поиска относительно корня репозитория; по умолчанию весь репозиторий",
    )


class FindFilesExecutor(ToolExecutor[FindFilesAction, TextObservation]):
    def __init__(self, root: Path, guard: PathGuard) -> None:
        self._root = root.resolve()
        self._guard = guard

    def __call__(
        self, action: FindFilesAction, conversation: object | None = None
    ) -> TextObservation:
        try:
            base = self._guard.check(action.path or ".", "read")
        except GuardViolation as e:
            return TextObservation.from_text(str(e), is_error=True)
        if not base.is_dir():
            return TextObservation.from_text(f"Не папка: {action.path}", is_error=True)
        pattern = action.pattern if "/" in action.pattern else "**/" + action.pattern
        regex = glob_to_regex(pattern)
        found: list[str] = []
        total = 0
        started = time.monotonic()
        for file in _walk_files(base, self._root, self._guard):
            if regex.match(file.relative_to(base).as_posix()):
                total += 1
                if len(found) < MAX_FIND_RESULTS:
                    found.append(_rel(self._root, file))
            if time.monotonic() - started > SEARCH_TIME_LIMIT:
                found.append("[поиск прерван по времени; сузьте шаблон или path]")
                break
        if not found:
            return TextObservation.from_text("Ничего не найдено.")
        text = "\n".join(found)
        if total > MAX_FIND_RESULTS:
            text += f"\n[показаны первые {MAX_FIND_RESULTS} из {total}; сузьте шаблон или path]"
        return TextObservation.from_text(text)


# --- search_text -------------------------------------------------------------------------------
class SearchTextAction(LenientAction):
    pattern: str = Field(
        description="Регулярное выражение (Python re) для поиска по содержимому файлов"
    )
    path: str | None = Field(
        default=None,
        description="Папка или файл относительно корня репозитория; по умолчанию весь репозиторий",
    )
    include: str | None = Field(
        default=None, description='Glob по имени файла, например "*.cs" или "*.mtd"'
    )
    ignore_case: bool = Field(default=False, description="Не учитывать регистр")
    mode: Literal["lines", "files"] = Field(
        default="lines",
        description='"lines": строки `путь:строка: текст`; "files": только файлы и число '
        "совпадений в каждом (дёшево для широкого шаблона: сначала найти файлы, потом читать)",
    )


class SearchTextExecutor(ToolExecutor[SearchTextAction, TextObservation]):
    def __init__(self, root: Path, guard: PathGuard) -> None:
        self._root = root.resolve()
        self._guard = guard

    def __call__(
        self, action: SearchTextAction, conversation: object | None = None
    ) -> TextObservation:
        if len(action.pattern) > MAX_PATTERN_CHARS:
            return TextObservation.from_text(
                f"Шаблон длиннее {MAX_PATTERN_CHARS} символов: упростите его", is_error=True
            )
        try:
            base = self._guard.check(action.path or ".", "read")
            # модуль regex умеет прерывать поиск по таймауту: шаблон от модели не должен
            # подвесить процесс (катастрофический бэктрекинг вида `(a+)+$`)
            matcher = regex.compile(action.pattern, regex.IGNORECASE if action.ignore_case else 0)
        except GuardViolation as e:
            return TextObservation.from_text(str(e), is_error=True)
        except regex.error as e:
            return TextObservation.from_text(
                f"Некорректное регулярное выражение: {e}", is_error=True
            )
        include = (
            glob_to_regex("**/" + action.include if action.include else "**")
            if action.include
            else None
        )
        files: Iterator[Path] = (
            iter([base]) if base.is_file() else _walk_files(base, self._root, self._guard)
        )

        matches: list[str] = []
        counts: dict[str, int] = {}  # режим files: файл → число совпадений
        by_files = action.mode == "files"
        started = time.monotonic()
        note = ""
        for file in files:
            if include is not None and not include.match(
                file.relative_to(base if base.is_dir() else base.parent).as_posix()
            ):
                continue
            try:
                if file.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                data = file.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8000]:
                continue
            for number, line in enumerate(
                data.decode("utf-8-sig", errors="replace").splitlines(), 1
            ):
                try:
                    found = matcher.search(line[:MAX_SEARCH_LINE_CHARS], timeout=LINE_REGEX_TIMEOUT)
                except TimeoutError:
                    return TextObservation.from_text(
                        "Шаблон слишком сложный: поиск по строке занял слишком долго. "
                        "Упростите шаблон (без вложенных повторов вида (a+)+).",
                        is_error=True,
                    )
                if found and by_files:
                    name = _rel(self._root, file)
                    counts[name] = counts.get(name, 0) + 1
                elif found:
                    shown = line.strip()[:MAX_LINE_CHARS]
                    matches.append(f"{_rel(self._root, file)}:{number}: {shown}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        break
            if len(counts) >= MAX_FIND_RESULTS:
                note = f"\n[показаны первые {MAX_FIND_RESULTS} файлов; уточните pattern или path]"
                break
            if len(matches) >= MAX_SEARCH_MATCHES:
                note = (
                    f"\n[показаны первые {MAX_SEARCH_MATCHES} совпадений; "
                    "уточните pattern, path или include]"
                )
                break
            if time.monotonic() - started > SEARCH_TIME_LIMIT:
                note = "\n[поиск прерван по времени; уточните path или include]"
                break
        if by_files and counts:
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            body = "\n".join(f"{name}: {n}" for name, n in ranked)
            return TextObservation.from_text(body + note)
        if not matches:
            return TextObservation.from_text("Совпадений нет." + note)
        return TextObservation.from_text("\n".join(matches) + note)


# --- read_file ---------------------------------------------------------------------------------
class ReadFileAction(LenientAction):
    path: str = Field(description="Путь к файлу относительно корня репозитория")
    offset: int = Field(default=1, ge=1, description="Номер первой строки (с 1)")
    limit: int = Field(
        default=DEFAULT_READ_LINES, ge=1, le=1000, description="Сколько строк вернуть"
    )


class ReadFileExecutor(ToolExecutor[ReadFileAction, TextObservation]):
    def __init__(self, root: Path, guard: PathGuard) -> None:
        self._root = root.resolve()
        self._guard = guard
        self._last: tuple[str, int, int, int, int] | None = None  # прошлый вызов и состояние файла
        # файл → (состояние файла, уже показанные диапазоны строк): повторное чтение не нужно
        self._shown: dict[str, tuple[tuple[int, int], list[tuple[int, int]]]] = {}

    def __call__(
        self, action: ReadFileAction, conversation: object | None = None
    ) -> TextObservation:
        try:
            path = self._guard.check(action.path, "read")
        except GuardViolation as e:
            return TextObservation.from_text(str(e), is_error=True)
        if not path.is_file():
            return TextObservation.from_text(f"Файл не найден: {action.path}", is_error=True)
        if path.stat().st_size > MAX_FILE_BYTES:
            return TextObservation.from_text(
                f"Файл слишком большой ({path.stat().st_size} байт); используйте search_text.",
                is_error=True,
            )
        info = path.stat()
        call = (str(path), action.offset, action.limit, info.st_mtime_ns, info.st_size)
        if call == self._last:
            # тот же вызов по тому же файлу подряд: точная копия ответа выглядит для SDK как цикл
            return TextObservation.from_text(
                "Повторный вызов с теми же аргументами: файл не менялся, результат тот же. "
                f"Читайте другой фрагмент (например, offset={action.offset + action.limit}) "
                "или найдите нужное место через search_text.",
                is_error=True,
            )
        self._last = call
        data = path.read_bytes()
        if b"\0" in data[:8000]:
            return TextObservation.from_text(
                "Бинарный файл: чтение не поддерживается.", is_error=True
            )
        lines = data.decode("utf-8-sig", errors="replace").splitlines()
        window = lines[action.offset - 1 : action.offset - 1 + action.limit]
        if not window:
            return TextObservation.from_text(
                f"В файле {len(lines)} строк; offset={action.offset} за его пределами."
            )
        out: list[str] = []
        size = 0
        for number, line in enumerate(window, action.offset):
            text = f"{number}\t{line}"
            size += len(text) + 1
            if size > MAX_READ_CHARS:
                break
            out.append(text)
        last = action.offset + len(out) - 1
        state = (info.st_mtime_ns, info.st_size)
        known_state, ranges = self._shown.get(str(path), (state, []))
        if known_state != state:  # файл изменился (правка): прежние чтения не в счёт
            ranges = []
        if _covered(ranges, action.offset, last):
            return TextObservation.from_text(
                f"Строки {action.offset}-{last} файла {action.path} ты уже читал выше, файл не "
                "менялся: возьми текст из истории. Читай другой фрагмент или ищи через search_text."
            )
        first_read = not ranges
        self._shown[str(path)] = (state, [*ranges, (action.offset, last)])
        footer = f"[строки {action.offset}-{last} из {len(lines)}]"
        if last < len(lines):
            footer += f" Для продолжения: offset={last + 1}."
        text = "\n".join(out) + "\n" + footer
        big_cs = len(lines) >= OUTLINE_MIN_LINES and path.suffix.lower() == ".cs"
        if first_read and action.offset == 1 and big_cs and (outline := _outline(lines)):
            text += "\n[структура файла: строка: объявление]\n" + outline
        return TextObservation.from_text(text)


# --- регистрация -------------------------------------------------------------------------------
def make_tool(
    factory: Callable[..., ToolDefinition[Any, Any]],
    action: type[Action],
    executor: ToolExecutor[Any, Any],
    description: str,
    title: str,
    *,
    read_only: bool = True,
) -> ToolDefinition[Any, Any]:
    return factory(
        action_type=action,
        observation_type=TextObservation,
        description=description,
        executor=executor,
        annotations=ToolAnnotations(
            title=title,
            readOnlyHint=read_only,
            destructiveHint=not read_only,
            idempotentHint=read_only,
            openWorldHint=False,
        ),
    )


class FindFilesTool(ToolDefinition[FindFilesAction, TextObservation]):
    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> Sequence[Self]:
        raise NotImplementedError("инструмент регистрируется готовым экземпляром")


class SearchTextTool(ToolDefinition[SearchTextAction, TextObservation]):
    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> Sequence[Self]:
        raise NotImplementedError("инструмент регистрируется готовым экземпляром")


class ReadFileTool(ToolDefinition[ReadFileAction, TextObservation]):
    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> Sequence[Self]:
        raise NotImplementedError("инструмент регистрируется готовым экземпляром")


READONLY_TOOL_NAMES: Sequence[str] = ("find_files", "search_text", "read_file")


def register_readonly_tools(root: Path, guard: PathGuard) -> None:
    """Регистрирует инструменты чтения, привязанные к корню RX и его `PathGuard`."""
    register_tool(
        FindFilesTool.name,
        make_tool(
            FindFilesTool,
            FindFilesAction,
            FindFilesExecutor(root, guard),
            "Поиск файлов по glob-шаблону (пути относительно корня репозитория). "
            "Возвращает до 100 путей.",
            "find_files",
        ),
    )
    register_tool(
        SearchTextTool.name,
        make_tool(
            SearchTextTool,
            SearchTextAction,
            SearchTextExecutor(root, guard),
            "Поиск по содержимому файлов (регулярное выражение). "
            "Возвращает `путь:строка: текст`, до 50 совпадений; mode=files даёт только файлы и "
            "число совпадений (для широкого шаблона).",
            "search_text",
        ),
    )
    register_tool(
        ReadFileTool.name,
        make_tool(
            ReadFileTool,
            ReadFileAction,
            ReadFileExecutor(root, guard),
            "Чтение файла по частям с номерами строк. Пути относительно корня репозитория; "
            "для больших файлов используйте offset и limit.",
            "read_file",
        ),
    )


class ReadonlyToolset:
    """Набор инструментов чтения (analyst, reviewer)."""

    def __init__(self, root: Path, guard: PathGuard) -> None:
        self._root = root
        self._guard = guard

    def register(self) -> None:
        register_readonly_tools(self._root, self._guard)
