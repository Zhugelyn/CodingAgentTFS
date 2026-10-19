"""Профили репозиториев (S15): загрузка, выбор по тикету, привязка к прогону."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ario.config import ConfigError, Settings
from ario.errors import GuardViolation
from ario.guardrails import Guardrails
from ario.orchestrator import Orchestrator, Stage, StageContext, TicketNotTakenError
from ario.repos import LEGACY_PROFILE, RepoProfile, RepoRegistry, RepoSelectionError
from ario.state import StateStore
from ario.ticket import Ticket

REPOS_YAML = """\
default: rx
repos:
  rx:
    path: rx-clone
    base_branch: master
    branch_template: antares/ai-{id}
    build_command: ""
    knowledge_dir: .
    write_deny_extra: ["*.generated"]
    match:
      area_path: ["SmartInstruments\\\\Antares"]
  web:
    path: {web}
    base_branch: main
    knowledge_dir: knowledge/web
    match:
      area_path: ["SmartInstruments\\\\Web"]
      tags: ["web-ui"]
"""


def ticket(area: str | None = "SmartInstruments\\Antares", tags: list[str] | None = None) -> Ticket:
    return Ticket(id="1", type="Bug", title="t", area=area, tags=tags or [], description="d")


def write_repos(tmp_path: Path, text: str | None = None) -> Path:
    path = tmp_path / "config" / "repos.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        text or REPOS_YAML.replace("{web}", (tmp_path / "web-clone").as_posix()), encoding="utf-8"
    )
    return path


# --- загрузка -----------------------------------------------------------------------------------
def test_load_resolves_paths_and_defaults(tmp_path: Path) -> None:
    registry = RepoRegistry.load(write_repos(tmp_path), project_dir=tmp_path / "proj")
    rx = registry.get("rx")
    assert rx.path == (tmp_path / "config" / "rx-clone").resolve()  # относительно repos.yaml
    assert rx.branch_template == "antares/ai-{id}" and rx.write_deny_extra == ("*.generated",)
    assert rx.knowledge_dir == (tmp_path / "proj").resolve()
    web = registry.get("web")
    assert web.base_branch == "main" and web.build_command == ()
    assert web.knowledge_dir == (tmp_path / "proj" / "knowledge" / "web").resolve()
    assert registry.default == rx and registry.names == ["rx", "web"]


def test_single_profile_becomes_default(tmp_path: Path) -> None:
    path = write_repos(tmp_path, "repos:\n  only:\n    path: /srv/only\n")
    assert RepoRegistry.load(path, tmp_path).default is not None


@pytest.mark.parametrize(
    "text",
    [
        "repos:\n  a:\n    path: /x\n    unknown_key: 1\n",  # опечатка в ключе
        "default: nope\nrepos:\n  a:\n    path: /x\n",  # default без профиля
        "repos:\n  a:\n    path: ''\n",  # пустой путь
        "repos:\n  a:\n    path: /x\n    branch_template: 'без-id'\n",  # шаблон без {id}
        "это: [не, схема",  # битый yaml
    ],
)
def test_bad_repos_file_is_a_config_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        RepoRegistry.load(write_repos(tmp_path, text), tmp_path)


def test_missing_repos_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        RepoRegistry.load(tmp_path / "нет.yaml", tmp_path)


# --- выбор --------------------------------------------------------------------------------------
@pytest.fixture
def registry(tmp_path: Path) -> RepoRegistry:
    return RepoRegistry.load(write_repos(tmp_path), tmp_path)


def names(registry: RepoRegistry, ticket_: Ticket, override: str | None = None) -> list[str]:
    return [p.name for p in registry.candidates(ticket_, override).profiles]


def test_candidates_by_area_path_prefix_and_tag(registry: RepoRegistry) -> None:
    assert names(registry, ticket("SmartInstruments\\Antares")) == ["rx"]
    assert names(registry, ticket("smartinstruments\\antares\\Sub")) == ["rx"]
    assert names(registry, ticket("SmartInstruments\\Web\\Ui")) == ["web"]
    assert names(registry, ticket("Other", tags=["Web-UI"])) == ["web"]
    assert registry.candidates(ticket("SmartInstruments\\Antares")).source == "match"


def test_area_prefix_must_end_on_a_path_segment(registry: RepoRegistry) -> None:
    other = registry.candidates(ticket("SmartInstruments\\Antares2"))  # не Antares: идёт default
    assert other.profiles == (registry.default,) and other.source == "default"


def test_no_match_without_default_goes_to_classification(tmp_path: Path) -> None:
    registry = RepoRegistry.load(write_repos(tmp_path), tmp_path)
    no_default = RepoRegistry({"a": registry.get("web"), "b": registry.get("rx")})
    found = no_default.candidates(ticket("Elsewhere"))
    assert [p.name for p in found.profiles] == ["web", "rx"] and found.source == "unmatched"
    assert "default не задан" in found.refusal


def test_ambiguous_match_goes_to_classification_among_the_matches(
    registry: RepoRegistry,
) -> None:
    found = registry.candidates(ticket("SmartInstruments\\Antares", tags=["web-ui"]))
    assert [p.name for p in found.profiles] == ["rx", "web"] and found.source == "ambiguous"
    assert "rx, web" in found.refusal


def test_override_wins_and_unknown_override_is_refused(registry: RepoRegistry) -> None:
    found = registry.candidates(ticket("SmartInstruments\\Antares"), override="web")
    assert [p.name for p in found.profiles] == ["web"] and found.source == "override"
    with pytest.raises(RepoSelectionError, match="не описан"):
        registry.candidates(ticket(), override="нет")


def test_a_single_repository_needs_no_rules(tmp_path: Path) -> None:
    only = RepoRegistry({"a": RepoRegistry.load(write_repos(tmp_path), tmp_path).get("web")})
    found = only.candidates(ticket("Anywhere"))
    assert [p.name for p in found.profiles] == ["web"] and found.source == "single"


def test_empty_registry_selects_nothing() -> None:
    assert RepoRegistry({}).candidates(ticket()).profiles == ()


# --- профиль из .env ------------------------------------------------------------------------------
def test_without_repos_file_profile_rx_comes_from_settings(settings: Settings) -> None:
    legacy = replace(
        settings,
        rx_path=Path("/srv/rx"),
        base_branch="dev",
        branch_template="ai/x-{id}",
        build_command=("do.bat", "dirx", "build"),
    )
    registry = RepoRegistry.from_settings(legacy)
    profile = registry.get(LEGACY_PROFILE)
    assert (profile.path, profile.base_branch, profile.branch_template) == (
        Path("/srv/rx"),
        "dev",
        "ai/x-{id}",
    )
    assert profile.build_command == ("do.bat", "dirx", "build")
    assert profile.knowledge_dir == legacy.project_dir
    assert registry.candidates(ticket("Anything")).profiles == (profile,)  # единственный


def test_no_rx_path_and_no_repos_file_means_no_repo(settings: Settings) -> None:
    assert RepoRegistry.from_settings(settings).candidates(ticket()).profiles == ()


def test_repos_file_takes_precedence_over_env(settings: Settings, tmp_path: Path) -> None:
    configured = replace(settings, rx_path=Path("/srv/rx"), repos_path=write_repos(tmp_path))
    assert RepoRegistry.from_settings(configured).names == ["rx", "web"]


# --- границы путей профиля -----------------------------------------------------------------------
def test_profile_extra_write_deny_is_added_to_the_common_rules(tmp_path: Path) -> None:
    guardrails = Guardrails.load(Path(__file__).resolve().parents[1] / "config" / "guardrails.yaml")
    guardrails.config.paths.write_allow.clear()  # здесь проверяется только запрет
    target = tmp_path / "A.generated"
    target.write_text("x", encoding="utf-8")
    plain = guardrails.path_guard(tmp_path)
    assert plain.check("A.generated", "write") == target.resolve()
    strict = guardrails.path_guard(tmp_path, ("*.generated",))
    with pytest.raises(GuardViolation):
        strict.check("A.generated", "write")
    with pytest.raises(GuardViolation):  # общие правила остаются
        strict.check("obj/A.cs", "write")


# --- оркестратор ---------------------------------------------------------------------------------
@pytest.fixture
def configured(settings: Settings, tmp_path: Path) -> Settings:
    return replace(settings, repos_path=write_repos(tmp_path))


def run_ticket(
    configured: Settings, repo_override: str | None = None
) -> tuple[list[RepoProfile | None], str]:
    seen: list[RepoProfile | None] = []

    def stage(ctx: StageContext) -> None:
        seen.append(ctx.repo)

    outcome = Orchestrator(
        configured, [Stage("s", stage)], preflight=lambda: None, repo_override=repo_override
    ).run("43031")
    return seen, outcome


def test_ticket_is_routed_to_its_repo_and_the_choice_is_saved(
    configured: Settings, inbox_ticket: str
) -> None:
    seen, outcome = run_ticket(configured)
    assert outcome == "done" and [p.name for p in seen if p] == ["rx"]  # area Antares
    state = StateStore(configured.runs_dir).load("43031")
    assert state is not None and state.repo == "rx"


def test_repo_override_goes_to_the_stage_and_the_state(
    configured: Settings, inbox_ticket: str
) -> None:
    seen, _ = run_ticket(configured, repo_override="web")
    assert [p.name for p in seen if p] == ["web"]
    state = StateStore(configured.runs_dir).load("43031")
    assert state is not None and state.repo == "web"


def test_unknown_override_leaves_the_ticket_in_inbox(
    configured: Settings, inbox_ticket: str
) -> None:
    with pytest.raises(TicketNotTakenError, match="не описан"):
        run_ticket(configured, repo_override="нет")
    assert (configured.tickets_dir / "inbox" / "43031.md").exists()


def test_ambiguous_ticket_is_not_taken(
    configured: Settings, inbox_ticket: str, tmp_path: Path
) -> None:
    path = configured.tickets_dir / "inbox" / "43031.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("tags: [Applied]", "tags: [web-ui]"),
        encoding="utf-8",
    )
    with pytest.raises(TicketNotTakenError, match="нескольким"):
        run_ticket(configured)
    comments = (configured.tickets_dir / "inbox" / "43031.comments.md").read_text(encoding="utf-8")
    assert "not_taken" in comments


def test_resume_keeps_the_saved_repo_even_if_rules_change(
    configured: Settings, inbox_ticket: str
) -> None:
    def failing(ctx: StageContext) -> None:
        from ario.errors import StageFailed

        raise StageFailed("упал")

    first = Orchestrator(
        configured, [Stage("s", failing)], preflight=lambda: None, repo_override="web"
    )
    assert first.run("43031") == "needs-human"
    seen: list[RepoProfile | None] = []
    second = Orchestrator(
        configured, [Stage("s", lambda c: seen.append(c.repo))], preflight=lambda: None
    )  # без override: правила выбрали бы rx, но прогон начат на web
    assert second.reopen("43031") == "done"
    assert [p.name for p in seen if p] == ["web"]


def test_profile_removed_during_the_run_sends_ticket_to_a_human(
    configured: Settings, inbox_ticket: str, tmp_path: Path
) -> None:
    from ario.errors import StageFailed

    def failing(ctx: StageContext) -> None:
        raise StageFailed("упал")

    assert (
        Orchestrator(
            configured, [Stage("s", failing)], preflight=lambda: None, repo_override="web"
        ).run("43031")
        == "needs-human"
    )
    write_repos(tmp_path, "repos:\n  rx:\n    path: /srv/rx\n")  # web убрали из repos.yaml
    again = Orchestrator(configured, [Stage("s", lambda c: None)], preflight=lambda: None)
    assert again.reopen("43031") == "needs-human"
    comments = (configured.tickets_dir / "needs-human" / "43031.comments.md").read_text(
        encoding="utf-8"
    )
    assert "web" in comments and "не описан" in comments


# --- исправления по ревью -----------------------------------------------------------------------
def test_repos_file_without_repositories_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        RepoRegistry.load(write_repos(tmp_path, "repos: {}\n"), tmp_path)


def test_repos_file_is_only_taken_from_the_explicit_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repos.yaml").write_text(REPOS_YAML, encoding="utf-8")
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARIO_REPOS", raising=False)
    assert Settings.load(empty).repos_path is None  # лежит рядом, но не включён
    monkeypatch.setenv("ARIO_REPOS", str(tmp_path / "config" / "repos.yaml"))
    assert Settings.load(empty).repos_path == (tmp_path / "config" / "repos.yaml").resolve()


def test_relative_knowledge_dir_is_taken_from_the_project_dir(tmp_path: Path) -> None:
    registry = RepoRegistry.load(write_repos(tmp_path), project_dir=tmp_path / "proj")
    assert registry.get("web").knowledge_dir == (tmp_path / "proj" / "knowledge" / "web").resolve()


def test_profile_is_saved_before_the_ticket_leaves_inbox(
    configured: Settings, inbox_ticket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ario.store import TicketStore

    def killed(self: TicketStore, *args: object) -> None:
        raise KeyboardInterrupt  # процесс убит между выбором профиля и переносом тикета

    monkeypatch.setattr(TicketStore, "move", killed)
    with pytest.raises(KeyboardInterrupt):
        run_ticket(configured, repo_override="web")
    state = StateStore(configured.runs_dir).load("43031")
    assert state is not None and state.repo == "web"


def test_stage_without_a_repo_names_both_ways_to_configure_it(
    settings: Settings, inbox_ticket: str
) -> None:
    from ario.agents.analyst import make_analyst_stage
    from ario.agents.backend import AgentRequest, AgentResult

    class NoBackend:
        def run(self, request: AgentRequest) -> AgentResult:
            raise AssertionError("агент не должен запускаться")

    Orchestrator(
        settings,
        [make_analyst_stage(NoBackend())],
        preflight=lambda: None,
    ).run(inbox_ticket)
    comments = (settings.tickets_dir / "needs-human" / "43031.comments.md").read_text("utf-8")
    assert "RX_PATH" in comments and "ARIO_REPOS" in comments


def test_developer_stage_takes_everything_from_the_profile(
    settings: Settings, tmp_path: Path
) -> None:
    import json

    from ario.agents.developer import make_developer_stage
    from tests.test_developer_stage import ANALYSIS, F_CS, FakeDotnet, ScriptedBackend
    from tests.test_tools_git import git

    web = tmp_path / "web"
    (web / "src" / "Mod").mkdir(parents=True)
    (web / "src" / "Mod" / "F.cs").write_bytes(F_CS.encode())
    (web / "src" / "Mod" / "Mod.csproj").write_text("<Project/>", encoding="utf-8")
    (web / "src" / "Mod" / "notes.locked").write_text("x", encoding="utf-8")
    git(web, "init", "-b", "main")
    git(web, "add", ".")
    git(web, "commit", "-m", "init")
    knowledge = tmp_path / "web-knowledge"
    (knowledge / "agents" / "developer").mkdir(parents=True)
    (knowledge / "AGENTS.md").write_text("ЗНАНИЯ WEB", encoding="utf-8")
    (knowledge / "agents" / "developer" / "AGENTS.md").write_text("РОЛЬ WEB", encoding="utf-8")
    (knowledge / "agents" / "developer" / "tools.yaml").write_text(
        "tools: [edit_file, build]\n", encoding="utf-8"
    )
    profile = RepoProfile(
        name="web",
        path=web,
        base_branch="main",
        branch_template="web/{id}",
        knowledge_dir=knowledge,
        write_deny_extra=("*.locked",),
    )
    # настройки .env указывают на другой репозиторий и другие знания: профиль главнее
    other = replace(settings, rx_path=tmp_path / "other", project_dir=tmp_path / "nowhere")
    denied: list[str] = []

    def script(toolset: object) -> str:
        from ario.agents.dev_tools import DeveloperToolset

        assert isinstance(toolset, DeveloperToolset)
        toolset.edit("src/Mod/F.cs", "int X = 1;", "int X = 2;")
        try:
            toolset.edit("src/Mod/notes.locked", "x", "y")
        except GuardViolation as e:
            denied.append(str(e))
        toolset.build("src/Mod/F.cs")
        return "готово"

    inbox = other.tickets_dir / "inbox"
    inbox.mkdir(parents=True)
    from tests.conftest import REPO_ROOT, TICKET_TEXT

    (inbox / "43031.md").write_text(TICKET_TEXT, encoding="utf-8")
    run_dir = other.runs_dir / "43031"
    run_dir.mkdir(parents=True)
    (run_dir / "analysis.json").write_text(json.dumps(ANALYSIS, ensure_ascii=False), "utf-8")

    backend = ScriptedBackend(script)
    outcome = Orchestrator(
        other,
        [make_developer_stage(backend, runner=FakeDotnet())],
        Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml"),
        preflight=lambda: None,
        repos=RepoRegistry({"web": profile}, "web"),
    ).run("43031")
    assert outcome == "done"
    assert git(web, "branch", "--show-current").strip() == "web/43031"  # ветка и база из профиля
    (request,) = backend.requests
    assert "ЗНАНИЯ WEB" in request.instructions and "РОЛЬ WEB" in request.instructions
    assert request.root == web and len(denied) == 1  # дополнительный запрет профиля сработал
