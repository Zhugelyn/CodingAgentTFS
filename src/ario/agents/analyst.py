"""Этап analyst: оценка тикета, план, вердикт (`analysis.json` + `analysis.md`)."""

from __future__ import annotations

from collections.abc import Callable

from ario.agents.backend import AgentBackend, AgentRequest
from ario.agents.prompts import analyst_task
from ario.agents.readonly_tools import ReadonlyToolset
from ario.agents.spec import SpecError, load_agent_spec
from ario.agents.verdicts import (
    Analysis,
    VerdictError,
    failure_text,
    parse_analysis,
    render_analysis_md,
)
from ario.errors import GuardViolation, StageFailed
from ario.fsutil import atomic_write_text
from ario.guardrails import PathGuard
from ario.orchestrator import Stage, StageContext
from ario.repos import NO_REPO, RepoProfile
from ario.stage_names import ANALYSIS
from ario.tools.git import GitRepo, Runner
from ario.tools.proc import ToolError, run_process

ROLE = "analyst"
STAGE_NAME = ANALYSIS


def _make_validator(guard: PathGuard) -> Callable[[str], None]:
    """Проверка ответа: схема и существование файлов плана (против выдуманных путей)."""

    def validate(text: str) -> None:
        analysis = parse_analysis(text)  # бросает VerdictError
        missing: list[str] = []
        for step in analysis.plan:
            for file in step.files:
                try:
                    exists = guard.check(file, "read").exists()
                except GuardViolation:
                    exists = False
                if not exists:
                    missing.append(file)
        if missing:
            raise VerdictError(
                "В плане указаны несуществующие файлы (указывай только пути, которые видел сам): "
                + ", ".join(dict.fromkeys(missing))
            )

    return validate


def _checkout_base(ctx: StageContext, repo: RepoProfile, runner: Runner) -> None:
    """Analyst читает рабочую копию, а developer потом ветвится от базовой ветки: если в клоне
    выбрана другая ветка, план строился бы по чужому коду и developer не нашёл бы его в базе."""
    git = GitRepo(
        repo.path, ctx.guardrails.config.env, repo.base_branch, repo.branch_template, runner=runner
    )
    if not git.is_repository():
        return
    try:
        previous = git.ensure_base_checked_out(ctx.ticket.id, ctx.ticket.type)
    except ToolError as e:
        raise StageFailed(f"Не удалось подготовить ветку для анализа: {e}") from e
    if previous is not None:
        ctx.trace.event(
            "branch_switched", stage=ANALYSIS, to=repo.base_branch, **{"from": previous}
        )


def make_analyst_stage(backend: AgentBackend, runner: Runner = run_process) -> Stage:
    def run(ctx: StageContext) -> None:
        repo = ctx.repo
        if repo is None:
            raise StageFailed(f"{NO_REPO}: analyst не знает, где искать код")
        try:
            spec = load_agent_spec(repo.knowledge_dir, ROLE)
        except SpecError as e:
            raise StageFailed(f"Ошибка конфигурации агента {ROLE}: {e}") from e

        _checkout_base(ctx, repo, runner)
        guard = ctx.guardrails.path_guard(repo.path, repo.write_deny_extra)
        validate = _make_validator(guard)
        request = AgentRequest(
            role=ROLE,
            root=repo.path,
            toolset=ReadonlyToolset(repo.path, guard),
            instructions=spec.instructions,
            task=analyst_task(ctx.ticket),
            tool_names=spec.tools,
            skill_paths=spec.skill_paths,
            max_steps=ctx.guardrails.max_steps(ROLE),
            token_budget=ctx.guardrails.token_budget(),
            deadline=ctx.deadline,
            scanner=ctx.guardrails.scanner,
            trace=ctx.trace,
            stage=STAGE_NAME,
            model_retries=ctx.guardrails.config.limits.model_retries,
            call_timeout=ctx.guardrails.config.limits.model_call_timeout_seconds,
            validate_final=validate,
        )
        result = backend.run(request)
        try:
            validate(result.final_message)  # после повтора бэкенд отдаёт ответ как есть
            analysis: Analysis = parse_analysis(result.final_message)
        except VerdictError as e:
            raise StageFailed(f"Агент {ROLE} вернул невалидный вердикт: {e}") from e

        atomic_write_text(
            ctx.run_dir / "analysis.json", ctx.mask(analysis.model_dump_json(indent=2))
        )
        atomic_write_text(ctx.run_dir / "analysis.md", ctx.mask(render_analysis_md(analysis)))
        ctx.trace.event(
            "analysis_verdict",
            stage=STAGE_NAME,
            agent=ROLE,
            decision=analysis.decision,
            risk=analysis.risk,
            steps=result.steps,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        if analysis.decision != "suitable":
            # причина и что дополнить попадают в комментарий тикета через `needs-human`
            raise StageFailed(failure_text(analysis))
        ctx.add_comment(ROLE, "suitable", render_analysis_md(analysis))

    return Stage(STAGE_NAME, run)
