"""Инструмент `build`: штатная сборка RX (`do dirx build`) или `dotnet build` проекта.

Разбор вывода MSBuild: агенту отдаются только ошибки (решения D03 и 2026-10-04).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ario.errors import GuardViolation
from ario.guardrails.boundaries import PathGuard, clean_env
from ario.guardrails.config import EnvConfig
from ario.tools.git import Runner
from ario.tools.proc import ProcResult, run_process

DEFAULT_TIMEOUT = 300.0  # 5 минут
MAX_ERRORS_SHOWN = 30
TAIL_LINES = 25
TRACE_TAIL_CHARS = 20_000

# Примеры:
#   src\A\B.cs(12,5): error CS1002: ; expected [C:\RX\src\A\A.csproj]
#   C:\RX\A.csproj : error NU1101: Unable to find package X
#   MSBUILD : error MSB1009: Project file does not exist.
_LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+\s+\w+\s+")
_DIAG = re.compile(
    r"^\s*(?:(?P<file>.+?)(?:\((?P<line>\d+)(?:,(?P<col>\d+))?\))?\s*:\s*)?"
    r"(?P<sev>error)\s+(?P<code>[A-Za-z]+\d+)\s*:\s*(?P<msg>.*?)"
    r"(?:\s+\[(?P<proj>[^\]]+)\])?\s*$"
)


@dataclass(frozen=True)
class Diagnostic:
    file: str | None
    line: int | None
    code: str
    message: str

    def render(self) -> str:
        where = ""
        if self.file:
            where = self.file + (f":{self.line}" if self.line else "") + " "
        return f"{where}{self.code}: {self.message}"


@dataclass
class BuildResult:
    ok: bool
    errors: list[Diagnostic] = field(default_factory=list)
    timed_out: bool = False
    raw_output: str = ""  # полный вывод; для трассы, агенту не отдаётся

    @property
    def trace_text(self) -> str:
        """Хвост вывода для трассы: ошибки и итог сборки стоят в конце, а не в начале."""
        return self.raw_output[-TRACE_TAIL_CHARS:]

    @property
    def tail(self) -> str:
        lines = [ln.rstrip() for ln in self.raw_output.splitlines() if ln.strip()]
        return "\n".join(lines[-TAIL_LINES:])

    def to_agent_text(self) -> str:
        """Краткий итог для агента: ошибки и итог, без сырого вывода."""
        if self.timed_out:
            return "Сборка прервана по таймауту."
        if self.ok:
            return "Сборка успешна."
        lines = [f"Сборка не удалась. Ошибок: {len(self.errors)}."]
        lines += [f"- {d.render()}" for d in self.errors[:MAX_ERRORS_SHOWN]]
        if len(self.errors) > MAX_ERRORS_SHOWN:
            lines.append(f"… и ещё {len(self.errors) - MAX_ERRORS_SHOWN} ошибок")
        if not self.errors:
            lines.append("Ошибок в формате MSBuild не найдено. Конец вывода сборки:")
            lines.append(self.tail)
        return "\n".join(lines)


def parse_msbuild_output(output: str) -> list[Diagnostic]:
    """Возвращает уникальные ошибки сборки. Предупреждения не разбираются и агенту не отдаются.

    MSBuild печатает каждую диагностику дважды (по ходу и в итоговом списке), поэтому
    дубликаты схлопываются.
    """
    errors: dict[tuple[str | None, int | None, str, str], Diagnostic] = {}
    for raw in output.splitlines():
        line = _LOG_PREFIX.sub("", raw)
        m = _DIAG.match(line)
        if m is None:
            continue
        line_no = int(m["line"]) if m["line"] else None
        key = (m["file"], line_no, m["code"], m["msg"])
        errors.setdefault(key, Diagnostic(m["file"], line_no, m["code"], m["msg"]))
    return list(errors.values())


def resolve_project(path: Path, root: Path) -> Path:
    """Находит `.csproj` для пути: сам файл `.csproj` или ближайший проект выше по дереву.

    В папках модулей RX лежат три проекта (`X.csproj`, `X.Ds.csproj`, `X.Dtc.csproj`):
    предпочитается тот, чьё имя совпадает с именем папки.
    """
    if path.is_file() and path.suffix.lower() == ".csproj":
        return path
    directory = path if path.is_dir() else path.parent
    while True:
        projects = sorted(directory.glob("*.csproj"))
        if projects:
            same_name = [p for p in projects if p.stem.casefold() == directory.name.casefold()]
            if len(same_name) == 1:
                return same_name[0]
            if len(projects) == 1:
                return projects[0]
            names = ", ".join(p.name for p in projects)
            rel = directory.relative_to(root).as_posix() or "."
            raise GuardViolation(
                f"В папке {rel} несколько проектов ({names}): укажите нужный .csproj"
            )
        if directory == root or root not in directory.parents:
            break
        directory = directory.parent
    raise GuardViolation(
        f"Для {path.relative_to(root).as_posix()} не найден .csproj выше по дереву"
    )


class BuildTool:
    def __init__(
        self,
        root: Path,
        guard: PathGuard,
        env_config: EnvConfig,
        runner: Runner = run_process,
        timeout: float = DEFAULT_TIMEOUT,
        command: Sequence[str] | None = None,
        command_cwd: Path | None = None,
    ) -> None:
        """`command` — штатная команда сборки всего решения (например,
        `do.bat dirx build`); без неё собирается отдельный проект через `dotnet build`."""
        self._command = list(command) if command else None
        self._command_cwd = command_cwd
        self._root = root
        self._guard = guard
        self._env: Mapping[str, str] = {
            **clean_env(os.environ, env_config),
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_NOLOGO": "1",
            "DOTNET_CLI_UI_LANGUAGE": "en",  # сообщения не локализуются: стабильный разбор
            "VSLANG": "1033",
        }
        self._runner = runner
        self._timeout = timeout

    @property
    def solution_mode(self) -> bool:
        """Собирается всё решение штатной командой, проект указывать не нужно."""
        return self._command is not None

    def build(self, project: str | None = None) -> BuildResult:
        """Собирает решение (режим команды) или один проект.

        В режиме проекта `project` — путь к `.csproj` или к любому файлу/папке внутри него
        (берётся ближайший проект выше по дереву), относительно корня RX. В режиме команды
        аргумент игнорируется. Нарушение границ (`GuardViolation`) возвращается агенту как
        ошибка инструмента.
        """
        if self._command is not None:
            result = self._runner(
                self._command, self._command_cwd or self._root, self._timeout, self._env
            )
            return self._result(result)
        if not project:
            raise GuardViolation("Укажите project: путь к .csproj или файлу внутри проекта")
        given = self._guard.check(project, "read")
        if not given.exists():
            raise GuardViolation(f"Не найдено: {project}")
        path = resolve_project(given, self._root)
        result = self._runner(
            ["dotnet", "build", str(path), "-nologo", "-nodeReuse:false", "-clp:ErrorsOnly"],
            self._root,
            self._timeout,
            self._env,
        )
        return self._result(result)

    @staticmethod
    def _result(result: ProcResult) -> BuildResult:
        errors = parse_msbuild_output(result.output)
        ok = result.returncode == 0 and not result.timed_out
        return BuildResult(ok, errors, result.timed_out, result.output)
