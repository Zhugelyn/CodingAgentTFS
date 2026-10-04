"""Запуск внешних процессов с таймаутом и гарантированным завершением дерева процессов."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import psutil


class ToolError(RuntimeError):
    """Инструмент не смог выполнить действие (не найден исполняемый файл, ошибка git и т.п.)."""


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    output: str  # stdout и stderr вместе
    timed_out: bool = False


def kill_tree(proc: subprocess.Popen[str]) -> None:
    """Убивает процесс и всех потомков (dotnet порождает msbuild-узлы).

    На Windows обход через psutil: `taskkill /T` на корпоративных машинах отрабатывает
    десятки секунд (медленный опрос WMI), а таймаут сборки должен срабатывать сразу.
    """
    if sys.platform == "win32":
        try:
            victims = psutil.Process(proc.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            victims = []
        for child in victims:
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


def run_process(
    args: Sequence[str], cwd: Path, timeout: float, env: Mapping[str, str]
) -> ProcResult:
    # новая группа процессов (Windows) / новая сессия (POSIX): позволяет убить всё дерево
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    new_session = sys.platform != "win32"
    try:
        proc = subprocess.Popen(  # noqa: S603  (аргументы — список, оболочка не используется)
            list(args),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            start_new_session=new_session,
        )
    except OSError as e:  # нет исполняемого файла или рабочей папки
        raise ToolError(f"Не удалось запустить {args[0]} в {cwd}: {e}") from e
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            # потомок, отсоединившийся от дерева, может держать канал: не ждём вечно
            output, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            output = ""
        return ProcResult(
            proc.returncode if proc.returncode is not None else -9, output or "", timed_out=True
        )
    return ProcResult(proc.returncode, output or "")
