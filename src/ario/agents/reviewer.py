"""Этап reviewer: проверка diff ветки `ai/<id>`, вердикт `review-<n>.json` + `review-<n>.md`."""

from __future__ import annotations

import json

from ario.agents.backend import AgentBackend, AgentRequest
from ario.agents.dev_tools import ReviewerToolset
from ario.agents.prompts import reviewer_task
from ario.agents.spec import SpecError, load_agent_spec
from ario.agents.verdicts import (
    Review,
    VerdictError,
    load_analysis,
    parse_review,
    render_analysis_md,
    render_review_md,
)
from ario.errors import StageFailed
from ario.fsutil import atomic_write_text
from ario.orchestrator import Stage, StageContext
from ario.tools.git import GitRepo, Runner, branch_name
from ario.tools.proc import ToolError, run_process

ROLE = "reviewer"
MAX_REVIEWS = 2  # схема B: разработка 1 → ревью 1 → разработка 2 → ревью 2


def _validate(text: str) -> None:
    parse_review(text)  # бросает VerdictError


def review_verdict(ctx: StageContext, attempt: int) -> str | None:
    """Вердикт ревью `attempt` (`approve` или `reject`), если оно уже есть."""
    path = ctx.run_dir / f"review-{attempt}.json"
    if not path.is_file():
        return None
    verdict = json.loads(path.read_text(encoding="utf-8")).get("verdict")
    return str(verdict) if verdict else None


def make_reviewer_stage(
    backend: AgentBackend, *, attempt: int = 1, runner: Runner = run_process
) -> Stage:
    name = "review" if attempt == 1 else f"review-{attempt}"

    def run(ctx: StageContext) -> None:
        settings = ctx.settings
        if settings.rx_path is None:
            raise StageFailed("Не задан RX_PATH: reviewer не знает, где смотреть код")
        rx = settings.rx_path
        try:
            spec = load_agent_spec(settings.project_dir, ROLE)
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

        git = GitRepo(rx, ctx.guardrails.config.env, settings.base_branch, runner=runner)
        try:
            expected = branch_name(ctx.ticket.id)
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

        guard = ctx.guardrails.path_guard(rx)
        request = AgentRequest(
            role=ROLE,
            root=rx,
            toolset=ReviewerToolset(rx, guard, git),
            instructions=spec.instructions,
            task=reviewer_task(ctx.ticket, plan_md, development_md, diff, attempt, MAX_REVIEWS),
            tool_names=spec.tools,
            skill_paths=spec.skill_paths,
            max_steps=ctx.guardrails.max_steps(ROLE),
            token_budget=ctx.guardrails.token_budget(),
            deadline=ctx.deadline,
            scanner=ctx.guardrails.scanner,
            trace=ctx.trace,
            stage=name,
            model_retries=ctx.guardrails.config.limits.model_retries,
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
