from __future__ import annotations

import os
from pathlib import Path

import pytest

from ario.fsutil import RunInProgressError, RunLock, atomic_write_text


def test_atomic_write_creates_replaces_and_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "a.json"
    atomic_write_text(path, "первая версия")
    atomic_write_text(path, "вторая версия")
    assert path.read_text(encoding="utf-8") == "вторая версия"
    assert [p.name for p in path.parent.iterdir()] == ["a.json"]


def test_atomic_write_failure_keeps_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.json"
    atomic_write_text(path, "старое")

    def broken(src: object, dst: object) -> None:
        raise OSError("диск отказал")

    monkeypatch.setattr("ario.fsutil.os.replace", broken)
    with pytest.raises(OSError, match="диск"):
        atomic_write_text(path, "новое")
    assert path.read_text(encoding="utf-8") == "старое"
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]  # временный файл убран


def test_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "1"
    with RunLock(run_dir):
        assert (run_dir / "run.lock").exists()
        with pytest.raises(RunInProgressError, match=str(os.getpid())):
            RunLock(run_dir).__enter__()
    assert not (run_dir / "run.lock").exists()
    with RunLock(run_dir):  # после освобождения снова доступна
        pass


def test_failed_acquire_does_not_remove_holders_lock(tmp_path: Path) -> None:
    run_dir = tmp_path / "1"
    with RunLock(run_dir):
        try:
            with RunLock(run_dir):
                pass
        except RunInProgressError:
            pass
        assert (run_dir / "run.lock").exists()  # блокировка владельца осталась
