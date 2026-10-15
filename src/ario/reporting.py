"""Отчёт оркестратора на доске: смена состояния по этапам и итоговые комментарии (S16, D08).

Что и когда писать, задаёт файл потока (`ARIO_TICKET_FLOW`, образец
`config/ticket-flow.yaml.example`).
Без файла состояния не меняются, пишутся только комментарии («нужен человек» и «готово»).
Пишутся только тикеты, пришедшие из TFS (`source: tfs`); тикеты из папки не трогаются.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ario.agents.verdicts import VerdictError, load_analysis, read_review_verdict
from ario.branches import branch_name
from ario.config import ConfigError, Settings
from ario.orchestrator import StageContext
from ario.publish import latest_round, read_publication
from ario.stage_names import (
    ANALYSIS,
    DEVELOPMENT,
    MAX_HUMAN_REWORKS,
    PUBLISH,
    development_stage,
    human_attempt,
    review_stage,
)
from ario.tfs.client import BOARD_TIMEOUT, TfsClient
from ario.ticket_system import NullTicketSystem, TfsTicketSystem, TicketSystem, WriteResult

MAX_PLAN_STEPS = 5
STAGE_STARTED = {
    ANALYSIS: "analysis_started",
    DEVELOPMENT: "development_started",
    **{
        development_stage(human_attempt(n)): "rework_started"
        for n in range(1, MAX_HUMAN_REWORKS + 1)
    },
}
STAGE_FINISHED = {ANALYSIS: "analysis_done", PUBLISH: "pr_created"}
REVIEWS = {review_stage(a): a for a in range(1, human_attempt(MAX_HUMAN_REWORKS) + 1)}


class _Transition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    to: str = Field(min_length=1)
    allowed_from: list[str] = Field(alias="from", min_length=1)


class _States(BaseModel):
    model_config = ConfigDict(extra="forbid")  # опечатка в названии события не должна молчать
    analysis_started: _Transition | None = None
    analysis_done: _Transition | None = None
    development_started: _Transition | None = None
    review_approved: _Transition | None = None
    pr_created: _Transition | None = None  # после push и открытия PR (S17)
    rework_started: _Transition | None = None  # доработка по замечаниям человека началась (S19)


class _Comments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    needs_human: bool = True
    done: bool = True


class TicketFlow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    states: _States = Field(default_factory=_States)
    comments: _Comments = Field(default_factory=_Comments)

    @classmethod
    def load(cls, path: Path) -> TicketFlow:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        except (OSError, yaml.YAMLError, ValidationError) as e:
            raise ConfigError(f"{path}: {e}") from e


class TicketReporter:
    """Наблюдатель за прогоном: переводит события оркестратора в записи на доске."""

    def __init__(self, system: TicketSystem, flow: TicketFlow | None = None) -> None:
        self._system = system
        self._flow = flow or TicketFlow()

    @property
    def system(self) -> TicketSystem:
        return self._system

    def stage_started(self, ctx: StageContext, stage: str) -> None:
        event = STAGE_STARTED.get(stage)
        if event is not None:
            self._move(ctx, event, stage)

    def stage_finished(self, ctx: StageContext, stage: str) -> None:
        if (event := STAGE_FINISHED.get(stage)) is not None:
            self._move(ctx, event, stage)
        elif (attempt := REVIEWS.get(stage)) is not None and _verdict(ctx, attempt) == "approve":
            self._move(ctx, "review_approved", stage)

    def run_finished(self, ctx: StageContext, outcome: str, reason: str | None) -> None:
        if not _is_tfs(ctx):
            return
        if outcome == "needs-human" and self._flow.comments.needs_human:
            text = (
                f"Агент остановился и не довёл тикет до конца.\nПричина: {reason or 'не указана'}"
            )
            self._comment(ctx, "needs_human", text, mention_author=True)
        elif outcome == "done" and self._flow.comments.done:
            self._comment(ctx, "done", _done_text(ctx), mention_author=False)

    def _move(self, ctx: StageContext, event: str, stage: str) -> None:
        transition = getattr(self._flow.states, event)
        if transition is None or not _is_tfs(ctx):
            return
        result = self._system.transition(
            ctx.ticket.id, transition.to, allowed_from=transition.allowed_from
        )
        _trace(ctx, "state", event, result, stage=stage, to=transition.to)

    def _comment(self, ctx: StageContext, event: str, text: str, *, mention_author: bool) -> None:
        result = self._system.comment(ctx.ticket.id, text, mention_author=mention_author)
        _trace(ctx, "comment", event, result)


def _is_tfs(ctx: StageContext) -> bool:
    return ctx.ticket.source == "tfs" and ctx.ticket.id.isdigit()


def _trace(
    ctx: StageContext,
    action: str,
    event: str,
    result: WriteResult,
    *,
    stage: str | None = None,
    to: str | None = None,
) -> None:
    ctx.trace.event(
        "ticket_system",
        stage=stage,
        action=action,
        on=event,
        status=result.status,
        detail=result.detail,
        to=to,
    )


def _done_text(ctx: StageContext) -> str:
    lines = ["Агент завершил работу над тикетом."]
    publication = read_publication(ctx.run_dir)
    if ctx.repo is not None:
        branch = branch_name(ctx.ticket.id, ctx.repo.branch_template, ctx.ticket.type)
        where = "отправлена, открыт PR" if publication else "создана локально, push и PR вручную"
        lines.append(f"Репозиторий: {ctx.repo.name}, ветка: {branch} ({where}).")
    try:
        plan = load_analysis(ctx.run_dir / "analysis.json").plan
    except (OSError, VerdictError):
        plan = []
    if plan:
        lines.append("План, по которому велась работа:")
        lines += [f"- {step.step}" for step in plan[:MAX_PLAN_STEPS]]
    if rounds := latest_round(ctx.run_dir):
        lines.append(f"Доработок по замечаниям человека: {rounds}.")
    if publication and publication.get("pr_url"):
        lines.append(f"PR: {publication['pr_url']}")
    verdicts = [v for n in (1, 2) if (v := _verdict(ctx, n))]
    if verdicts:
        lines.append(f"Ревью: {' → '.join(verdicts)}.")
    return "\n".join(lines)


def _verdict(ctx: StageContext, attempt: int) -> str | None:
    try:
        return read_review_verdict(ctx.run_dir, attempt)
    except (OSError, ValueError):
        return None


def write_config_problem(settings: Settings) -> str | None:
    """Чего не хватает для записи на доску при включённом `TFS_WRITE`; иначе `None`."""
    if settings.tfs_write and not (
        settings.tfs_url and settings.tfs_project and (settings.tfs_write_pat or settings.tfs_pat)
    ):
        return "TFS_WRITE задан, но нет TFS_URL, TFS_PROJECT или токена (TFS_WRITE_PAT, TFS_PAT)"
    return None


def build_reporter(settings: Settings, mask: Callable[[str], str]) -> TicketReporter:
    """Отчёт на доске по настройкам; без `TFS_WRITE` или без токена пишет только в трассу."""
    flow = TicketFlow.load(settings.ticket_flow_path) if settings.ticket_flow_path else None
    if not settings.tfs_write:
        return TicketReporter(NullTicketSystem(), flow)
    if (problem := write_config_problem(settings)) is not None:
        return TicketReporter(NullTicketSystem(problem), flow)  # тикеты из папки идут как шли
    assert settings.tfs_url and settings.tfs_project  # проверено выше
    client = TfsClient(
        settings.tfs_url,
        settings.tfs_project,
        settings.tfs_write_pat or settings.tfs_pat or "",
        timeout=BOARD_TIMEOUT,
        retry_wait=(),  # доска внутри прогона: без долгих повторов чтения
    )
    return TicketReporter(TfsTicketSystem(client, mask), flow)
