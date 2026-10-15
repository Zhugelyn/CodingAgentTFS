"""Поток тикета (схема B): analyst → developer → reviewer (до одной доработки) → publish."""

from __future__ import annotations

from dataclasses import replace

from ario.agents.analyst import make_analyst_stage
from ario.agents.backend import AgentBackend
from ario.agents.developer import make_developer_stage
from ario.agents.reviewer import make_reviewer_stage, review_verdict
from ario.orchestrator import Stage, StageContext
from ario.publish import make_publish_stage, should_publish
from ario.tools.git import Runner
from ario.tools.proc import run_process


def build_pipeline(backend: AgentBackend, runner: Runner = run_process) -> list[Stage]:
    """Шесть этапов; доработка и второе ревью выполняются, только если первое ревью отклонило.

    Условия считаются по артефактам прогона, поэтому возобновление после падения работает
    так же, как обычный запуск.
    """

    def needs_rework(ctx: StageContext) -> bool:
        return review_verdict(ctx, 1) == "reject"

    def rework_done(ctx: StageContext) -> bool:
        return ctx.state.stage("development-2").status == "done"

    return [
        make_analyst_stage(backend, runner),
        make_developer_stage(backend, attempt=1, runner=runner),
        make_reviewer_stage(backend, attempt=1, runner=runner),
        replace(make_developer_stage(backend, attempt=2, runner=runner), when=needs_rework),
        replace(make_reviewer_stage(backend, attempt=2, runner=runner), when=rework_done),
        # push и PR: только после одобрения ревью и только если публикация настроена и включена
        replace(make_publish_stage(runner), when=should_publish),
    ]
