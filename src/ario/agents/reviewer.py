"""Этап reviewer: проверка diff ветки тикета, вердикт `review-<n>.json` + `review-<n>.md`."""

from __future__ import annotations

from ario.agents.backend import AgentBackend, AgentRequest
from ario.agents.dev_tools import ReviewerToolset
from ario.agents.prompts import reviewer_task
from ario.agents.spec import SpecError, load_agent_spec
from ario.agents.verdicts import (
    Review,
    VerdictError,
    load_analysis,
    parse_review,
    read_review_verdict,
    render_analysis_md,
    render_review_md,
)
from ario.errors import StageFailed
from ario.fsutil import atomic_write_text
from ario.orchestrator import Stage, StageContext
from ario.repos import NO_REPO
from ario.stage_names import FIRST_HUMAN_ATTEMPT, review_stage
from ario.tools.git import GitRepo, Runner
from ario.tools.proc import ToolError, run_process

ROLE = "reviewer"
MAX_REVIEWS = 2  # схема B: разработка 1 → ревью 1 → разработка 2 → ревью 2


def _validate(text: str) -> None:
    parse_review(text)  # бросает VerdictError


def review_verdict(ctx: StageContext, attempt: int) -> str | None:
    """Вердикт ревью `attempt` (`approve` или `reject`), если оно уже есть."""
    return read_review_verdict(ctx.run_dir, attempt)


def make_reviewer_stage(
    backend: AgentBackend, *, attempt: int = 1, runner: Runner = run_process
) -> Stage:
    name = review_stage(attempt)

    def run(ctx: StageContext) -> None:
        repo = ctx.repo
        if repo is None:
            raise StageFailed(f"{NO_REPO}: reviewer не знает, где смотреть код")
        rx = repo.path
        try:
            spec = load_agent_spec(repo.knowledge_dir, ROLE)
        except SpecError as e:
            raise StageFailed(f"Ошибка конфигурации агента {ROLE}: {e}") from e

        analysis_path = ctx.run_dir / "analysis.json"
        development_path = ctx.run_dir / f"development-{attempt}.md"
        for required in (analysis_path, development_path):
            if not required.is_file():
                raise StageFailed(f"Нет {required.name}: ревью идёт после analyst и developer")
        try:
            plan_md = render_analysis_md(load_analysis(analysis_path))
        except VerdictError as e:
            raise StageFailed(str(e)) from e
        development_md = development_path.read_text(encoding="utf-8")

        git = GitRepo(
            rx,
            ctx.guardrails.config.env,
            repo.base_branch,
            repo.branch_template,
            runner=runner,
        )
        try:
            expected = git.branch_for(ctx.ticket.id, ctx.ticket.type)
            current = git.current_branch()
            if current != expected:
                raise StageFailed(f"Для ревью ожидалась ветка {expected}, сейчас {current or '?'}")
            diff = git.diff()
        except ToolError as e:
            raise StageFailed(f"Git: {e}") from e
        if not diff.strip():
            raise StageFailed("В ветке нет изменений относительно master: ревьюить нечего")
        ctx.guardrails.scanner.check(diff, "diff изменений")  # InjectionDetected → needs-human
        ctx.guardrails.scanner.check(development_md, "отчёте developer")

        guard = ctx.guardrails.path_guard(rx, repo.write_deny_extra)
        request = AgentRequest(
            role=ROLE,
            root=rx,
            toolset=ReviewerToolset(rx, guard, git),
            instructions=spec.instructions,
            task=reviewer_task(
                ctx.ticket,
                plan_md,
                development_md,
                diff,
                attempt,
                MAX_REVIEWS,
                human_rework=attempt >= FIRST_HUMAN_ATTEMPT,
            ),
            tool_names=spec.tools,
            skill_paths=spec.skill_paths,
            max_steps=ctx.guardrails.max_steps(ROLE),
            token_budget=ctx.guardrails.token_budget(),
            deadline=ctx.deadline,
            scanner=ctx.guardrails.scanner,
            trace=ctx.trace,
            stage=name,
            model_retries=ctx.guardrails.config.limits.model_retries,
            call_timeout=ctx.guardrails.config.limits.model_call_timeout_seconds,
            validate_final=_validate,
        )
        result = backend.run(request)
        try:
            _validate(result.final_message)  # после повтора бэкенд отдаёт ответ как есть
            review: Review = parse_review(result.final_message)
        except VerdictError as e:
            raise StageFailed(f"Агент {ROLE} вернул невалидный вердикт: {e}") from e

        markdown = render_review_md(review)
        atomic_write_text(
            ctx.run_dir / f"review-{attempt}.json", ctx.mask(review.model_dump_json(indent=2))
        )
        atomic_write_text(ctx.run_dir / f"review-{attempt}.md", ctx.mask(markdown))
        ctx.trace.event(
            "review_verdict",
            stage=name,
            agent=ROLE,
            verdict=review.verdict,
            issues=len(review.issues),
            steps=result.steps,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        if review.verdict == "reject" and attempt >= MAX_REVIEWS:
            # замечания попадут в комментарий тикета через `needs-human`, отдельно не дублируем
            raise StageFailed("Ревью отклонило реализацию и после доработки:\n\n" + markdown)
        ctx.add_comment(ROLE, review.verdict, markdown)

    return Stage(name, run)
