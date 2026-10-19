from __future__ import annotations

from pathlib import Path

import pytest

from ario.errors import GuardViolation
from ario.guardrails import PathGuard
from ario.tools.edit import BOM, EditError, FileEditor
from tests.test_guardrails import CONFIG


@pytest.fixture
def rx(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    return tmp_path


@pytest.fixture
def editor(rx: Path) -> FileEditor:
    return FileEditor(rx, PathGuard(rx, CONFIG.paths))


def write(rx: Path, name: str, data: bytes) -> Path:
    path = rx / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


CODE_LF = "class A\n{\n    void Foo()\n    {\n        var x = 1;\n    }\n}\n"


def test_lf_file_is_edited(editor: FileEditor, rx: Path) -> None:
    path = write(rx, "src/A.cs", CODE_LF.encode())
    outcome = editor.replace("src/A.cs", "var x = 1;", "var x = 2;")
    assert path.read_bytes() == CODE_LF.replace("var x = 1;", "var x = 2;").encode()
    assert outcome.path == "src/A.cs"
    assert (outcome.first_line, outcome.last_line) == (5, 5)
    assert "5\t        var x = 2;" in outcome.snippet
    assert "изменён: строки 5-5" in outcome.to_agent_text()


def test_crlf_is_preserved_even_when_agent_sends_lf(editor: FileEditor, rx: Path) -> None:
    original = CODE_LF.replace("\n", "\r\n").encode()
    path = write(rx, "src/A.cs", original)
    editor.replace(
        "src/A.cs",
        "    void Foo()\n    {\n        var x = 1;\n",  # LF, а в файле CRLF
        "    void Foo()\n    {\n        var x = 1;\n        var y = 2;\n",
    )
    data = path.read_bytes()
    assert b"var y = 2;\r\n" in data
    assert data.count(b"\r\n") == original.count(b"\r\n") + 1
    assert b"\n" not in data.replace(b"\r\n", b"")  # одиноких LF не появилось


def test_bom_is_preserved(editor: FileEditor, rx: Path) -> None:
    path = write(rx, "src/A.cs", BOM + CODE_LF.replace("\n", "\r\n").encode())
    editor.replace("src/A.cs", "var x = 1;", "var x = 3;")
    data = path.read_bytes()
    assert data.startswith(BOM)
    assert not data.startswith(BOM + BOM)
    assert b"var x = 3;" in data


def test_no_bom_is_not_added(editor: FileEditor, rx: Path) -> None:
    path = write(rx, "src/A.cs", CODE_LF.encode())
    editor.replace("src/A.cs", "var x = 1;", "var x = 3;")
    assert not path.read_bytes().startswith(BOM)


def test_multiline_new_text_and_line_range(editor: FileEditor, rx: Path) -> None:
    write(rx, "src/A.cs", CODE_LF.encode())
    outcome = editor.replace("src/A.cs", "var x = 1;", "var x = 1;\nvar y = 2;\nvar z = 3;")
    assert (outcome.first_line, outcome.last_line) == (5, 7)


def test_cyrillic_and_missing_trailing_newline(editor: FileEditor, rx: Path) -> None:
    path = write(rx, "src/R.resx", "<v>Привет</v>".encode())
    editor.replace("src/R.resx", "Привет", "Здравствуйте")
    assert (
        path.read_text(encoding="utf-8") == "<v>Здравствуйте</v>"
    )  # без добавленного перевода строки


def test_dominant_eol_wins_in_mixed_file(editor: FileEditor, rx: Path) -> None:
    path = write(rx, "src/M.cs", b"a\r\nb\r\nc\r\nd\n")
    editor.replace("src/M.cs", "b\nc", "b\nX\nc")
    assert path.read_bytes() == b"a\r\nb\r\nX\r\nc\r\nd\n"


def test_not_found_with_hint_about_indentation(editor: FileEditor, rx: Path) -> None:
    write(rx, "src/A.cs", CODE_LF.encode())
    with pytest.raises(EditError, match="отступы"):
        editor.replace("src/A.cs", "	var x = 1;", "var x = 2;")  # в файле пробелы, не таб
    with pytest.raises(EditError, match="не найден"):
        editor.replace("src/A.cs", "совсем другое", "x")


def test_ambiguous_match_is_rejected_and_file_untouched(editor: FileEditor, rx: Path) -> None:
    original = b"x = 1;\nx = 1;\n"
    path = write(rx, "src/A.cs", original)
    with pytest.raises(EditError, match="2 раз"):
        editor.replace("src/A.cs", "x = 1;", "x = 2;")
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [("", "x", "пуст"), ("a", "a", "совпадают"), ("a" * 20_001, "b", "длиннее")],
)
def test_degenerate_edits(editor: FileEditor, rx: Path, old: str, new: str, message: str) -> None:
    write(rx, "src/A.cs", b"a")
    with pytest.raises(EditError, match=message):
        editor.replace("src/A.cs", old, new)


def test_new_files_cannot_be_created(editor: FileEditor, rx: Path) -> None:
    with pytest.raises(EditError, match="Создавать новые файлы нельзя"):
        editor.replace("src/New.cs", "a", "b")
    assert not (rx / "src" / "New.cs").exists()


@pytest.mark.parametrize(
    "path",
    ["src/Foo.g.cs", "bin/x.cs", "src/obj/y.cs", ".git/config", "../outside.cs", "BIN/z.cs"],
)
def test_protected_paths_are_guard_violations(editor: FileEditor, rx: Path, path: str) -> None:
    write(rx, "src/Foo.g.cs", b"a")
    for protected in ("bin/x.cs", "src/obj/y.cs", ".git/config", "BIN/z.cs"):
        write(rx, protected, b"a")
    with pytest.raises(GuardViolation):
        editor.replace(path, "a", "b")


def test_non_utf8_and_huge_files_refused(editor: FileEditor, rx: Path) -> None:
    write(rx, "src/Old.cs", "привет".encode("cp1251"))
    with pytest.raises(EditError, match="UTF-8"):
        editor.replace("src/Old.cs", "x", "y")
    write(rx, "src/Big.cs", b"a" * 2_100_000)
    with pytest.raises(EditError, match="слишком большой"):
        editor.replace("src/Big.cs", "a", "b")


def test_no_temp_files_left_behind(editor: FileEditor, rx: Path) -> None:
    write(rx, "src/A.cs", CODE_LF.encode())
    editor.replace("src/A.cs", "var x = 1;", "var x = 2;")
    assert sorted(p.name for p in (rx / "src").iterdir()) == ["A.cs"]


def test_readonly_file_gives_clear_error_and_stays_intact(editor: FileEditor, rx: Path) -> None:
    import os
    import stat

    path = write(rx, "src/Ro.cs", b"a")
    os.chmod(path, stat.S_IREAD)
    try:
        if os.access(path, os.W_OK):
            pytest.skip("процесс обходит права (root)")
        with pytest.raises(EditError, match="только для чтения"):
            editor.replace("src/Ro.cs", "a", "b")
        assert path.read_bytes() == b"a"
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def test_replace_failure_becomes_edit_error(
    editor: FileEditor, rx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(rx, "src/Busy.cs", b"a")

    def locked(src: object, dst: object) -> None:
        raise PermissionError("файл занят другим процессом")

    monkeypatch.setattr("ario.tools.edit.os.replace", locked)
    with pytest.raises(EditError, match="Не удалось записать"):
        editor.replace("src/Busy.cs", "a", "b")
    assert path.read_bytes() == b"a"
    assert sorted(p.name for p in path.parent.iterdir()) == ["Busy.cs"]  # временный файл убран
