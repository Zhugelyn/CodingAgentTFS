"""Файловые утилиты: атомарная запись артефактов и блокировка прогона тикета."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from types import TracebackType

import psutil


def atomic_write_text(path: Path, text: str) -> None:
    """Пишет файл целиком или не пишет вовсе: убитый процесс не оставит обрезанный артефакт."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class RunInProgressError(RuntimeError):
    """Этот тикет уже обрабатывает другой живой процесс."""


class RunLock:
    """Блокировка `runs/<id>/run.lock`: два процесса не ведут один тикет одновременно.

    В файле хранятся pid и время создания процесса. Блокировка убитого процесса считается
    устаревшей и перехватывается, поэтому после падения команду можно просто запустить снова.
    """

    def __init__(self, run_dir: Path) -> None:
        self._dir = run_dir
        self._path = run_dir / "run.lock"
        self._mine = False

    @staticmethod
    def _holder_alive(info: object) -> bool:
        if not isinstance(info, dict):
            return False
        pid, created = info.get("pid"), info.get("create_time")
        if not isinstance(pid, int) or not psutil.pid_exists(pid):
            return False
        try:
            actual = psutil.Process(pid).create_time()
        except psutil.Error:
            return False
        # pid мог быть переиспользован другим процессом: сверяем время создания
        return not isinstance(created, int | float) or abs(actual - created) < 2.0

    def _read_raw(self) -> str | None:
        try:
            return self._path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _try_create(self) -> bool:
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        me = psutil.Process()
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": me.pid, "create_time": me.create_time()}, f)
        return True

    def __enter__(self) -> RunLock:
        self._dir.mkdir(parents=True, exist_ok=True)
        if not self._try_create():
            raw = self._read_raw()
            try:
                info = json.loads(raw) if raw is not None else None
            except ValueError:
                info = None
            if self._holder_alive(info):
                pid = info.get("pid") if isinstance(info, dict) else "?"
                raise RunInProgressError(f"Тикет уже обрабатывается процессом {pid}")
            if self._read_raw() != raw:
                # пока мы решали, кто-то другой уже перехватил блокировку: чужую не удаляем
                raise RunInProgressError(
                    "Блокировку прогона тикета только что перехватил другой процесс"
                )
            self._path.unlink(missing_ok=True)  # устаревшая блокировка убитого процесса
            if not self._try_create():
                raise RunInProgressError("Не удалось получить блокировку прогона тикета")
        self._mine = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._mine:
            with contextlib.suppress(OSError):
                self._path.unlink()
            self._mine = False
