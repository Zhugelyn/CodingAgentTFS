from __future__ import annotations

from pathlib import Path

import pytest

from ario.agents.readonly_tools import (
    FindFilesAction,
    FindFilesExecutor,
    ReadFileAction,
    ReadFileExecutor,
    SearchTextAction,
    SearchTextExecutor,
    glob_to_regex,
)
from ario.guardrails import PathGuard
from tests.test_guardrails import CONFIG


@pytest.fixture
def rx(tmp_path: Path) -> Path:
    files = {
        "src/Mod/Server/ModuleFunctions.cs": "class A\r\n{\r\n    void Foo() {}\r\n}\r\n",
        "src/Mod/Shared/Module.mtd": '{ "Name": "Mod" }\n',
        "src/Mod/Server/Other.g.cs": "// generated Foo\n",
        "src/Mod/Server/bin/Debug/x.dll.txt": "Foo in bin\n",
        "src/Mod/Server/obj/y.cs": "Foo in obj\n",
        ".git/config": "[core] Foo\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    (tmp_path / "src/Mod/Server/bom.cs").write_bytes(b"\xef\xbb\xbfclass Bom {}\n")
    (tmp_path / "src/Mod/Server/blob.bin").write_bytes(b"\x00\x01\x02Foo")
    return tmp_path


@pytest.fixture
def guard(rx: Path) -> PathGuard:
    return PathGuard(rx, CONFIG.paths)


def find(rx: Path, guard: PathGuard, pattern: str, path: str | None = None) -> str:
    return FindFilesExecutor(rx, guard)(FindFilesAction(pattern=pattern, path=path)).text


def search(rx: Path, guard: PathGuard, **kwargs: object) -> str:
    return SearchTextExecutor(rx, guard)(SearchTextAction(**kwargs)).text  # type: ignore[arg-type]


def read(rx: Path, guard: PathGuard, **kwargs: object) -> tuple[str, bool]:
    obs = ReadFileExecutor(rx, guard)(ReadFileAction(**kwargs))  # type: ignore[arg-type]
    return obs.text, obs.is_error


# --- glob ---
@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/*.cs", "a/b/c.cs", True),
        ("**/*.cs", "c.cs", True),
        ("*.cs", "a/c.cs", False),  # `*` не пересекает `/`
        ("src/**/Module.mtd", "src/Mod/Shared/Module.mtd", True),
        ("*.{cs,mtd}", "x.mtd", True),
        ("file?.cs", "file1.cs", True),
        ("file?.cs", "file10.cs", False),
        ("**/*.CS", "a/c.cs", True),  # без учёта регистра
    ],
)
def test_glob_to_regex(pattern: str, path: str, expected: bool) -> None:
    assert bool(glob_to_regex(pattern).match(path)) is expected


# --- find_files ---
def test_find_by_name_skips_bin_obj_git(rx: Path, guard: PathGuard) -> None:
    out = find(rx, guard, "*.cs").splitlines()
    assert "src/Mod/Server/ModuleFunctions.cs" in out
    assert "src/Mod/Server/Other.g.cs" in out  # читать *.g.cs можно, писать нельзя
    assert not any("/bin/" in p or "/obj/" in p or p.startswith(".git") for p in out)


def test_find_in_subfolder_returns_paths_relative_to_root(rx: Path, guard: PathGuard) -> None:
    out = find(rx, guard, "**/*.mtd", path="src/Mod")
    assert out == "src/Mod/Shared/Module.mtd"


def test_find_nothing_and_errors(rx: Path, guard: PathGuard) -> None:
    assert find(rx, guard, "*.xyz") == "Ничего не найдено."
    assert "вне репозитория" in find(rx, guard, "*.cs", path="..")
    assert "Не папка" in find(rx, guard, "*.cs", path="src/Mod/Shared/Module.mtd")


def test_find_caps_results(rx: Path, guard: PathGuard) -> None:
    many = rx / "many"
    many.mkdir()
    for i in range(130):
        (many / f"f{i}.txt").write_text("x", encoding="utf-8")
    out = find(rx, guard, "*.txt", path="many")
    assert "первые 100 из 130" in out
    assert len(out.splitlines()) == 101


# --- search_text ---
def test_search_finds_with_line_numbers_and_skips_bin_obj_binary(
    rx: Path, guard: PathGuard
) -> None:
    out = search(rx, guard, pattern="Foo").splitlines()
    assert "src/Mod/Server/ModuleFunctions.cs:3: void Foo() {}" in out
    assert "src/Mod/Server/Other.g.cs:1: // generated Foo" in out
    assert not any("bin" in line.split(":")[0].split("/") for line in out)
    assert not any(".git/" in line or "blob.bin" in line for line in out)


def test_search_include_path_and_case(rx: Path, guard: PathGuard) -> None:
    only_cs = search(rx, guard, pattern="foo", include="*.cs", ignore_case=True)
    assert "ModuleFunctions.cs:3" in only_cs
    assert "Module.mtd" not in only_cs
    assert search(rx, guard, pattern="Name", path="src/Mod/Shared").startswith(
        "src/Mod/Shared/Module.mtd:1"
    )
    assert search(rx, guard, pattern="foo") == "Совпадений нет."  # регистр учитывается


def test_search_bad_regex_and_outside_path(rx: Path, guard: PathGuard) -> None:
    assert "Некорректное регулярное выражение" in search(rx, guard, pattern="(")
    assert "вне репозитория" in search(rx, guard, pattern="x", path="../..")


def test_search_caps_matches(rx: Path, guard: PathGuard) -> None:
    (rx / "big.txt").write_text("hit\n" * 80, encoding="utf-8")
    out = search(rx, guard, pattern="hit", path="big.txt")
    assert "первые 50 совпадений" in out
    assert len(out.splitlines()) == 51


# --- read_file ---
def test_read_numbers_lines_and_handles_crlf(rx: Path, guard: PathGuard) -> None:
    text, is_error = read(rx, guard, path="src/Mod/Server/ModuleFunctions.cs")
    assert not is_error
    assert text.splitlines()[:3] == ["1\tclass A", "2\t{", "3\t    void Foo() {}"]
    assert "[строки 1-4 из 4]" in text
    assert "\r" not in text


def test_read_offset_limit_and_continuation(rx: Path, guard: PathGuard) -> None:
    (rx / "long.txt").write_text("\n".join(f"line{i}" for i in range(1, 501)), encoding="utf-8")
    text, _ = read(rx, guard, path="long.txt", offset=10, limit=5)
    assert text.splitlines()[0] == "10\tline10"
    assert "[строки 10-14 из 500] Для продолжения: offset=15." in text
    past, _ = read(rx, guard, path="long.txt", offset=900)
    assert "за его пределами" in past


def test_read_strips_bom(rx: Path, guard: PathGuard) -> None:
    text, _ = read(rx, guard, path="src/Mod/Server/bom.cs")
    assert text.startswith("1\tclass Bom {}")


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../outside.txt", "вне репозитория"),
        (".git/config", "запрещено"),
        ("src/Mod/Server/blob.bin", "Бинарный"),
        ("src/нет.cs", "не найден"),
        ("src/Mod", "не найден"),  # папка
    ],
)
def test_read_errors(rx: Path, guard: PathGuard, path: str, message: str) -> None:
    text, is_error = read(rx, guard, path=path)
    assert is_error
    assert message in text


def test_read_big_file_refused_and_long_output_capped(rx: Path, guard: PathGuard) -> None:
    (rx / "huge.txt").write_bytes(b"x" * 2_100_000)
    assert "слишком большой" in read(rx, guard, path="huge.txt")[0]
    (rx / "wide.txt").write_text(("y" * 200 + "\n") * 500, encoding="utf-8")
    text, _ = read(rx, guard, path="wide.txt", limit=500)
    assert len(text) < 21_000
    assert "Для продолжения" in text


# --- устойчивость к искажённым именам аргументов слабой модели ---
def test_mangled_argument_names_are_repaired() -> None:
    action = SearchTextAction.model_validate(
        {"pattern": "Tin", "include": "*.cs", "pathsrc": "Packages/Sungero.Parties"}
    )
    assert action.path == "Packages/Sungero.Parties"
    assert ReadFileAction.model_validate({"pathX": "a.cs"}).path == "a.cs"
    # более длинное имя поля побеждает: `ignore_casex` не превращается в другое поле
    assert SearchTextAction.model_validate({"pattern": "x", "ignore_casex": True}).ignore_case


def test_unknown_arguments_still_rejected() -> None:
    with pytest.raises(ValueError):
        SearchTextAction.model_validate({"pattern": "x", "recursive": True})
    with pytest.raises(ValueError):  # исходное поле не перезаписывается искажённым дублем
        ReadFileAction.model_validate({"path": "a.cs", "pathsrc": "b.cs"})


def test_symlink_pointing_outside_rx_is_not_searched(rx: Path, guard: PathGuard) -> None:
    secret = rx.parent / "secret.txt"
    secret.write_text("TOPSECRET Foo\n", encoding="utf-8")
    link = rx / "src" / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("символические ссылки недоступны")
    assert "TOPSECRET" not in search(rx, guard, pattern="TOPSECRET")
    assert "link.txt" not in find(rx, guard, "*.txt")
    inside = rx / "src" / "inside_link.cs"
    inside.symlink_to(rx / "src" / "Mod" / "Server" / "ModuleFunctions.cs")
    assert "inside_link.cs" in find(rx, guard, "*.cs")  # ссылки внутри RX работают


def test_mangled_sdk_meta_fields_are_dropped() -> None:
    """Реальный случай: Qwen прислал `security_risk` и ещё `security_risk_low` рядом."""
    action = ReadFileAction.model_validate(
        {"path": "a.cs", "security_risk": "LOW", "security_risk_low": "x", "summaryCheck": "y"}
    )
    assert action.path == "a.cs"
