"""Оркестратор: машина состояний тикета. Сам LLM не использует."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ario.config import Settings
from ario.errors import InjectionDetected, LimitExceeded, StageFailed
from ario.guardrails import Deadline, Guardrails
from ario.state import RunState, StateStore
from ario.store import Folder, TicketStore, validate_ticket_id
from ario.ticket import Ticket, TicketError, load_ticket
from ario.trace import TraceWriter

__all__ = ["Orchestrator", "Stage", "StageContext", "StageFailed", "TicketAlreadyFinishedError"]


class TicketAlreadyFinishedError(RuntimeError):
    """Тикет уже в `done/` или `needs-human/`; повторный запуск не нужен."""


@dataclass
class StageContext:
    ticket: Ticket
    state: RunState
    run_dir: Path
    trace: TraceWriter
    settings: Settings
    guardrails: Guardrails
    deadline: Deadline


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[StageContext], None]


def _noop(ctx: StageContext) -> None:
    ctx.trace.event("stub", stage="stub", note="заглушка этапа")


def default_stages() -> list[Stage]:
    """Поток MVP. Пока заглушка; analyst/developer/reviewer добавляются в S03, S05, S06."""
    return [Stage("stub", _noop)]


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        stages: Sequence[Stage] | None = None,
        guardrails: Guardrails | None = None,
    ) -> None:
        self._settings = settings
        self._guardrails = guardrails or Guardrails.load(settings.guardrails_path)
        self._stages = list(stages) if stages is not None else default_stages()
        self._tickets = TicketStore(settings.tickets_dir)
        self._states = StateStore(settings.runs_dir)

    def run(self, ticket_id: str) -> Folder:
        """Проводит тикет по этапам; возвращает итоговую папку (`done` или `needs-human`).

        Повторный вызов для тикета в `in-progress/` продолжает с последнего готового этапа.
        """
        validate_ticket_id(ticket_id)
        self._tickets.ensure_dirs()
        folder = self._tickets.locate(ticket_id)
        if folder in ("done", "needs-human"):
            raise TicketAlreadyFinishedError(f"Тикет {ticket_id} уже в {folder}/")
        if folder == "inbox":
            others = [f for f in self._tickets.locations(ticket_id) if f != "inbox"]
            if others:
                raise TicketAlreadyFinishedError(
                    f"Тикет {ticket_id} есть и в inbox/, и в {others[0]}/: уберите дубликат"
                )
            self._tickets.move(ticket_id, "inbox", "in-progress")
            state = RunState(ticket_id=ticket_id)  # новая подача: старый прогон не переиспользуем
        else:
            state = self._states.load(ticket_id) or RunState(ticket_id=ticket_id)
        trace = TraceWriter(self._settings.traces_dir, ticket_id, state.trace_id)
        trace.event("run_started", resumed=folder == "in-progress")

        failure = self._execute(ticket_id, state, trace)
        if failure is None:
            state.status = "done"
            final: Folder = "done"
        else:
            state.status, state.reason = "needs_human", failure
            final = "needs-human"
            self._tickets.add_comment(
                ticket_id, "in-progress", "orchestrator", "needs_human", failure
            )
        self._states.save(state)
        self._tickets.move(ticket_id, "in-progress", final)
        trace.event("run_finished", outcome=final, reason=failure)
        return final

    def _execute(self, ticket_id: str, state: RunState, trace: TraceWriter) -> str | None:
        """Возвращает причину отказа или `None`, если все этапы выполнены."""
        path = self._tickets.path(ticket_id, "in-progress")
        try:
            ticket = load_ticket(path)
            self._guardrails.scanner.check(path.read_text(encoding="utf-8"), "тексте тикета")
        except TicketError as e:
            trace.event("ticket_invalid", reason=str(e))
            return f"Некорректный файл тикета: {e}"
        except InjectionDetected as e:
            trace.event("guardrail_blocked", guard="injection", reason=e.reason)
            return e.reason

        elapsed = (datetime.now(UTC) - datetime.fromisoformat(state.started_at)).total_seconds()
        deadline = self._guardrails.deadline(max(elapsed, 0.0))  # время считается с первого старта
        ctx = StageContext(
            ticket,
            state,
            self._states.run_dir(ticket_id),
            trace,
            self._settings,
            self._guardrails,
            deadline,
        )
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        for stage in self._stages:
            if state.stage(stage.name).status == "done":
                trace.event("stage_skipped", stage=stage.name)
                continue
            try:
                deadline.check()
            except LimitExceeded as e:
                trace.event("guardrail_blocked", guard="timeout", stage=stage.name, reason=e.reason)
                return e.reason
            state.start_stage(stage.name)
            self._states.save(state)
            trace.event("stage_started", stage=stage.name)
            try:
                stage.run(ctx)
            except StageFailed as e:
                reason = e.reason
            except Exception as e:  # сбой этапа не должен оставлять тикет «в работе»
                reason = f"Необработанная ошибка этапа {stage.name}: {type(e).__name__}: {e}"
            else:
                state.finish_stage(stage.name, ok=True)
                self._states.save(state)
                trace.event("stage_finished", stage=stage.name)
                continue
            state.finish_stage(stage.name, ok=False)
            self._states.save(state)
            trace.event("stage_failed", stage=stage.name, reason=reason)
            return reason
        return None
