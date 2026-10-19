"""Этап publish: коммит, push ветки тикета и открытие PR (S17, D08).

Это делает оркестратор, а не агент: у агентов нет ни commit, ни push. Этап идёт после одобрения
reviewer и только если репозиторий профиля описан блоком `publish` и задан `ARIO_PUBLISH=1`.

Безопасность: push без force и только ветки тикета (явный refspec); токен передаётся
переменными окружения `GIT_CONFIG_*` (его нет ни в адресе remote, ни в аргументах процесса);
повторный запуск после сбоя ничего не дублирует: пустой коммит не создаётся, push
идемпотентен, существующий активный PR переиспользуется.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from ario.agents.verdicts import VerdictError, load_analysis, read_review_verdict
from ario.errors import GuardViolation, StageFailed
from ario.fsutil import atomic_write_text
from ario.guardrails.boundaries import PathGuard, clean_env
from ario.guardrails.config import EnvConfig
from ario.orchestrator import Stage, StageContext
from ario.repos import PublishConfig, RepoProfile
from ario.safe_text import clean, close_open_fence, code_block, inline_code
from ario.stage_names import (
    MAX_HUMAN_REWORKS,
    PUBLISH,
    development_stage,
    human_attempt,
    rework_file,
)
from ario.tfs.client import TfsClient, TfsError
from ario.tools.git import GitRepo, Runner
from ario.tools.proc import ProcResult, ToolError, run_process

PUBLISH_FILE = "publish.json"
AGENT_REPLY_MARK = "Доработка агентом"  # по метке собственные ответы в PR отличаем от замечаний
MAX_PR_DESCRIPTION = 3900  # у TFS лимит описания PR 4000 символов
MAX_REPORT_CHARS = 1500
MAX_PLAN_STEPS = 8
PUSH_TIMEOUT = 180.0

ClientFactory = Callable[[PublishConfig, str], TfsClient]


def default_client(config: PublishConfig, token: str) -> TfsClient:
    return TfsClient(config.url, config.project, token, kind="git")


class GitPublisher:
    """Коммит и push ветки тикета. Отдельно от `GitRepo`: тот намеренно без commit и push."""

    def __init__(
        self,
        root: Path,
        env_config: EnvConfig,
        *,
        runner: Runner = run_process,
        timeout: float = PUSH_TIMEOUT,
    ) -> None:
        self._root = root
        self._env = {**clean_env(os.environ, env_config), "GIT_TERMINAL_PROMPT": "0"}
        self._runner = runner
        self._timeout = timeout

    def _run(self, *args: str, env: dict[str, str] | None = None) -> ProcResult:
        result = self._runner(
            ["git", *args], self._root, self._timeout, {**self._env, **(env or {})}
        )
        if result.timed_out:
            raise ToolError(f"git {args[0]}: превышен таймаут {self._timeout:g} с")
        return result

    def _git(self, *args: str, env: dict[str, str] | None = None) -> str:
        result = self._run(*args, env=env)
        if result.returncode != 0:
            raise ToolError(
                f"git {args[0]} завершился с кодом {result.returncode}: {result.output.strip()}"
            )
        return result.output

    def commits_ahead(self, base: str) -> int:
        """Сколько коммитов в ветке сверх базовой."""
        return int(self._git("rev-list", "--count", f"{base}..HEAD").strip() or 0)

    def remote_url(self, remote: str) -> str:
        return self._git("remote", "get-url", remote).strip()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def stage_all(self) -> list[str]:
        """Индексирует всё изменённое в рабочей папке; возвращает имена файлов в индексе."""
        self._git("add", "--all", "--", ".")
        names = self._git("diff", "--cached", "--name-only", "--relative", "-z")
        return [name for name in names.split("\0") if name]

    def staged_additions(self) -> dict[str, str]:
        """Добавленные в индекс строки по файлам (`git diff --cached -U0`): проверка секретов."""
        diff = self._git("diff", "--cached", "--no-color", "-U0", "--no-ext-diff", "--relative")
        found: dict[str, list[str]] = {}
        current: str | None = None
        for line in diff.splitlines():
            if line.startswith("+++ "):
                name = line[4:].strip()
                current = name[2:] if name.startswith("b/") else None
            elif line.startswith("+") and current is not None:
                found.setdefault(current, []).append(line[1:])
        return {name: "\n".join(lines) for name, lines in found.items()}

    def unstage(self) -> None:
        self._git("reset", "--quiet")

    def commit_all(self, message: str, author: tuple[str, str] | None = None) -> bool:
        """Индексирует и коммитит всё изменённое; `False`, если коммитить нечего."""
        return self.commit(message, author) if self.stage_all() else False

    def commit(self, message: str, author: tuple[str, str] | None = None) -> bool:
        """Коммитит то, что уже в индексе; `False`, если индекс пуст."""
        if self._run("diff", "--cached", "--quiet").returncode == 0:
            return False
        identity = (
            ["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}"] if author else []
        )
        self._git(*identity, "commit", "--message", message)
        return True

    def push(self, remote: str, branch: str, token: str) -> None:
        """Отправляет ветку тикета без force; существующую чужую историю не перезаписывает."""
        basic = base64.b64encode(f":{token}".encode()).decode()
        auth = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
        refspec = f"refs/heads/{branch}:refs/heads/{branch}"  # без `+`: не force
        self._git("push", "--set-upstream", remote, refspec, env=auth)


def board_url(ctx: StageContext) -> str | None:
    """Ссылка на тикет на доске (серверы доски и git разные, PR связываем ссылкой в тексте)."""
    settings = ctx.settings
    if ctx.ticket.source != "tfs" or not settings.tfs_url or not settings.tfs_project:
        return None
    base = f"{settings.tfs_url.rstrip('/')}/{quote(settings.tfs_project)}"
    return f"{base}/_workitems/edit/{ctx.ticket.id}"


def pull_request_url(config: PublishConfig, pr_id: int) -> str:
    return (
        f"{config.url}/{quote(config.project, safe='')}/_git/"
        f"{quote(config.repository, safe='')}/pullrequest/{pr_id}"
    )


_WORK_ITEM_REF = re.compile(r"#(?=\d)|(?<!\w)!(?=\d)")


def neutralize_refs(text: str) -> str:
    """`#123`, `AB#123` и `!123` TFS превращает в ссылку на work item или PR СВОЕГО сервера.
    Номера тикетов принадлежат серверу доски, а на сервере кода это были бы другие элементы."""
    return _WORK_ITEM_REF.sub("№", text)


def check_remote(url: str, config: PublishConfig) -> None:
    """Push только на сервер и в репозиторий из `publish`: remote в клоне мог указывать не туда."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return  # локальный путь или ssh: чужого сервера по http здесь нет
    expected = urlparse(config.url)
    path = unquote(parsed.path).lower().rstrip("/")
    if parsed.hostname != expected.hostname or not path.endswith(
        f"/_git/{config.repository.lower()}"
    ):
        raise StageFailed(
            f"Remote {config.remote} указывает на {parsed.hostname}{parsed.path}, а в publish "
            f"сервер {expected.hostname}, репозиторий {config.repository}: push отменён"
        )


FENCE_RESERVE = 16  # запас на закрывающее ограждение, если обрезка оборвала блок кода


def _limit(text: str) -> str:
    """Обрезка описания PR по границе строки; оборванный блок кода закрывается."""
    if len(text) <= MAX_PR_DESCRIPTION:
        return text
    cut = text[: MAX_PR_DESCRIPTION - FENCE_RESERVE]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    return close_open_fence(cut)


def _forbidden(names: list[str], guard: PathGuard) -> list[str]:
    """Файлы индекса, которые агенту нельзя ни менять, ни читать (и тем более публиковать)."""
    bad: list[str] = []
    for name in names:
        try:
            guard.check(name, "write")
            guard.check(name, "read")
        except GuardViolation:
            bad.append(name)
    return bad


def _one_line(text: str) -> str:
    return " ".join(text.split())


_ATTEMPTS = range(1, human_attempt(MAX_HUMAN_REWORKS) + 1)


def _verdicts(ctx: StageContext) -> list[str]:
    return [v for n in _ATTEMPTS if (v := read_review_verdict(ctx.run_dir, n))]


def latest_round(run_dir: Path) -> int:
    """Номер последнего запрошенного раунда доработки по замечаниям человека (0, если не было)."""
    return max(
        (n for n in range(1, MAX_HUMAN_REWORKS + 1) if (run_dir / rework_file(n)).is_file()),
        default=0,
    )


def last_review_approved(ctx: StageContext) -> bool:
    """Решающим считается самое позднее ревью (после доработок это последняя попытка)."""
    for attempt in reversed(_ATTEMPTS):
        verdict = read_review_verdict(ctx.run_dir, attempt)
        if verdict is not None:
            return verdict == "approve"
    return False


def should_publish(ctx: StageContext) -> bool:
    return (
        ctx.settings.publish
        and ctx.repo is not None
        and ctx.repo.publish is not None
        and last_review_approved(ctx)
    )


def render_pr_description(ctx: StageContext) -> str:
    link = board_url(ctx)
    lines = [
        f"Тикет на сервере доски (не в этом репозитории): {link}"
        if link
        else f"Тикет № {ctx.ticket.id} (сервер доски)",
        "",
        inline_code(ctx.ticket.title),  # текст автора тикета: только как код, не как разметка
    ]
    try:
        plan = load_analysis(ctx.run_dir / "analysis.json").plan
    except (OSError, VerdictError):
        plan = []
    if plan:  # текст модели (S25): в блоке кода разметка, картинки и упоминания не работают
        steps = []
        for step in plan[:MAX_PLAN_STEPS]:
            files = f" ({', '.join(step.files)})" if step.files else ""
            steps.append(f"- {step.step}{files}")
        lines += ["", "## План", code_block("\n".join(steps))]
    for name in ("development-2.md", "development-1.md"):
        report = ctx.run_dir / name
        if report.is_file():
            text = report.read_text(encoding="utf-8")[:MAX_REPORT_CHARS]
            lines += ["", "## Отчёт разработчика", code_block(text)]
            break
    if verdicts := _verdicts(ctx):
        lines += ["", f"## Ревью: {' → '.join(verdicts)}"]
    lines += ["", "_Создано ArioCodingAgent. Изменения нужно проверить человеку до слияния._"]
    return _limit(neutralize_refs(ctx.mask("\n".join(lines))))


def read_publication(run_dir: Path) -> dict[str, Any] | None:
    """Итог публикации (`branch`, `commit`, `pr_id`, `pr_url`), если этап уже выполнен."""
    path = run_dir / PUBLISH_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def make_publish_stage(
    runner: Runner = run_process, client_factory: ClientFactory = default_client
) -> Stage:
    def run(ctx: StageContext) -> None:
        repo: RepoProfile | None = ctx.repo
        config = repo.publish if repo is not None else None
        if repo is None or config is None:
            raise StageFailed("Публикация не настроена: в профиле репозитория нет блока publish")
        token = os.environ.get(config.pat_env, "").strip()
        if not token:
            raise StageFailed(f"Не задан токен {config.pat_env}: push и PR невозможны")
        try:
            _publish(ctx, repo, config, token, runner, client_factory)
        except (ToolError, TfsError) as e:
            raise StageFailed(f"Публикация не удалась: {ctx.mask(str(e))}") from e

    return Stage(PUBLISH, run)


def _reply_in_pull_request(
    ctx: StageContext,
    config: PublishConfig,
    client_factory: ClientFactory,
    token: str,
    pr_id: int,
    round_number: int,
) -> None:
    """Итог доработки пишется в PR отдельным комментарием; сбой комментария этап не роняет."""
    report = ctx.run_dir / f"{development_stage(human_attempt(round_number))}.md"
    summary = report.read_text(encoding="utf-8")[:MAX_REPORT_CHARS] if report.is_file() else ""
    head = f"{AGENT_REPLY_MARK} №{round_number} по замечаниям учтена."
    text = neutralize_refs(
        ctx.mask(f"{head}\n\n{code_block(summary)}" if summary.strip() else head)
    )
    try:
        with client_factory(config, token) as client:
            client.add_pull_request_comment(config.repository, pr_id, text)
    except TfsError as e:
        ctx.trace.event("pr_comment_failed", stage=PUBLISH, reason=ctx.mask(str(e)))


def _publish(
    ctx: StageContext,
    repo: RepoProfile,
    config: PublishConfig,
    token: str,
    runner: Runner,
    client_factory: ClientFactory,
) -> None:
    env = ctx.guardrails.config.env
    git = GitRepo(repo.path, env, repo.base_branch, repo.branch_template, runner=runner)
    branch = git.branch_for(ctx.ticket.id, ctx.ticket.type)
    current = git.current_branch()
    if current != branch:
        raise StageFailed(f"Для публикации ожидалась ветка {branch}, сейчас {current or '?'}")
    publisher = GitPublisher(repo.path, env, runner=runner)

    title = _one_line(ctx.ticket.title)
    message = config.commit_template.format(id=ctx.ticket.id, title=title)
    link = board_url(ctx)
    if link:
        message += f"\n\nТикет: {link}"
    author = (
        (config.author_name, config.author_email)
        if config.author_name and config.author_email
        else None
    )
    check_remote(publisher.remote_url(config.remote), config)  # до любых изменений в клоне
    staged = publisher.stage_all()
    forbidden = _forbidden(staged, ctx.guardrails.path_guard(repo.path, repo.rules))
    if forbidden:
        publisher.unstage()
        shown = ", ".join(forbidden[:10]) + (" и ещё" if len(forbidden) > 10 else "")
        raise StageFailed(
            f"В изменениях есть файлы, которые агенту менять и публиковать нельзя: {shown}. "
            "Это не его правки (сборка, чужие файлы): уберите их и запустите этап снова."
        )
    leaked = [name for name, text in publisher.staged_additions().items() if ctx.mask(text) != text]
    if leaked:  # сам секрет в сообщение не попадает: только имена файлов
        publisher.unstage()
        ctx.trace.event("secret_blocked", stage=PUBLISH, where="publish", files=leaked[:10])
        shown = ", ".join(leaked[:10]) + (" и ещё" if len(leaked) > 10 else "")
        raise StageFailed(
            f"В добавленных строках есть секреты (ключи, токены, пароли): {shown}. "
            "Публикация остановлена: уберите их из правок и запустите этап снова."
        )
    committed = publisher.commit(neutralize_refs(clean(ctx.mask(message))), author)
    if publisher.commits_ahead(repo.base_branch) == 0:
        raise StageFailed("В ветке нет коммитов сверх базовой: публиковать нечего")
    publisher.push(config.remote, branch, token)

    with client_factory(config, token) as client:
        existing = client.find_pull_request(config.repository, branch, config.target_branch)
        previous_pr = (read_publication(ctx.run_dir) or {}).get("pr_id")
        if existing is None and previous_pr:  # прежний PR закрыт или отменён: откроется новый
            ctx.trace.event("pr_replaced", stage=PUBLISH, previous_pr=previous_pr)
        pr = existing or client.create_pull_request(
            config.repository,
            branch,
            config.target_branch,
            neutralize_refs(
                clean(ctx.mask(config.pr_title_template.format(id=ctx.ticket.id, title=title)))
            ),
            render_pr_description(ctx),
            draft=config.draft,
        )
    pr_id = pr.get("pullRequestId")
    if not isinstance(pr_id, int) or isinstance(pr_id, bool):
        raise TfsError("TFS не вернул номер PR")
    rework_round = latest_round(ctx.run_dir)
    previous = read_publication(ctx.run_dir) or {}
    if rework_round > int(previous.get("rework_round") or 0):
        _reply_in_pull_request(ctx, config, client_factory, token, pr_id, rework_round)
    result = {
        "branch": branch,
        "commit": publisher.head(),
        "pr_id": pr_id,
        "pr_url": pull_request_url(config, pr_id),
        "target": config.target_branch,
        "rework_round": rework_round,
    }
    atomic_write_text(ctx.run_dir / PUBLISH_FILE, json.dumps(result, ensure_ascii=False, indent=2))
    ctx.trace.event(
        "published",
        stage=PUBLISH,
        branch=branch,
        committed=committed,
        pr_id=pr_id,
        pr_reused=existing is not None,
        pr_url=result["pr_url"],
    )
