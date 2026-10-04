from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from ario.errors import GuardViolation
from ario.tools import BuildTool, ProcResult, parse_msbuild_output, run_process
from tests.test_guardrails import CONFIG

FAILED_OUTPUT = """\
  Determining projects to restore...
C:\\RX\\src\\A\\B.cs(12,5): error CS1002: ; expected [C:\\RX\\src\\A\\A.csproj]
C:\\RX\\src\\A\\B.cs(20,1): warning CS8632: annotation [C:\\RX\\src\\A\\A.csproj]
C:\\RX\\src\\A\\A.csproj : error NU1101: Unable to find package X. No packages exist
MSBUILD : error MSB1009: Project file does not exist.

Build FAILED.

C:\\RX\\src\\A\\B.cs(12,5): error CS1002: ; expected [C:\\RX\\src\\A\\A.csproj]
C:\\RX\\src\\A\\B.cs(20,1): warning CS8632: annotation [C:\\RX\\src\\A\\A.csproj]
    1 Warning(s)
    3 Error(s)
"""


def test_parse_dedupes_and_extracts_fields() -> None:
    errors = parse_msbuild_output(FAILED_OUTPUT)
    assert [e.code for e in errors] == ["CS1002", "NU1101", "MSB1009"]
    first = errors[0]
    assert first.file == "C:\\RX\\src\\A\\B.cs"
    assert (first.line, first.message) == (12, "; expected")
    assert errors[2].file == "MSBUILD"


def test_parse_clean_output() -> None:
    assert parse_msbuild_output("Build succeeded.\n    0 Warning(s)\n    0 Error(s)\n") == []


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.csproj").write_text("<Project/>", encoding="utf-8")
    return tmp_path


def make_tool(root: Path, result: ProcResult, calls: list[list[str]] | None = None) -> BuildTool:
    def runner(args: list[str], cwd: Path, timeout: float, env: Mapping[str, str]) -> ProcResult:
        if calls is not None:
            calls.append(args)
            assert timeout == 300.0
            assert cwd == root
            assert "OPENROUTER_API_KEY" not in env
        return result

    return BuildTool(root, _guard(root), CONFIG.env, runner=runner)


def _guard(root: Path):  # type: ignore[no-untyped-def]
    from ario.guardrails import PathGuard

    return PathGuard(root, CONFIG.paths)


def test_successful_build(project: Path) -> None:
    calls: list[list[str]] = []
    tool = make_tool(project, ProcResult(0, "Build succeeded.\n  2 Warning(s)\n"), calls)
    result = tool.build("src/A.csproj")
    assert result.ok
    assert result.to_agent_text() == "Сборка успешна."
    assert calls[0][:2] == ["dotnet", "build"]
    assert calls[0][2] == str((project / "src" / "A.csproj").resolve())
    assert "-nodeReuse:false" in calls[0]
    assert "-clp:ErrorsOnly" in calls[0]  # предупреждения MSBuild не печатает вовсе


def test_failed_build_reports_errors_only(project: Path) -> None:
    tool = make_tool(project, ProcResult(1, FAILED_OUTPUT))
    result = tool.build("src/A.csproj")
    assert not result.ok
    text = result.to_agent_text()
    assert "Ошибок: 3" in text
    assert "CS8632" not in text  # предупреждения агенту не отдаются
    assert "редупрежд" not in text
    assert "B.cs:12 CS1002: ; expected" in text
    assert "Determining projects" not in text  # сырой вывод агенту не отдаётся
    assert result.raw_output == FAILED_OUTPUT


def test_many_errors_are_truncated(project: Path) -> None:
    output = "\n".join(f"F.cs({i},1): error CS0001: boom {i} [P.csproj]" for i in range(1, 41))
    text = make_tool(project, ProcResult(1, output)).build("src/A.csproj").to_agent_text()
    assert "Ошибок: 40" in text
    assert "и ещё 10 ошибок" in text


def test_timeout_is_reported(project: Path) -> None:
    result = make_tool(project, ProcResult(-9, "", timed_out=True)).build("src/A.csproj")
    assert not result.ok
    assert result.timed_out
    assert "таймаут" in result.to_agent_text()


@pytest.mark.parametrize(
    "path", ["src/Missing.csproj", "src/missing.cs", "../outside.csproj", "bin/A.csproj"]
)
def test_bad_projects_rejected_without_running(project: Path, path: str) -> None:
    (project / "bin").mkdir(exist_ok=True)
    calls: list[list[str]] = []
    tool = make_tool(project, ProcResult(0, ""), calls)
    if path == "bin/A.csproj":  # читать bin можно, но файла нет
        (project / "bin" / "A.csproj").write_text("<Project/>", encoding="utf-8")
        tool.build(path)
        return
    with pytest.raises(GuardViolation):
        tool.build(path)
    assert calls == []


def test_run_process_kills_on_timeout(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_process(
        [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, 1.0, dict(os.environ)
    )
    assert result.timed_out
    assert time.monotonic() - started < 20


def test_build_accepts_source_file_and_finds_nearest_project(project: Path) -> None:
    (project / "src" / "Sub" / "Deep").mkdir(parents=True)
    (project / "src" / "Sub" / "Deep" / "F.cs").write_text("class F {}", encoding="utf-8")
    calls: list[list[str]] = []
    tool = make_tool(project, ProcResult(0, "ok"), calls)
    assert tool.build("src/Sub/Deep/F.cs").ok
    assert calls[0][2] == str((project / "src" / "A.csproj").resolve())


def test_module_folder_prefers_project_named_like_folder(tmp_path: Path) -> None:
    from ario.tools.build import resolve_project

    folder = tmp_path / "Sungero.X.Server"
    folder.mkdir()
    for name in (
        "Sungero.X.Server.csproj",
        "Sungero.X.Server.Ds.csproj",
        "Sungero.X.Server.Dtc.csproj",
    ):
        (folder / name).write_text("<Project/>", encoding="utf-8")
    (folder / "Func").mkdir()
    (folder / "Func" / "F.cs").write_text("x", encoding="utf-8")
    assert (
        resolve_project((folder / "Func" / "F.cs").resolve(), tmp_path.resolve()).name
        == "Sungero.X.Server.csproj"
    )


def test_ambiguous_or_missing_project(tmp_path: Path) -> None:
    from ario.tools.build import resolve_project

    folder = tmp_path / "Mod"
    folder.mkdir()
    (folder / "A.csproj").write_text("<Project/>", encoding="utf-8")
    (folder / "B.csproj").write_text("<Project/>", encoding="utf-8")
    with pytest.raises(GuardViolation, match="несколько проектов"):
        resolve_project((folder / "X.cs").resolve(), tmp_path.resolve())
    lonely = tmp_path / "Lonely"
    lonely.mkdir()
    with pytest.raises(GuardViolation, match="не найден .csproj"):
        resolve_project((lonely / "F.cs").resolve(), tmp_path.resolve())


# --- режим штатной команды сборки (do dirx build) ---
_P = r"D:\RX\src\A\A.csproj"
_STAMP = "2026-10-03 20:41:38,101\tINFO\t"
LAUNCHER_FAILED = "\n".join(
    [
        _STAMP + r"D:\RX\src\A\B.cs(12,5): error CS1002: ; expected [" + _P + "]",
        _STAMP + r"D:\RX\src\A\B.cs(20,1): warning CS1591: нет комментария [" + _P + "]",
        _STAMP + "    Предупреждений: 447",
        _STAMP + "    Ошибок: 1",
    ]
)


def make_solution_tool(
    project: Path, result: ProcResult, calls: list[tuple[list[str], Path]]
) -> BuildTool:
    def runner(args: list[str], cwd: Path, timeout: float, env: Mapping[str, str]) -> ProcResult:
        calls.append((args, cwd))
        return result

    return BuildTool(
        project,
        _guard(project),
        CONFIG.env,
        runner=runner,
        command=[r"D:\DL\do.bat", "dirx", "build"],
        command_cwd=Path(r"D:\DL"),
    )


def test_solution_mode_runs_command_and_ignores_project(project: Path) -> None:
    calls: list[tuple[list[str], Path]] = []
    tool = make_solution_tool(project, ProcResult(0, "Сборка успешно завершена."), calls)
    assert tool.solution_mode
    assert tool.build().ok
    assert tool.build("src/A.cs").ok  # аргумент игнорируется
    assert calls[0] == ([r"D:\DL\do.bat", "dirx", "build"], Path(r"D:\DL"))
    assert len(calls) == 2


def test_solution_mode_parses_launcher_log_lines_without_warnings(project: Path) -> None:
    tool = make_solution_tool(project, ProcResult(1, LAUNCHER_FAILED), [])
    result = tool.build()
    text = result.to_agent_text()
    assert not result.ok
    assert "Ошибок: 1." in text
    assert "B.cs:12 CS1002: ; expected" in text
    assert "CS1591" not in text
    assert "447" not in text
    assert result.errors[0].file == r"D:\RX\src\A\B.cs"  # префикс времени и уровня снят


def test_failure_without_diagnostics_shows_output_tail(project: Path) -> None:
    output = "\n".join(f"строка {i}" for i in range(100)) + "\nException: Failed to build\n"
    text = make_solution_tool(project, ProcResult(1, output), []).build().to_agent_text()
    assert "Ошибок в формате MSBuild не найдено" in text
    assert "Failed to build" in text
    assert "строка 99" in text
    assert "строка 10\n" not in text  # только хвост


def test_trace_text_keeps_the_end_of_long_output() -> None:
    from ario.tools.build import BuildResult

    raw = "начало\n" + "x" * 50_000 + "\nERRORS: итог в конце"
    trace = BuildResult(False, raw_output=raw).trace_text
    assert trace.endswith("итог в конце")
    assert len(trace) == 20_000


def test_project_mode_requires_project(project: Path) -> None:
    tool = make_tool(project, ProcResult(0, "ok"))
    assert not tool.solution_mode
    with pytest.raises(GuardViolation, match="Укажите project"):
        tool.build()
