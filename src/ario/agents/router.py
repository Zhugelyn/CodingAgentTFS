"""Этап routing: какой репозиторий доски нужен тикету (S23).

Идёт перед analyst, только если правил (`--repo`, один репозиторий, `match`, `default`) не хватило
и кандидатов несколько. Классификатор: один вызов дешёвой модели без инструментов; видит тикет и
описания репозиториев, кода не читает. Назвать репозиторий вне списка кандидатов он не может.
Неуверенный или неопределённый выбор уводит тикет к человеку.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ario.agents.backend import AgentBackend, AgentRequest
from ario.agents.prompts import ROUTER_INSTRUCTIONS, router_task
from ario.agents.verdicts import (
    UNKNOWN_REPO,
    Routing,
    VerdictError,
    load_routing,
    parse_routing,
)
from ario.errors import StageFailed
from ario.fsutil import atomic_write_text
from ario.orchestrator import Stage, StageContext
from ario.repos import RepoProfile
from ario.stage_names import ROUTING

ROLE = "router"
ROUTING_FILE = "routing.json"
DEFAULT_MAX_STEPS = 3  # вызов finish и запас на повтор неверного ответа


class _NoTools:
    """У классификатора нет инструментов: только finish."""

    def register(self) -> None:
        return None


def _validator(names: Sequence[str]) -> Callable[[str], None]:
    def validate(text: str) -> None:
        routing = parse_routing(text)  # бросает VerdictError
        if routing.repo != UNKNOWN_REPO and routing.repo not in names:
            raise VerdictError(
                f"репозитория {routing.repo!r} нет в списке: выбери из {', '.join(names)} "
                f"или верни {UNKNOWN_REPO}"
            )

    return validate


def _refusal(routing: Routing, names: Sequence[str]) -> str:
    kind = (
        "не смог определить репозиторий"
        if routing.repo == UNKNOWN_REPO
        else f"выбрал {routing.repo}, но не уверен ({routing.confidence})"
    )
    return (
        f"Классификатор {kind}: {routing.reason.strip() or 'причина не указана'}. "
        f"Репозитории доски: {', '.join(names)}. Уточните область (area), теги или описание "
        "тикета либо верните его в inbox/ и запустите с --repo <имя>."
    )


def classify(
    backend: AgentBackend, ctx: StageContext, candidates: Sequence[RepoProfile]
) -> Routing:
    """Один вызов классификатора; результат записан в `routing.json`, события в трассе."""
    names = [p.name for p in candidates]
    validate = _validator(names)
    limits = ctx.guardrails.config.limits
    request = AgentRequest(
        role=ROLE,
        root=ctx.run_dir,  # репозитория ещё нет: рабочая папка прогона, инструментов нет
        toolset=_NoTools(),
        instructions=ROUTER_INSTRUCTIONS,
        task=router_task(ctx.ticket, candidates),
        tool_names=[],
        skill_paths=[],
        max_steps=limits.max_steps.get(ROLE, DEFAULT_MAX_STEPS),
        token_budget=ctx.guardrails.token_budget(),
        deadline=ctx.deadline,
        scanner=ctx.guardrails.scanner,
        trace=ctx.trace,
        stage=ROUTING,
        model_retries=limits.model_retries,
        call_timeout=limits.model_call_timeout_seconds,
        validate_final=validate,
    )
    result = backend.run(request)
    try:
        validate(result.final_message)  # после повтора бэкенд отдаёт ответ как есть
        routing = parse_routing(result.final_message)
    except VerdictError as e:
        raise StageFailed(f"Агент {ROLE} вернул невалидный вердикт: {e}") from e
    routing = routing.model_copy(update={"candidates": names, "source": "model"})
    atomic_write_text(ctx.run_dir / ROUTING_FILE, ctx.mask(routing.model_dump_json(indent=2)))
    ctx.trace.event(
        "routing_verdict",
        stage=ROUTING,
        agent=ROLE,
        repo=routing.repo,
        confidence=routing.confidence,
        reason=ctx.mask(routing.reason),
        steps=result.steps,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    return routing


def make_router_stage(backend: AgentBackend) -> Stage:
    def run(ctx: StageContext) -> None:
        candidates = ctx.candidates
        names = [p.name for p in candidates]
        routing = load_routing(ctx.run_dir / ROUTING_FILE)
        # принятый раньше выбор (возврат после падения на проверке дерева) не повторяется: нужны
        # и репозиторий из текущих кандидатов, и достаточная уверенность
        if routing is None or not routing.decided or routing.repo not in names:
            routing = classify(backend, ctx, candidates)
        if not routing.decided:
            raise StageFailed(_refusal(routing, names))
        ctx.select_repo(next(p for p in candidates if p.name == routing.repo), "model")

    return Stage(ROUTING, run, when=lambda ctx: bool(ctx.candidates) and ctx.repo is None)
