"""Бэкенд запуска агента: интерфейс и реализация на OpenHands SDK."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ario.agents.verdicts import VerdictError
from ario.errors import InjectionDetected, LimitExceeded, StageFailed, StepLimitReached
from ario.guardrails import Deadline, InjectionScanner, TokenBudget
from ario.llm_guard import DEFAULT_CALL_TIMEOUT, hard_call_limit
from ario.llm_telemetry import llm_context
from ario.trace import TraceWriter

MAX_TRACE_TEXT = 20_000
WRAP_UP_STEPS = 3  # шагов на финальный запрос после исчерпания лимита
# доли лимита шагов, на которых агенту напоминают о бюджете (сообщение дописывается в конец истории)
BUDGET_NUDGES = (0.5, 0.8)
ERROR_STREAK_NUDGE = 2  # столько ошибок одного инструмента подряд, и агенту даётся подсказка
WRAP_UP_MESSAGE = (
    "Лимит шагов исчерпан, больше исследовать нельзя. Немедленно вызови finish с итоговым "
    "ответом в том формате, который требует твоя роль, по тому, что ты уже выяснил. "
    "Не вызывай другие инструменты. Всё, что не успел проверить, не придумывай: "
    "в плане или отчёте так и напиши «Уточнить: …» или «не проверено»."
)


@dataclass
class AgentRequest:
    """Всё, что нужно для одного запуска агента."""

    role: str
    root: Path  # корень RX: рабочая папка агента
    toolset: Toolset  # регистрирует инструменты роли в SDK перед запуском
    instructions: str  # корневой AGENTS.md + AGENTS.md роли
    task: str  # первое сообщение пользователя
    tool_names: list[str]
    skill_paths: list[Path]
    max_steps: int
    token_budget: TokenBudget
    deadline: Deadline
    scanner: InjectionScanner
    trace: TraceWriter
    stage: str
    model_retries: int = 3
    call_timeout: float = DEFAULT_CALL_TIMEOUT  # жёсткий предел одного запроса к модели, с
    # проверяет итоговый ответ; бросает VerdictError, тогда агенту даётся один повтор
    validate_final: Callable[[str], None] | None = None


@dataclass
class AgentResult:
    final_message: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    steps: int = 0
    notes: list[str] = field(default_factory=list)


class Toolset(Protocol):
    def register(self) -> None: ...


class AgentBackend(Protocol):
    def run(self, request: AgentRequest) -> AgentResult: ...


def _truncate(text: str) -> str:
    if len(text) <= MAX_TRACE_TEXT:
        return text
    return text[:MAX_TRACE_TEXT] + f"… [обрезано, всего {len(text)} симв.]"


class OpenHandsBackend:
    """Запускает агента через OpenHands SDK (`Agent` + `Conversation`)."""

    def __init__(
        self,
        model: str,
        api_key: str | None,
        retry_wait: tuple[int, int] = (8, 64),
        role_models: Mapping[str, str] | None = None,
    ) -> None:
        self._model = model
        self._role_models = dict(role_models or {})  # роль -> модель, иначе общая `model`
        self._api_key = api_key
        self._retry_wait = retry_wait  # (минимум, максимум) секунд между повторами запроса

    def run(self, request: AgentRequest) -> AgentResult:
        # SDK импортируем лениво: баннер и тяжёлые зависимости нужны только при реальном запуске
        os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
        _prepare_sdk()
        from openhands.sdk import LLM, Agent, AgentContext, Conversation, Tool
        from openhands.sdk.context import Skill
        from openhands.sdk.conversation.exceptions import ConversationRunError
        from openhands.sdk.conversation.state import ConversationExecutionStatus
        from openhands.sdk.event import ActionEvent, AgentErrorEvent, MessageEvent, ObservationEvent
        from openhands.sdk.event.conversation_error import ConversationErrorEvent
        from openhands.sdk.llm import content_to_str
        from pydantic import SecretStr

        request.toolset.register()

        def on_retry(attempt: int, max_attempts: int, error: BaseException | None) -> None:
            request.trace.event(
                "llm_retry",
                stage=request.stage,
                agent=request.role,
                attempt=attempt,
                max_attempts=max_attempts,
                error=_truncate(f"{type(error).__name__}: {error}") if error else None,
            )

        llm_model = self._role_models.get(request.role, self._model)
        llm = LLM(
            model=llm_model,
            api_key=SecretStr(self._api_key) if self._api_key else None,
            num_retries=request.model_retries,
            retry_min_wait=self._retry_wait[0],
            retry_max_wait=self._retry_wait[1],
            retry_listener=on_retry,
            timeout=180,
            max_output_tokens=4096,
            temperature=0.0,
            # OpenRouter возвращает в usage реальную цену запроса (с учётом кэша провайдера)
            litellm_extra_body=(
                {"usage": {"include": True}} if llm_model.startswith("openrouter/") else {}
            ),
        )
        context = AgentContext(
            skills=[Skill.load(p) for p in request.skill_paths],
            system_message_suffix=request.instructions,
            load_user_skills=False,
            load_public_skills=False,
            load_project_skills=False,
        )
        agent = Agent(
            llm=llm,
            agent_context=context,
            tools=[Tool(name=name) for name in request.tool_names],
            include_default_tools=["FinishTool"],
        )

        stop: list[StageFailed] = []  # первая причина принудительной остановки
        final: list[str] = []
        steps = 0
        trace, stage, role = request.trace, request.stage, request.role

        def used_tokens() -> int:
            usage = conversation.conversation_stats.get_combined_metrics().accumulated_token_usage
            # размер последнего запроса (prompt) — то, что ограничивает лимит контекста
            return int(usage.per_turn_token) if usage else 0

        def token_fields() -> dict[str, int | float]:
            """Токены и цена на момент вызова: контекст последнего запроса и суммы за прогон."""
            metrics = conversation.conversation_stats.get_combined_metrics()
            usage = metrics.accumulated_token_usage
            if not usage:
                return {}
            return {
                "context_tokens": int(usage.per_turn_token),
                "prompt_total": int(usage.prompt_tokens),
                "completion_total": int(usage.completion_tokens),
                "cost_total": round(float(metrics.accumulated_cost), 6),
            }

        edits = 0  # сколько раз агент менял файлы
        nudged: set[float] = set()
        error_streak: dict[str, int] = {}  # инструмент → ошибок подряд

        def error_nudge(tool: str) -> None:
            """Модель повторяет сломанный вызов, видя лишь «unparseable JSON»: подсказываем."""
            streak = error_streak.get(tool, 0) + 1
            error_streak[tool] = streak
            if streak != ERROR_STREAK_NUDGE or stop:
                return
            trace.event("error_nudge", stage=stage, agent=role, tool=tool, streak=streak)
            conversation.send_message(
                f"Вызов инструмента {tool} не удался {streak} раза подряд: аргументы пришли "
                "невалидными или обрезанными. Не повторяй тот же вызов. Передай шаблон только "
                "ASCII-символами (часть имени метода или идентификатора, без кириллицы, кавычек и "
                "обратных косых черт) или поищи по другому слову."
            )

        def budget_nudge() -> None:
            """Напоминание о бюджете шагов: модель сама шаги не считает и застревает в разведке."""
            for share in BUDGET_NUDGES:
                if share in nudged or steps < request.max_steps * share or stop:
                    continue
                nudged.add(share)
                left = max(request.max_steps - steps, 0)
                if share == BUDGET_NUDGES[0]:
                    advice = (
                        "Правок ещё нет: прекращай разведку и внеси простейшее исправление в "
                        "границах правил; допущения опиши в итоговом ответе."
                        if role == "developer" and not edits
                        else "Если информации уже достаточно, переходи к итоговому ответу; "
                        "не повторяй похожие поиски."
                    )
                else:
                    advice = "Заверши работу и дай итоговый ответ."
                trace.event("budget_nudge", stage=stage, agent=role, steps=steps, left=left)
                conversation.send_message(
                    f"Бюджет шагов: использовано {steps} из {request.max_steps}, "
                    f"осталось около {left}. {advice}"
                )

        def halt(reason: StageFailed) -> None:
            if not stop:
                stop.append(reason)
                trace.event("guardrail_blocked", stage=stage, agent=role, reason=reason.reason)
                conversation.pause()

        def on_event(event: object) -> None:
            nonlocal steps, edits
            if isinstance(event, ActionEvent):
                steps += 1
                arguments = getattr(event.tool_call, "arguments", "")
                trace.event(
                    "agent_action",
                    stage=stage,
                    agent=role,
                    tool=event.tool_name,
                    arguments=_truncate(str(arguments)),
                    thought=_truncate(" ".join(getattr(t, "text", "") for t in event.thought)),
                    reasoning=_truncate(str(getattr(event, "reasoning_content", None) or "")),
                    **token_fields(),
                )
                if event.tool_name == "finish":
                    final.append(str(getattr(event.action, "message", "")))
                if event.tool_name == "edit_file":
                    edits += 1
            elif isinstance(event, ObservationEvent):
                error_streak.pop(event.tool_name, None)  # инструмент снова работает
                text = event.observation.text
                trace.event(
                    "agent_observation",
                    stage=stage,
                    agent=role,
                    tool=event.tool_name,
                    text=_truncate(text),
                )
                try:
                    request.scanner.check(text, f"выводе инструмента {event.tool_name}")
                except InjectionDetected as e:
                    halt(e)
            elif isinstance(event, AgentErrorEvent):
                # ошибки вызова инструмента (искажённые аргументы): без них трассу не разобрать
                trace.event(
                    "agent_error",
                    stage=stage,
                    agent=role,
                    tool=getattr(event, "tool_name", None),
                    error=_truncate(str(event.error)),
                )
                error_nudge(str(getattr(event, "tool_name", None) or "?"))
            elif isinstance(event, MessageEvent):
                text = "".join(content_to_str(event.llm_message.content))
                trace.event(
                    "agent_message",
                    stage=stage,
                    agent=role,
                    source=event.source,
                    text=_truncate(text),
                )
            if isinstance(event, ActionEvent) and event.tool_name != "finish":
                budget_nudge()
            try:
                request.token_budget.check(used_tokens())
                request.deadline.check()
            except LimitExceeded as e:
                halt(e)

        conversation: Any = Conversation(
            agent=agent,
            workspace=str(request.root),
            callbacks=[on_event],
            max_iteration_per_run=request.max_steps,
            visualizer=None,
        )
        trace.event(
            "agent_started",
            stage=stage,
            agent=role,
            model=self._role_models.get(role, self._model),
            max_steps=request.max_steps,
            tools=request.tool_names,
            skills=[path.parent.name for path in request.skill_paths],
            instructions=_truncate(request.instructions),  # системные инструкции целиком
            task=_truncate(request.task),
        )

        def run_once(message: str) -> None:
            final.clear()
            conversation.send_message(message)
            try:
                with (
                    llm_context(request.trace.ticket_id, stage, role),
                    hard_call_limit(request.call_timeout, request.deadline),
                ):
                    conversation.run()
            except ConversationRunError as e:
                if stop:
                    return
                raise classify_run_error(e, role, request.model_retries) from e
            if stop:
                return
            state = conversation.state
            if state.execution_status == ConversationExecutionStatus.STUCK:
                raise StageFailed(f"Агент {role} зациклился")
            if state.execution_status == ConversationExecutionStatus.ERROR:
                detail = " ".join(
                    str(ev.detail) for ev in state.events if isinstance(ev, ConversationErrorEvent)
                )
                if "maximum iterations" in detail.lower():
                    raise StepLimitReached(
                        f"Превышен лимит шагов агента {role} ({request.max_steps})"
                    )
                raise StageFailed(f"Агент {role} завершился с ошибкой: {detail or 'неизвестно'}")

        try:
            try:
                run_once(request.task)
            except StepLimitReached:
                # шаги кончились без ответа: лучше неполный вердикт, чем потерянная работа
                trace.event("limit_wrap_up", stage=stage, agent=role, steps=steps)
                conversation.max_iteration_per_run = WRAP_UP_STEPS
                run_once(WRAP_UP_MESSAGE)
            if stop:
                raise stop[0]
            if request.validate_final is not None:
                answer = final[-1] if final else ""
                try:
                    request.validate_final(answer)
                except VerdictError as e:
                    trace.event("verdict_retry", stage=stage, agent=role, error=str(e))
                    run_once(
                        f"Твой итоговый ответ не прошёл проверку: {e}. "
                        "Исправь и вызови finish ещё раз, message — только JSON-объект по схеме."
                    )
                    if stop:
                        raise stop[0] from None
        finally:
            metrics = conversation.conversation_stats.get_combined_metrics()
            usage = metrics.accumulated_token_usage
            prompt = int(usage.prompt_tokens) if usage else 0
            completion = int(usage.completion_tokens) if usage else 0
            cached = int(usage.cache_read_tokens) if usage else 0
            cost = round(float(metrics.accumulated_cost), 6)
            trace.event(
                "agent_finished",
                stage=stage,
                agent=role,
                steps=steps,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cache_read_tokens=cached,
                cost_usd=cost,
            )
            conversation.close()
        if stop:
            raise stop[0]
        if not final:
            raise StageFailed(f"Агент {role} не вызвал finish с итоговым ответом")
        return AgentResult(final[-1], prompt, completion, steps)


def _causes(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain and len(chain) < 8:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def classify_run_error(error: BaseException, role: str, retries: int) -> StageFailed:
    """Превращает ошибку запуска агента в причину остановки тикета с понятным текстом."""
    from openhands.sdk.llm.exceptions import (
        LLMAuthenticationError,
        LLMContextWindowExceedError,
        LLMRateLimitError,
        LLMServiceUnavailableError,
        LLMTimeoutError,
    )

    for cause in _causes(error):
        if isinstance(cause, LimitExceeded):  # своё ограничение (время тикета), не сбой модели
            return cause
        status = getattr(cause, "status_code", None)
        if isinstance(cause, LLMAuthenticationError) or status in (401, 403):
            return StageFailed(
                f"Модель отклонила авторизацию (проверьте OPENROUTER_API_KEY): {cause}"
            )
        if isinstance(cause, LLMContextWindowExceedError):
            return LimitExceeded(f"Превышено окно контекста модели: {cause}")
        retryable = isinstance(
            cause, LLMRateLimitError | LLMTimeoutError | LLMServiceUnavailableError
        ) or (isinstance(status, int) and (status in (408, 429) or status >= 500))
        if retryable:
            return LimitExceeded(f"Сбой модели после {retries} попыток: {cause}")
    return StageFailed(f"Ошибка при работе агента {role}: {error}")


def _prepare_sdk() -> None:
    """Настройки процесса, без которых SDK не работает в нашей среде (см. docs/spike.md)."""
    import truststore

    truststore.inject_into_ssl()  # корпоративный прокси с self-signed сертификатом

    import openhands.sdk.tool.tool as tool_module

    # Поле `summary` в действиях ломает tool calling Qwen (`summaryCheck`): отключаем
    tool_module._create_action_type_with_summary = lambda action_type: action_type

    _dedupe_skill_invocations()


SKILL_LOADED = (
    "Навык {name} уже загружен выше в этом диалоге: возьми его текст из истории и не вызывай "
    "навык повторно."
)


def _dedupe_skill_invocations() -> None:
    """Повторный `invoke_skill` того же навыка в одном диалоге возвращает короткую заглушку.

    Навык (до 12 тыс. символов) остаётся в истории и пересылается на каждом шаге; повторный
    вызов добавил бы вторую копию (S24: навыки были 53% prompt-токенов).
    """
    from openhands.sdk.tool.builtins import invoke_skill as module

    executor = module.InvokeSkillExecutor
    if getattr(executor, "_ario_dedupe", False):
        return
    original = executor.__call__

    def call(self: Any, action: Any, conversation: Any = None) -> Any:
        name = str(action.name).strip()
        if conversation is not None and name in conversation.state.invoked_skills:
            return module.InvokeSkillObservation.from_text(
                text=SKILL_LOADED.format(name=name), skill_name=name
            )
        return original(self, action, conversation)

    executor.__call__ = call  # type: ignore[method-assign]
    executor._ario_dedupe = True  # type: ignore[attr-defined]
