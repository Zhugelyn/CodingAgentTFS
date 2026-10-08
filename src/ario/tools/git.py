"""Git для харнесса: только то, что разрешено правилами. Нет commit, push, reset, checkout."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from ario.branches import DEFAULT_BRANCH_TEMPLATE, branch_name, validate_branch_template
from ario.errors import StageFailed
from ario.guardrails.boundaries import clean_env
from ario.guardrails.config import EnvConfig
from ario.tools.proc import ProcResult, ToolError, run_process

Runner = Callable[[list[str], Path, float, Mapping[str, str]], ProcResult]

_REF = re.compile(r"[\w][\w./-]*")
MAX_OUTPUT_CHARS = 20_000


class DirtyTreeError(StageFailed):
    """В рабочем дереве есть чужие незакоммиченные изменения: тикет не берём."""


class BranchExistsError(StageFailed):
    """Ветка тикета уже существует: харнесс её не перезаписывает."""


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n… вывод обрезан ({len(text) - MAX_OUTPUT_CHARS} симв.)"


class GitRepo:
    def __init__(
        self,
        root: Path,
        env_config: EnvConfig,
        base_branch: str = "master",
        branch_template: str = DEFAULT_BRANCH_TEMPLATE,
        runner: Runner = run_process,
        timeout: float = 60.0,
    ) -> None:
        self._root = root
        self._env = {
            **clean_env(os.environ, env_config),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LITERAL_PATHSPECS": "1",  # `:(top)…`, `:(glob)…` в путях агента не магия git
        }
        self._base = base_branch
        self._branch_template = validate_branch_template(branch_template)
        self._runner = runner
        self._timeout = timeout

    def _git(self, *args: str) -> str:
        result = self._runner(["git", *args], self._root, self._timeout, self._env)
        if result.timed_out:
            raise ToolError(f"git {args[0]}: превышен таймаут {self._timeout:g} с")
        if result.returncode != 0:
            raise ToolError(
                f"git {' '.join(args)} завершился с кодом {result.returncode}: "
                f"{result.output.strip()}"
            )
        return result.output

    # --- состояние ---
    def current_branch(self) -> str:
        return self._git("branch", "--show-current").strip()

    def branch_exists(self, name: str) -> bool:
        result = self._runner(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            self._root,
            self._timeout,
            self._env,
        )
        return result.returncode == 0

    def toplevel_prefix(self) -> str:
        """Путь рабочей папки относительно корня репозитория (`src/`, либо пусто)."""
        return self._git("rev-parse", "--show-prefix").strip()

    def changed_files(self, *, include_untracked: bool = True) -> list[str]:
        """Файлы с незакоммиченными изменениями (игнорируемые `.gitignore` не считаются)."""
        untracked = "--untracked-files=normal" if include_untracked else "--untracked-files=no"
        raw = self._git("status", "--porcelain=v1", "-z", untracked, "--", ".")  # только RX_PATH
        entries = [e for e in raw.split("\0") if e]
        files: list[str] = []
        skip_next = False
        for entry in entries:
            if skip_next:  # у переименований следующая запись — прежнее имя
                skip_next = False
                continue
            files.append(entry[3:])
            if entry[0] in "RC" or entry[1] in "RC":
                skip_next = True
        return files

    def ensure_clean(self, *, include_untracked: bool = True) -> None:
        files = self.changed_files(include_untracked=include_untracked)
        if files:
            shown = ", ".join(files[:10]) + (f" и ещё {len(files) - 10}" if len(files) > 10 else "")
            raise DirtyTreeError(
                f"В рабочем дереве RX есть незакоммиченные изменения ({len(files)} файлов): "
                f"{shown}. Закоммитьте или уберите их и запустите тикет снова."
            )

    # --- ветка тикета ---
    def branch_for(self, ticket_id: str, ticket_type: str | None = None) -> str:
        """Имя ветки тикета по шаблону `BRANCH_TEMPLATE`."""
        return branch_name(ticket_id, self._branch_template, ticket_type)

    def create_ticket_branch(self, ticket_id: str, ticket_type: str | None = None) -> str:
        """Создаёт ветку тикета от базовой и переключается на неё. Существующую не трогает."""
        name = self.branch_for(ticket_id, ticket_type)
        if not _REF.fullmatch(name):
            raise ToolError(f"Недопустимое имя ветки: {name}")
        if self.branch_exists(name):
            raise BranchExistsError(
                f"Ветка {name} уже существует. Харнесс её не перезаписывает: "
                f"удалите или переименуйте ветку вручную."
            )
        if not self.branch_exists(self._base):
            raise ToolError(f"Базовая ветка {self._base} не найдена")
        self._git("switch", "--create", name, self._base)
        return name

    def ensure_on_ticket_branch(self, ticket_id: str, ticket_type: str | None = None) -> None:
        expected = self.branch_for(ticket_id, ticket_type)
        actual = self.current_branch()
        if actual != expected:
            raise ToolError(f"Ожидалась ветка {expected}, сейчас {actual or 'detached HEAD'}")

    # --- чтение для агентов ---
    def status(self) -> str:
        return _truncate(self._git("status", "--short", "--branch", "--", "."))

    def diff(self, base: str | None = None, path: str | None = None) -> str:
        """Diff рабочего дерева относительно базовой ветки (по умолчанию master).

        `path` ограничивает diff одним файлом или папкой (путь от рабочей папки git): так можно
        получить часть большого diff, который иначе обрезается.
        """
        ref = base or self._base
        if not _REF.fullmatch(ref):
            raise ToolError(f"Недопустимое имя ветки: {ref}")
        # Агент не коммитит и не делает `git add`, поэтому новые файлы нужно пометить
        # (`--intent-to-add` меняет только индекс), иначе их не будет в diff для reviewer.
        self._git("add", "--intent-to-add", "--all", "--", ".")
        limit = [path] if path else ["."]  # без path: всё поддерево RX_PATH, не весь репозиторий
        return _truncate(
            self._git("diff", "--no-color", "--no-ext-diff", "--no-textconv", ref, "--", *limit)
        )

    def log(self, limit: int = 10) -> str:
        return _truncate(self._git("log", "--oneline", "-n", str(max(1, min(limit, 50)))))
