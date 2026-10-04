"""Правка существующих файлов с сохранением BOM и перевода строк (CRLF/LF).

Агент пишет `old_text`/`new_text` с `\\n`, инструмент приводит их к стилю файла. Новые
файлы создавать нельзя (правило прикладной разработки RX: классы создаёт DDS).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ario.errors import GuardViolation
from ario.guardrails.boundaries import PathGuard

BOM = b"\xef\xbb\xbf"
MAX_FILE_BYTES = 2_000_000
MAX_TEXT_CHARS = 20_000
CONTEXT_LINES = 3


class EditError(ValueError):
    """Правка не выполнена; сообщение возвращается агенту как ошибка инструмента."""


@dataclass(frozen=True)
class EditOutcome:
    path: str  # относительно корня RX
    first_line: int
    last_line: int
    snippet: str  # изменённый участок с номерами строк

    def to_agent_text(self) -> str:
        return (
            f"Файл {self.path} изменён: строки {self.first_line}-{self.last_line}.\n{self.snippet}"
        )


def _decode(raw: bytes) -> tuple[bool, str]:
    has_bom = raw.startswith(BOM)
    try:
        return has_bom, (raw[len(BOM) :] if has_bom else raw).decode("utf-8")
    except UnicodeDecodeError:
        raise EditError("Файл не в кодировке UTF-8: правка не поддерживается") from None


def _dominant_eol(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


class FileEditor:
    def __init__(self, root: Path, guard: PathGuard) -> None:
        self._root = root.resolve()
        self._guard = guard

    def replace(self, path: str, old_text: str, new_text: str) -> EditOutcome:
        """Заменяет единственное вхождение `old_text` на `new_text`."""
        target = self._guard.check(path, "write")  # GuardViolation: вне RX, *.g.cs, bin, obj, .git
        if not target.is_file():
            raise EditError(f"Файл не найден: {path}. Создавать новые файлы нельзя.")
        if not os.access(target, os.W_OK):
            raise EditError(f"Файл только для чтения или недоступен для записи: {path}")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise EditError(f"Файл слишком большой для правки ({target.stat().st_size} байт)")
        if not old_text:
            raise EditError("old_text пуст: укажите фрагмент файла, который нужно заменить")
        if len(old_text) > MAX_TEXT_CHARS or len(new_text) > MAX_TEXT_CHARS:
            raise EditError(f"Фрагмент длиннее {MAX_TEXT_CHARS} символов: правьте по частям")
        if old_text == new_text:
            raise EditError("old_text и new_text совпадают: изменений нет")

        has_bom, text = _decode(target.read_bytes())
        eol = _dominant_eol(text)

        def to_file_style(value: str) -> str:
            return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", eol)

        old, new = to_file_style(old_text), to_file_style(new_text)
        count = text.count(old)
        if count == 0:
            hint = ""
            if old.strip() and old.strip() in text:
                hint = " Похожий фрагмент есть, но отступы или пробелы вокруг отличаются."
            raise EditError(
                "old_text не найден в файле." + hint + " Перечитайте файл (read_file) и "
                "скопируйте фрагмент в точности, включая отступы."
            )
        if count > 1:
            raise EditError(
                f"old_text найден {count} раз: добавьте окружающие строки, "
                "чтобы фрагмент был единственным."
            )

        index = text.index(old)
        updated = text[:index] + new + text[index + len(old) :]
        self._write(target, has_bom, updated)

        first_line = text.count("\n", 0, index) + 1
        last_line = first_line + new.count("\n")
        lines = updated.split(eol)
        start = max(first_line - CONTEXT_LINES, 1)
        end = min(last_line + CONTEXT_LINES, len(lines))
        snippet = "\n".join(f"{n}\t{lines[n - 1]}" for n in range(start, end + 1))
        return EditOutcome(
            target.relative_to(self._root).as_posix(), first_line, last_line, snippet
        )

    @staticmethod
    def _write(target: Path, has_bom: bool, text: str) -> None:
        data = (BOM if has_bom else b"") + text.encode("utf-8")
        tmp = target.with_name(f".{target.name}.ario-tmp")
        try:
            tmp.write_bytes(data)
            shutil.copymode(target, tmp)
            os.replace(tmp, target)  # атомарно: оборванная запись не портит файл
        except OSError as e:  # файл занят другим процессом, нет прав на папку
            raise EditError(f"Не удалось записать файл {target.name}: {e}") from e
        finally:
            tmp.unlink(missing_ok=True)


__all__ = ["EditError", "EditOutcome", "FileEditor", "GuardViolation"]
