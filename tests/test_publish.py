"""Публикация (S17): коммит, push и PR на настоящих git-репозиториях и заглушке API TFS."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from ario.config import ConfigError, Settings
from ario.guardrails import Guardrails
from ario.orchestrator import Orchestrator, StageContext
from ario.publish import (
    GitPublisher,
    make_publish_stage,
    pull_request_url,
    read_publication,
    render_pr_description,
    should_publish,
)
from ario.repos import PublishConfig, RepoProfile, RepoRegistry
from ario.tfs.client import TfsClient, TfsError
from ario.tools.proc import ProcResult, ToolError, run_process
from tests.conftest import REPO_ROOT, TICKET_TEXT
from tests.test_repos import REPOS_YAML, write_repos
from tests.test_tfs import respond
from tests.test_tools_git import git

TOKEN = "git-secret-pat-0123456789"
AUTHOR = ("Agent", "agent@example.test")


def git_utf8(repo: Path, *args: str) -> str:
    """Вывод git как UTF-8 (общий помощник читает в кодировке консоли Windows)."""
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True, encoding="utf-8"
    ).stdout


PLAN = {
    "decision": "suitable",
    "plan": [{"step": "Заменить 1 на 2", "files": ["A.cs"]}],
    "risk": "low",
    "risk_reason": "",
}


def publish_config(**overrides: Any) -> PublishConfig:
    base: dict[str, Any] = {
        "url": "https://rxtfs.example.test/Collection",
        "project": "Sungero",
        "repository": "DirectumRX",
        "target_branch": "master",
        "pat_env": "RX_GIT_PAT",
        "author_name": AUTHOR[0],
        "author_email": AUTHOR[1],
    }
    return PublishConfig(**{**base, **overrides})


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    """Рабочая копия с веткой тикета и незакоммиченной правкой, плюс bare-remote."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-b", "master")
    work = tmp_path / "rx"
    work.mkdir()
    git(work, "init", "-b", "master")
    (work / "A.cs").write_text("class A { int X = 1; }\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "origin", "master")
    git(work, "switch", "-c", "ai/43031")
    (work / "A.cs").write_text("class A { int X = 2; }\n", encoding="utf-8")
    return {"work": work, "remote": remote}


class Api:
    """Заглушка API pull request: запоминает запросы."""

    def __init__(self, existing: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.existing = existing or []
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return respond({"message": f"отказ {TOKEN}"}, 400)
        if request.method == "GET":
            return respond({"value": self.existing})
        return respond({"pullRequestId": 42})

    def factory(self) -> Callable[[PublishConfig, str], TfsClient]:
        return lambda cfg, token: TfsClient(
            cfg.url,
            cfg.project,
            token,
            kind="git",
            transport=httpx.MockTransport(self),
            retry_wait=(),
        )

    @property
    def created(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


class Recording:
    """Запускает настоящий git и запоминает команды и окружение."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(
        self, args: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> ProcResult:
        self.calls.append((list(args), dict(env)))
        return run_process(args, cwd, timeout, env)


# --- GitPublisher ------------------------------------------------------------------------------
def publisher(work: Path, runner: Recording | None = None) -> GitPublisher:
    guardrails = Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml")
    return GitPublisher(work, guardrails.config.env, runner=runner or run_process)


def test_commit_all_commits_once_and_then_has_nothing_to_commit(world: dict[str, Path]) -> None:
    pub = publisher(world["work"])
    assert pub.commit_all("42: заголовок", AUTHOR) is True
    assert pub.commit_all("ещё раз", AUTHOR) is False
    assert pub.commits_ahead("master") == 1
    assert git_utf8(world["work"], "log", "-1", "--format=%an|%ae|%s").strip() == (
        "Agent|agent@example.test|42: заголовок"
    )
    assert git(world["work"], "status", "--porcelain").strip() == ""  # всё закоммичено


def test_push_sends_the_branch_and_keeps_the_token_out_of_arguments(
    world: dict[str, Path],
) -> None:
    runner = Recording()
    pub = publisher(world["work"], runner)
    pub.commit_all("msg", AUTHOR)
    pub.push("origin", "ai/43031", TOKEN)
    assert "ai/43031" in git(world["remote"], "branch", "--list")
    pushes = [(a, e) for a, e in runner.calls if "push" in a]
    ((args, env),) = pushes
    assert "--force" not in args and "-f" not in args
    assert not any(TOKEN in a for a in args)  # токена нет в командной строке
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    others = [e for a, e in runner.calls if "push" not in a]
    assert others and not any("GIT_CONFIG_VALUE_0" in e for e in others)  # заголовок только на push


def test_push_is_idempotent_and_never_overwrites_foreign_history(
    world: dict[str, Path], tmp_path: Path
) -> None:
    pub = publisher(world["work"])
    pub.commit_all("msg", AUTHOR)
    pub.push("origin", "ai/43031", TOKEN)
    pub.push("origin", "ai/43031", TOKEN)  # повтор после сбоя безопасен
    other = tmp_path / "other"
    git(tmp_path, "clone", str(world["remote"]), str(other))
    git(other, "switch", "ai/43031")
    (other / "B.cs").write_text("чужой коммит\n", encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-m", "foreign")
    git(other, "push", "origin", "ai/43031")
    (world["work"] / "A.cs").write_text("class A { int X = 3; }\n", encoding="utf-8")
    pub.commit_all("ещё", AUTHOR)
    with pytest.raises(ToolError):  # история разошлась: force не используется
        pub.push("origin", "ai/43031", TOKEN)
    assert "foreign" in git(world["remote"], "log", "-1", "--format=%s", "ai/43031")


# --- клиент PR -----------------------------------------------------------------------------------
def test_find_and_create_pull_request_requests() -> None:
    api = Api()
    with api.factory()(publish_config(), TOKEN) as client:
        assert client.find_pull_request("DirectumRX", "ai/1", "master") is None
        pr = client.create_pull_request("DirectumRX", "ai/1", "master", "Заголовок", "Описание")
    assert pr["pullRequestId"] == 42
    find, create = api.requests
    assert find.url.params["searchCriteria.sourceRefName"] == "refs/heads/ai/1"
    assert find.url.params["searchCriteria.status"] == "active"
    assert create.url.raw_path.split(b"?")[0].endswith(
        b"/Sungero/_apis/git/repositories/DirectumRX/pullrequests"
    )
    assert json.loads(create.content) == {
        "sourceRefName": "refs/heads/ai/1",
        "targetRefName": "refs/heads/master",
        "title": "Заголовок",
        "description": "Описание",
        "isDraft": False,
    }


def test_creating_a_pull_request_is_not_retried_after_a_server_error() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return respond({}, 503)

    client = TfsClient(
        "https://x.test/c", "P", TOKEN, kind="git", transport=httpx.MockTransport(handler)
    )
    with client, pytest.raises(TfsError):
        client.create_pull_request("R", "a", "master", "t", "d")
    assert len(calls) == 1


def test_pull_request_url_quotes_names() -> None:
    cfg = publish_config(project="My Project", repository="Repo Name")
    assert pull_request_url(cfg, 7) == (
        "https://rxtfs.example.test/Collection/My%20Project/_git/Repo%20Name/pullrequest/7"
    )


# --- этап publish --------------------------------------------------------------------------------
@pytest.fixture
def run_stage(
    world: dict[str, Path], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[str, Api, Settings]]:
    def run(
        *,
        api: Api | None = None,
        config: PublishConfig | None = None,
        publish: bool = True,
        token: str | None = TOKEN,
        verdicts: tuple[str, ...] = ("approve",),
        runner: Recording | None = None,
        stage_settings: Settings | None = None,
        plan: dict[str, Any] | None = None,
        report: str = "Заменил 1 на 2 в A.cs",
        continued: bool = False,
    ) -> tuple[str, Api, Settings]:
        api = api or Api()
        config = config or publish_config()
        if token is not None:
            monkeypatch.setenv(config.pat_env, token)
        profile = RepoProfile(name="rx", path=world["work"], base_branch="master", publish=config)
        base = stage_settings or replace(settings, publish=publish)
        # continued: тикет уже в работе (доработка, возобновление), а не новая подача из inbox/
        folder = base.tickets_dir / ("in-progress" if continued else "inbox")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "43031.md").write_text(TICKET_TEXT, encoding="utf-8")
        run_dir = base.runs_dir / "43031"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "analysis.json").write_text(
            json.dumps(plan or PLAN, ensure_ascii=False), "utf-8"
        )
        (run_dir / "development-1.md").write_text(report, encoding="utf-8")
        for n, verdict in enumerate(verdicts, start=1):
            (run_dir / f"review-{n}.json").write_text(json.dumps({"verdict": verdict}), "utf-8")
        stage = make_publish_stage(runner or run_process, api.factory())
        outcome = Orchestrator(
            base,
            [replace(stage, when=should_publish)],
            Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml"),
            preflight=lambda: None,
            repos=RepoRegistry({"rx": profile}, "rx"),
        ).run("43031")
        return outcome, api, base

    return run


def reason(settings: Settings) -> str:
    return (settings.tickets_dir / "needs-human" / "43031.comments.md").read_text("utf-8")


def test_publish_commits_pushes_and_opens_a_pull_request(
    run_stage: Callable[..., tuple[str, Api, Settings]], world: dict[str, Path]
) -> None:
    outcome, api, settings = run_stage()
    assert outcome == "done"
    assert "ai/43031" in git(world["remote"], "branch", "--list")
    assert git_utf8(world["work"], "log", "-1", "--format=%s").startswith("43031: ")
    (create,) = api.created
    body = json.loads(create.content)
    assert body["sourceRefName"] == "refs/heads/ai/43031" and body["targetRefName"].endswith(
        "master"
    )
    assert body["title"].startswith("43031: ")
    assert "Заменить 1 на 2" in body["description"] and "Ревью: approve" in body["description"]
    assert TOKEN not in json.dumps(body)
    publication = read_publication(settings.runs_dir / "43031")
    assert publication is not None and publication["pr_id"] == 42
    assert publication["pr_url"].endswith("/_git/DirectumRX/pullrequest/42")
    assert publication["commit"] == git(world["work"], "rev-parse", "HEAD").strip()
    trace = (settings.traces_dir / "43031" / "events.jsonl").read_text(encoding="utf-8")
    assert '"kind": "published"' in trace and TOKEN not in trace


def test_existing_pull_request_is_reused_not_duplicated(
    run_stage: Callable[..., tuple[str, Api, Settings]], world: dict[str, Path]
) -> None:
    api = Api(existing=[{"pullRequestId": 7}])
    outcome, api, settings = run_stage(api=api)
    assert outcome == "done" and api.created == []
    publication = read_publication(settings.runs_dir / "43031")
    assert publication is not None and publication["pr_id"] == 7


def test_draft_and_target_branch_come_from_the_profile(
    run_stage: Callable[..., tuple[str, Api, Settings]],
) -> None:
    _, api, _ = run_stage(config=publish_config(draft=True, target_branch="release"))
    body = json.loads(api.created[0].content)
    assert body["isDraft"] is True and body["targetRefName"] == "refs/heads/release"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"publish": False},  # выключено ARIO_PUBLISH
        {"verdicts": ("reject",)},  # ревью отклонило
        {"verdicts": ("reject", "reject")},  # и после доработки
    ],
)
def test_publish_is_skipped_when_disabled_or_not_approved(
    run_stage: Callable[..., tuple[str, Api, Settings]],
    world: dict[str, Path],
    kwargs: dict[str, Any],
) -> None:
    outcome, api, _ = run_stage(**kwargs)
    assert outcome == "done" and api.requests == []
    assert "ai/43031" not in git(world["remote"], "branch", "--list")  # ничего не ушло


def test_approval_after_rework_publishes(
    run_stage: Callable[..., tuple[str, Api, Settings]],
) -> None:
    outcome, api, _ = run_stage(verdicts=("reject", "approve"))
    assert outcome == "done" and len(api.created) == 1


def test_missing_token_stops_the_stage_with_a_clear_reason(
    run_stage: Callable[..., tuple[str, Api, Settings]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RX_GIT_PAT", raising=False)
    outcome, api, settings = run_stage(token=None)
    assert outcome == "needs-human" and "RX_GIT_PAT" in reason(settings) and api.requests == []


def test_nothing_to_publish_is_a_refusal(
    run_stage: Callable[..., tuple[str, Api, Settings]], world: dict[str, Path]
) -> None:
    git(world["work"], "checkout", "--", "A.cs")  # правок нет, коммитов сверх базовой нет
    outcome, api, settings = run_stage()
    assert outcome == "needs-human" and "публиковать нечего" in reason(settings)
    assert api.requests == []


def test_wrong_branch_is_a_refusal(
    run_stage: Callable[..., tuple[str, Api, Settings]], world: dict[str, Path]
) -> None:
    git(world["work"], "stash")
    git(world["work"], "switch", "master")
    outcome, api, settings = run_stage()
    assert outcome == "needs-human" and "ожидалась ветка ai/43031" in reason(settings)
    assert api.requests == []


def test_api_error_is_reported_without_the_token(
    run_stage: Callable[..., tuple[str, Api, Settings]],
) -> None:
    outcome, _, settings = run_stage(api=Api(fail=True))
    text = reason(settings)
    assert outcome == "needs-human" and "Публикация не удалась" in text and TOKEN not in text


def test_rerun_after_a_failed_pr_does_not_duplicate_commit_or_push_errors(
    run_stage: Callable[..., tuple[str, Api, Settings]], world: dict[str, Path]
) -> None:
    run_stage(api=Api(fail=True))  # коммит и push прошли, PR нет
    ahead = git(world["work"], "rev-list", "--count", "master..HEAD").strip()
    assert ahead == "1"
    # повторный запуск того же этапа: пустой коммит не создаётся, push идемпотентен
    import ario.publish as module

    pub = module.GitPublisher(
        world["work"], Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml").config.env
    )
    assert pub.commit_all("повтор", AUTHOR) is False
    pub.push("origin", "ai/43031", TOKEN)
    assert git(world["work"], "rev-list", "--count", "master..HEAD").strip() == "1"


# --- описание PR и конфигурация ----------------------------------------------------------------
def test_description_has_ticket_link_plan_report_and_is_limited(
    settings: Settings, tmp_path: Path
) -> None:
    from ario.state import RunState
    from ario.ticket import parse_ticket
    from ario.trace import TraceWriter

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "analysis.json").write_text(json.dumps(PLAN, ensure_ascii=False), "utf-8")
    (run_dir / "development-1.md").write_text("я" * 10_000, encoding="utf-8")
    (run_dir / "review-1.json").write_text('{"verdict": "approve"}', encoding="utf-8")
    ticket = parse_ticket(
        TICKET_TEXT.replace("severity: 3 - Medium\n", "severity: 3 - Medium\nsource: tfs\n")
    )
    configured = replace(
        settings, tfs_url="https://tech.example.test/Portfolio", tfs_project="Smart Instruments"
    )
    ctx = StageContext(
        ticket,
        RunState(ticket_id="43031"),
        run_dir,
        TraceWriter(tmp_path / "t", "43031", "tr"),
        configured,
        Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml"),
        Guardrails.load(REPO_ROOT / "config" / "guardrails.yaml").deadline(),
        lambda *_: None,
        lambda text: text.replace(TOKEN, "***"),
    )
    text = render_pr_description(ctx)
    assert "https://tech.example.test/Portfolio/Smart%20Instruments/_workitems/edit/43031" in text
    assert "Заменить 1 на 2 (A.cs)" in text and len(text) <= 3900


def test_publish_block_is_parsed_with_defaults(tmp_path: Path) -> None:
    text = REPOS_YAML.replace("{web}", (tmp_path / "w").as_posix()) + (
        "    publish:\n"
        "      url: https://rxtfs.example.test/Collection/\n"
        "      project: Sungero\n"
        "      repository: DirectumRX\n"
    )
    # publish относится к последнему репозиторию (web): у него base_branch main
    registry = RepoRegistry.load(write_repos(tmp_path, text), tmp_path)
    assert registry.get("rx").publish is None
    cfg = registry.get("web").publish
    assert cfg is not None
    assert cfg.url == "https://rxtfs.example.test/Collection"  # без завершающего /
    assert (cfg.target_branch, cfg.pat_env, cfg.remote, cfg.draft) == (
        "main",
        "GIT_PAT",
        "origin",
        False,
    )


@pytest.mark.parametrize(
    "bad",
    [
        "      url: ftp://x\n      project: P\n      repository: R\n",
        "      url: https://x\n      project: P\n      repository: R\n      pat_env: 'bad name'\n",
        "      url: https://x\n      project: P\n      repository: R\n"
        "      commit_template: '{nope}'\n",
        "      url: https://x\n      project: P\n      repository: R\n      surprise: 1\n",
    ],
)
def test_bad_publish_block_is_a_config_error(tmp_path: Path, bad: str) -> None:
    text = f"repos:\n  a:\n    path: /x\n    publish:\n{bad}"
    with pytest.raises(ConfigError):
        RepoRegistry.load(write_repos(tmp_path, text), tmp_path)


# --- тикеты и код на разных серверах --------------------------------------------------------------
def test_clients_refuse_actions_of_the_other_server() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond({})

    transport = httpx.MockTransport(handler)
    board = TfsClient("https://tech.test/c", "P", TOKEN, transport=transport)
    code = TfsClient("https://rxtfs.test/c", "Sungero", TOKEN, kind="git", transport=transport)
    with pytest.raises(TfsError, match="git"):
        board.find_pull_request("R", "a", "master")
    with pytest.raises(TfsError, match="git"):
        board.create_pull_request("R", "a", "master", "t", "d")
    work_item_calls: list[Callable[[], object]] = [
        lambda: code.wiql("SELECT 1", 1),
        lambda: code.work_item(43031),
        lambda: code.work_item_brief(43031),
        lambda: code.comments(43031),
        lambda: code.type_states("Bug"),
        lambda: code.add_comment(43031, "x"),
        lambda: code.set_state(43031, "Development", 1),
    ]
    for call in work_item_calls:
        with pytest.raises(TfsError, match="board"):
            call()
    assert seen == []  # ни один запрос не ушёл на чужой сервер


def test_publish_uses_a_git_client_never_a_board_client() -> None:
    from ario.publish import default_client

    assert "git" in repr(default_client(publish_config(), TOKEN))


def test_work_item_references_never_reach_the_code_server(
    run_stage: Callable[..., tuple[str, Api, Settings]], world: dict[str, Path]
) -> None:
    from ario.publish import neutralize_refs

    assert neutralize_refs(
        "см. #43031, AB#7 и №5, цвет #fff"
    ) == "см. №43031, ABN7 и №5, цвет #fff".replace("ABN7", "AB№7")
    config = publish_config(commit_template="#{id}: {title}", pr_title_template="PR #{id} {title}")
    outcome, api, settings = run_stage(config=config)
    assert outcome == "done"
    (create,) = api.created
    body = json.loads(create.content)
    commit = git_utf8(world["work"], "log", "-1", "--format=%B")
    import re

    for text in (body["title"], body["description"], commit):
        assert not re.search(r"#\d", text), text  # `#123` на сервере кода — чужой work item
    assert "№43031" in body["title"]
    assert "доски" in body["description"]
    assert "workItemRefs" not in body  # PR не привязывается к work items сервера кода


def test_plan_and_report_text_with_hash_numbers_is_neutralized(
    run_stage: Callable[..., tuple[str, Api, Settings]],
) -> None:
    import re

    plan = {**PLAN, "plan": [{"step": "Исправить как в #12345", "files": ["A.cs"]}]}
    outcome, api, _ = run_stage(plan=plan, report="Сделано по #999 и AB#1")
    assert outcome == "done"
    description = json.loads(api.created[0].content)["description"]
    assert not re.search(r"#\d", description) and "№12345" in description
    assert "№999" in description and "AB№1" in description


def test_push_goes_only_to_the_server_and_repository_from_the_profile(
    run_stage: Callable[..., tuple[str, Api, Settings]], world: dict[str, Path]
) -> None:
    from ario.errors import StageFailed
    from ario.publish import check_remote

    config = publish_config()
    good = "https://rxtfs.example.test/Collection/Sungero/_git/DirectumRX"
    check_remote(good, config)
    check_remote(good + "/", config)
    check_remote("https://rxtfs.example.test/Collection/Sungero/_git/directumrx", config)  # регистр
    check_remote("/srv/git/anything.git", config)  # локальный путь
    for bad in (
        "https://other.example.test/Collection/Sungero/_git/DirectumRX",  # другой сервер
        "https://rxtfs.example.test/Collection/Sungero/_git/OtherRepo",  # другой репозиторий
    ):
        with pytest.raises(StageFailed, match="push отменён"):
            check_remote(bad, config)
    git(
        world["work"],
        "remote",
        "set-url",
        "origin",
        "https://evil.example.test/c/p/_git/DirectumRX",
    )
    outcome, api, settings = run_stage()
    assert outcome == "needs-human" and "push отменён" in reason(settings) and api.requests == []
