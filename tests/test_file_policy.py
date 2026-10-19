"""Политика файлов (S24): `hide`, правила репозитория, `.env`, секреты в правках."""

from __future__ import annotations

from pathlib import Path

import pytest

from ario.agents.dev_tools import DeveloperToolset
from ario.agents.readonly_tools import (
    FindFilesAction,
    FindFilesExecutor,
    SearchTextAction,
    SearchTextExecutor,
)
from ario.board import BoardConfig
from ario.errors import GuardViolation
from ario.guardrails import FileRules, Guardrails
from ario.repos import RepoRegistry
from ario.tools.build import BuildTool
from ario.tools.edit import EditError, added_text
from ario.tools.git import GitRepo
from ario.trace import Masker, TraceWriter
from tests.conftest import REPO_ROOT

GUARDRAILS = Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml")
SECRET_LINE = 'private const string Password = "Xk93jf02kd8";'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    files = {
        "src/Module.cs": "class Module\n{\n    int Foo;\n}\n",
        "src/Module.g.cs": "// generated Foo\n",
        "src/Other.cs": f"class Other\n{{\n    {SECRET_LINE}\n}}\n",
        ".env": "TOKEN=live-token-123456\n",
        ".env.production": "TOKEN=prod-token-123456\n",
        "config/.env": "TOKEN=nested-token-123456\n",
        "keys/server.pem": "-----BEGIN PRIVATE KEY-----\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def find(repo: Path, guard_rules: FileRules | None, pattern: str) -> str:
    guard = GUARDRAILS.path_guard(repo, guard_rules or ())
    return FindFilesExecutor(repo, guard)(FindFilesAction(pattern=pattern)).text


def search(repo: Path, guard_rules: FileRules | None, **kwargs: object) -> str:
    guard = GUARDRAILS.path_guard(repo, guard_rules or ())
    return SearchTextExecutor(repo, guard)(SearchTextAction(**kwargs)).text  # type: ignore[arg-type]


# --- hide: сгенерированные файлы ----------------------------------------------------------------
def test_generated_files_are_hidden_from_search_but_readable_by_path(repo: Path) -> None:
    assert "Module.g.cs" not in find(repo, None, "*.cs")
    assert "Module.cs" in find(repo, None, "*.cs")
    assert "Module.g.cs" not in search(repo, None, pattern="Foo")
    assert "src/Module.cs:3" in search(repo, None, pattern="Foo")
    guard = GUARDRAILS.path_guard(repo)
    assert guard.check("src/Module.g.cs", "read").is_file()  # явный путь доступен
    with pytest.raises(GuardViolation):
        guard.check("src/Module.g.cs", "write")  # править нельзя
    assert "generated Foo" in search(repo, None, pattern="Foo", path="src/Module.g.cs")


# --- правила репозитория ------------------------------------------------------------------------
def test_repository_rules_extend_the_global_ones_and_never_loosen_them(repo: Path) -> None:
    (repo / "x.secret").write_text("s", encoding="utf-8")
    (repo / "x.gen").write_text("g Foo", encoding="utf-8")
    (repo / "x.locked").write_text("l", encoding="utf-8")
    rules = FileRules(write_deny=["*.locked"], read_deny=["*.secret"], hide=["*.gen"])
    guard = GUARDRAILS.path_guard(repo, rules)
    with pytest.raises(GuardViolation):
        guard.check("x.secret", "read")
    with pytest.raises(GuardViolation):
        guard.check("x.locked", "write")
    assert guard.check("x.gen", "read").is_file()  # скрыт из поиска, но читается по пути
    assert "x.gen" not in find(repo, rules, "*")
    assert "x.secret" not in find(repo, rules, "*")
    # общие правила остаются, как бы ни были заданы правила репозитория
    with pytest.raises(GuardViolation):
        guard.check(".env", "read")
    with pytest.raises(GuardViolation):
        guard.check("src/Module.g.cs", "write")
    empty = GUARDRAILS.path_guard(repo, FileRules())
    with pytest.raises(GuardViolation):
        empty.check(".env", "write")


def test_legacy_sequence_means_extra_write_denies(repo: Path) -> None:
    (repo / "a.locked").write_text("x", encoding="utf-8")
    with pytest.raises(GuardViolation):
        GUARDRAILS.path_guard(repo, ("*.locked",)).check("a.locked", "write")


REPOS_YAML = """\
repos:
  rx:
    path: rx-clone
    write_deny_extra: ["*.old"]
    files:
      write_deny: ["*.locked"]
      read_deny: ["*.secret"]
      hide: ["*.gen"]
"""


def test_profile_rules_come_from_repos_yaml_and_from_the_board_file(tmp_path: Path) -> None:
    repos = tmp_path / "repos.yaml"
    repos.write_text(REPOS_YAML, encoding="utf-8")
    rx = RepoRegistry.load(repos, tmp_path).get("rx")
    assert rx.rules.write_deny == ["*.old", "*.locked"]
    assert rx.rules.read_deny == ["*.secret"] and rx.rules.hide == ["*.gen"]
    board = tmp_path / "board.yaml"
    board.write_text(
        "board: {url: 'https://tfs.example.test/c', project: P}\n"
        "repositories:\n  rx:\n    path: rx\n    files: {hide: ['*.gen']}\n",
        encoding="utf-8",
    )
    assert BoardConfig.load(board).registry(tmp_path).get("rx").rules.hide == ["*.gen"]
    bad = tmp_path / "bad.yaml"
    bad.write_text("repos:\n  rx:\n    path: rx\n    files: {hide_it: ['*.gen']}\n")
    from ario.config import ConfigError

    with pytest.raises(ConfigError):
        RepoRegistry.load(bad, tmp_path)


# --- .env и ключи -------------------------------------------------------------------------------
@pytest.mark.parametrize("name", [".env", ".env.production", "config/.env", "keys/server.pem"])
def test_env_and_key_files_cannot_be_read_written_or_found(repo: Path, name: str) -> None:
    guard = GUARDRAILS.path_guard(repo)
    for mode in ("read", "write"):
        with pytest.raises(GuardViolation):
            guard.check(name, mode)
    assert Path(name).name not in find(repo, None, "**/*")
    assert "token-123456" not in search(repo, None, pattern="token")
    assert not guard.visible(repo / name)


# --- секреты в правках --------------------------------------------------------------------------
def toolset(repo: Path, tmp_path: Path, masker: Masker) -> DeveloperToolset:
    guard = GUARDRAILS.path_guard(repo)
    env = GUARDRAILS.config.env
    return DeveloperToolset(
        repo,
        guard,
        GitRepo(repo, env),
        BuildTool(repo, guard, env),
        TraceWriter(tmp_path / "traces", "1", "t"),
        "development",
        has_secret=masker.contains_secret,
    )


def test_an_edit_that_adds_a_secret_is_refused_and_the_file_is_untouched(
    repo: Path, tmp_path: Path
) -> None:
    masker = Masker(["live-token-from-env-987654"])
    tools = toolset(repo, tmp_path / "t", masker)
    target = repo / "src" / "Module.cs"
    before = target.read_text(encoding="utf-8")
    for added in (
        'const string Key = "live-token-from-env-987654";',
        'const string Password = "Zq81nd04xk2";',
        "-----BEGIN RSA PRIVATE KEY-----",
        "Authorization: Bearer abcdef0123456789abcdef",
    ):
        with pytest.raises(EditError, match="секрет"):
            tools.edit("src/Module.cs", "    int Foo;", f"    int Foo;\n    {added}")
    assert target.read_text(encoding="utf-8") == before
    events = (tmp_path / "t" / "traces" / "1" / "events.jsonl").read_text(encoding="utf-8")
    assert events.count('"secret_blocked"') == 4
    assert "live-token-from-env-987654" not in events and "Zq81nd04xk2" not in events


def test_normal_edits_and_untouched_secret_like_lines_pass(repo: Path, tmp_path: Path) -> None:
    tools = toolset(repo, tmp_path / "t", Masker(["live-token-from-env-987654"]))
    out = tools.edit(
        "src/Module.cs", "    int Foo;", "    int Foo = 2;\n    // risk-management-process-and-plan"
    )
    assert "изменён" in out
    # строка с секретоподобным значением уже была в файле и правкой не добавляется
    other = "    " + SECRET_LINE
    assert "изменён" in tools.edit("src/Other.cs", other, other + "\n    int N;")


def test_added_text_is_only_the_new_lines() -> None:
    assert added_text("a\nb\n", "a\nc\nb\nd\n") == "c\nd"
    assert added_text("a", "a") == ""


def test_masker_detects_secrets_without_false_alarms() -> None:
    masker = Masker(["live-token-from-env-987654"])
    assert masker.contains_secret("x = live-token-from-env-987654;")
    assert masker.contains_secret('password = "Zq81nd04xk2"')
    assert masker.contains_secret("sk-abcdef0123456789abcdef")
    assert masker.contains_secret("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert not masker.contains_secret("var token = CancellationToken.None;")
    assert not masker.contains_secret("// risk-management-process-and-mitigation-plan")
    assert not masker.contains_secret("password = GetPassword();")


# --- белый список записи (S26) ------------------------------------------------------------------
def write_ok(guard: object, name: str) -> bool:
    try:
        guard.check(name, "write")  # type: ignore[attr-defined]
    except GuardViolation:
        return False
    return True


def test_only_listed_file_types_can_be_written_by_default(repo: Path) -> None:
    guard = GUARDRAILS.path_guard(repo)
    for name in ("src/A.cs", "src/A.resx", "src/Module.mtd", "src/Settings/x.json", "SRC/B.CS"):
        assert write_ok(guard, name), name
    # то, что сборка или окружение могут исполнить, и всё прочее: запрещено по умолчанию
    for name in (
        "src/Generator/gen.py",
        "src/Components/app.js",
        "src/App.config",
        "src/query.sql",
        "env/ci.yml",
        "tools/tool.exe",
        "Makefile",
        "README",
    ):
        assert not write_ok(guard, name), name
    with pytest.raises(GuardViolation, match="не из списка разрешённых"):
        guard.check("src/Generator/gen.py", "write")


def test_deny_rules_still_win_over_the_allow_list(repo: Path) -> None:
    guard = GUARDRAILS.path_guard(repo)
    for name in ("src/Module.g.cs", "package.json", "src/Components/package-lock.json", ".env"):
        assert not write_ok(guard, name), name
    with pytest.raises(GuardViolation, match="запрещено правилом"):
        guard.check("package.json", "write")


def test_reading_is_not_limited_by_the_allow_list(repo: Path) -> None:
    (repo / "tool.py").write_text("print(1)", encoding="utf-8")
    guard = GUARDRAILS.path_guard(repo)
    assert guard.check("tool.py", "read").is_file()
    assert "tool.py" in find(repo, None, "*.py")


def test_repository_allow_list_narrows_the_global_one_and_never_widens_it(repo: Path) -> None:
    rules = FileRules(write_allow=["src/Packages/*.cs", "src/Packages/*.json", "*.py"])
    guard = GUARDRAILS.path_guard(repo, rules)
    assert write_ok(guard, "src/Packages/Mod/Server/A.cs")
    assert write_ok(guard, "SRC/packages/Mod/Settings/x.JSON")  # регистр не важен
    assert not write_ok(guard, "src/Generator/A.cs")  # вне разрешённой папки
    assert not write_ok(guard, "src/Packages/Mod/A.resx")  # общий список разрешает, репозиторий нет
    assert not write_ok(guard, "src/Packages/Mod/gen.py")  # репозиторий разрешил бы, общий нет
    assert not write_ok(guard, "src/Packages/Mod/App.g.cs")  # запреты по-прежнему главные


def test_empty_lists_mean_no_restriction(repo: Path) -> None:
    open_guardrails = Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml")
    open_guardrails.config.paths.write_allow.clear()
    assert write_ok(open_guardrails.path_guard(repo), "tools/gen.py")
    only_repo = open_guardrails.path_guard(repo, FileRules(write_allow=["*.cs"]))
    assert write_ok(only_repo, "src/A.cs") and not write_ok(only_repo, "tools/gen.py")


def test_the_editor_refuses_files_outside_the_allow_list(repo: Path, tmp_path: Path) -> None:
    (repo / "tools").mkdir()
    (repo / "tools" / "gen.py").write_text("x = 1\n", encoding="utf-8")
    tools = toolset(repo, tmp_path / "t", Masker([]))
    with pytest.raises((EditError, GuardViolation), match="не из списка разрешённых"):
        tools.edit("tools/gen.py", "x = 1", "import os; os.system('calc')")
    assert (repo / "tools" / "gen.py").read_text(encoding="utf-8") == "x = 1\n"


def test_allow_list_comes_from_the_profile_and_the_default_config(tmp_path: Path) -> None:
    assert GUARDRAILS.config.paths.write_allow == ["*.cs", "*.resx", "*.mtd", "*.json"]
    repos = tmp_path / "repos.yaml"
    repos.write_text(
        "repos:\n  rx:\n    path: rx\n    files:\n      write_allow: ['src/Packages/*.cs']\n",
        encoding="utf-8",
    )
    assert RepoRegistry.load(repos, tmp_path).get("rx").rules.write_allow == ["src/Packages/*.cs"]
