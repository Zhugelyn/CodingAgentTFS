"""Инструменты developer: правка файлов, git (чтение) и сборка.

Логика живёт в `DeveloperToolset` (обычный Python, тестируется без SDK), а классы
`*Tool` — тонкие обёртки для OpenHands. Ошибки границ и правок возвращаются агенту как
текст ошибки инструмента и не останавливают тикет.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, Self

from openhands.sdk.tool import ToolDefinition, ToolExecutor, register_tool
from pydantic import Field

from ario.agents.readonly_tools import (
    LenientAction,
    TextObservation,
    make_tool,
    register_readonly_tools,
)
from ario.errors import GuardViolation
from ario.guardrails.boundaries import PathGuard
from ario.tools.build import BuildTool as ProjectBuilder
from ario.tools.edit import EditError, FileEditor
from ario.tools.git import GitRepo
from ario.tools.proc import ToolError
from ario.trace import TraceWriter

BUILD_DESCRIPTION_SOLUTION = (
    "Сборка всего решения RX штатной командой (около минуты). Аргументы не нужны. "
    "Возвращает «Сборка успешна» либо список ошибок (файл:строка, код, текст). "
    "Предупреждения не показываются."
)
BUILD_DESCRIPTION_PROJECT = (
    "Сборка проекта. Укажите project: .csproj или файл внутри проекта (соберётся ближайший "
    "проект). Возвращает «Сборка успешна» либо список ошибок. Предупреждения не показываются."
)


class DeveloperToolset:
    """Состояние и логика инструментов developer одного запуска."""

    def __init__(
        self,
        root: Path,
        guard: PathGuard,
        git: GitRepo,
        builder: ProjectBuilder,
        trace: TraceWriter,
        stage: str,
    ) -> None:
        self._root = root.resolve()
        self._guard = guard
        self._git = git
        self._builder = builder
        self._editor = FileEditor(root, guard)
        self._trace = trace
        self._stage = stage
        self.edited: dict[str, int] = {}  # путь → число правок
        self.edits_since_build = 0
        self.last_build_ok: bool | None = None
        self.build_runs = 0

    # --- логика ---
    def edit(self, path: str, old_text: str, new_text: str) -> str:
        outcome = self._editor.replace(path, old_text, new_text)
        self.edited[outcome.path] = self.edited.get(outcome.path, 0) + 1
        self.edits_since_build += 1
        return outcome.to_agent_text()

    def build(self, project: str | None = None) -> str:
        result = self._builder.build(project)
        self.build_runs += 1
        self.last_build_ok = result.ok
        self.edits_since_build = 0
        self._trace.event(
            "build_output",
            stage=self._stage,
            agent="developer",
            project=project,
            ok=result.ok,
            errors=len(result.errors),
            text=result.trace_text,
        )
        return result.to_agent_text()

    def git_status(self) -> str:
        return self._git.status()

    def git_diff(self, path: str | None = None) -> str:
        return self._git.diff(path=self._checked(path))

    def _checked(self, path: str | None) -> str | None:
        if not path:
            return None
        return self._guard.check(path, "read").relative_to(self._root).as_posix()

    @property
    def solution_build(self) -> bool:
        return self._builder.solution_mode

    @property
    def build_is_green(self) -> bool:
        """Последняя сборка успешна и после неё не было правок."""
        return self.last_build_ok is True and self.edits_since_build == 0

    # --- регистрация в SDK ---
    def register(self) -> None:
        register_readonly_tools(self._root, self._guard)
        _register_developer_tools(self)


# --- обёртки SDK -------------------------------------------------------------------------------
class EditFileAction(LenientAction):
    path: str = Field(description="Путь к существующему файлу относительно корня RX")
    old_text: str = Field(
        description="Фрагмент файла, который нужно заменить: в точности как в файле, "
        "с отступами; должен встречаться ровно один раз"
    )
    new_text: str = Field(description="Текст, на который заменяется old_text")


class GitStatusAction(LenientAction):
    """Без аргументов."""


class GitDiffAction(LenientAction):
    path: str | None = Field(
        default=None,
        description="Показать diff только этого файла или папки (относительно корня RX); "
        "нужно, когда общий diff обрезан",
    )


class BuildAction(LenientAction):
    project: str | None = Field(
        default=None,
        description="Только если описание инструмента требует проект: путь к .csproj или файлу "
        "внутри проекта, относительно корня RX. При сборке всего решения не нужен.",
    )


def _guarded(call: Callable[[], str]) -> TextObservation:
    try:
        return TextObservation.from_text(call())
    except (GuardViolation, EditError, ToolError) as e:
        return TextObservation.from_text(str(e), is_error=True)


class _EditExecutor(ToolExecutor[EditFileAction, TextObservation]):
    def __init__(self, toolset: DeveloperToolset) -> None:
        self._toolset = toolset

    def __call__(
        self, action: EditFileAction, conversation: object | None = None
    ) -> TextObservation:
        return _guarded(lambda: self._toolset.edit(action.path, action.old_text, action.new_text))


class GitReader(Protocol):
    def git_status(self) -> str: ...

    def git_diff(self, path: str | None = None) -> str: ...


class _GitStatusExecutor(ToolExecutor[GitStatusAction, TextObservation]):
    def __init__(self, toolset: GitReader) -> None:
        self._toolset = toolset

    def __call__(
        self, action: GitStatusAction, conversation: object | None = None
    ) -> TextObservation:
        return _guarded(self._toolset.git_status)


class _GitDiffExecutor(ToolExecutor[GitDiffAction, TextObservation]):
    def __init__(self, toolset: GitReader) -> None:
        self._toolset = toolset

    def __call__(
        self, action: GitDiffAction, conversation: object | None = None
    ) -> TextObservation:
        return _guarded(lambda: self._toolset.git_diff(action.path))


class _BuildExecutor(ToolExecutor[BuildAction, TextObservation]):
    def __init__(self, toolset: DeveloperToolset) -> None:
        self._toolset = toolset

    def __call__(self, action: BuildAction, conversation: object | None = None) -> TextObservation:
        return _guarded(lambda: self._toolset.build(action.project))


class _NoCreate(ToolDefinition[Any, Any]):
    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> Sequence[Self]:
        raise NotImplementedError("инструмент регистрируется готовым экземпляром")


class EditFileTool(_NoCreate):
    pass


class GitStatusTool(_NoCreate):
    pass


class GitDiffTool(_NoCreate):
    pass


class BuildTool(_NoCreate):  # имя инструмента для модели: `build`
    pass


def register_git_tools(toolset: GitReader) -> None:
    """`git_status` и `git_diff`: общие для developer и reviewer."""
    specs: list[tuple[type[ToolDefinition[Any, Any]], type[LenientAction], Any, str, str]] = [
        (
            GitStatusTool,
            GitStatusAction,
            _GitStatusExecutor(toolset),
            "Краткий статус git: ветка и изменённые файлы.",
            "git_status",
        ),
        (
            GitDiffTool,
            GitDiffAction,
            _GitDiffExecutor(toolset),
            "Diff рабочего дерева относительно master (включая новые файлы). Большой diff "
            "обрезается: для остального вызовите с path (файл или папка).",
            "git_diff",
        ),
    ]
    for cls, action, executor, description, title in specs:
        register_tool(cls.name, make_tool(cls, action, executor, description, title))


def _register_developer_tools(toolset: DeveloperToolset) -> None:
    register_git_tools(toolset)
    specs: list[tuple[type[ToolDefinition[Any, Any]], type[LenientAction], Any, str, str]] = [
        (
            EditFileTool,
            EditFileAction,
            _EditExecutor(toolset),
            "Правка существующего файла: заменяет единственное вхождение old_text на new_text. "
            "Перевод строк и BOM файла сохраняются. Новые файлы создавать нельзя. Если old_text "
            "не найден или встречается несколько раз, вернётся ошибка: перечитайте файл.",
            "edit_file",
        ),
        (
            BuildTool,
            BuildAction,
            _BuildExecutor(toolset),
            BUILD_DESCRIPTION_SOLUTION if toolset.solution_build else BUILD_DESCRIPTION_PROJECT,
            "build",
        ),
    ]
    for cls, action, executor, description, title in specs:
        register_tool(
            cls.name, make_tool(cls, action, executor, description, title, read_only=False)
        )


class ReviewerToolset:
    """Инструменты reviewer: чтение кода и git, без правок и сборки."""

    def __init__(self, root: Path, guard: PathGuard, git: GitRepo) -> None:
        self._root = root.resolve()
        self._guard = guard
        self._git = git

    def git_status(self) -> str:
        return self._git.status()

    def git_diff(self, path: str | None = None) -> str:
        return self._git.diff(path=self._checked(path))

    def _checked(self, path: str | None) -> str | None:
        if not path:
            return None
        return self._guard.check(path, "read").relative_to(self._root).as_posix()

    def register(self) -> None:
        register_readonly_tools(self._root, self._guard)
        register_git_tools(self)
