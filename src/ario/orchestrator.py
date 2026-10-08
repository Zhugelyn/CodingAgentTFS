"""Оркестратор: машина состояний тикета. Сам LLM не использует."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ario.config import Settings
from ario.errors import InjectionDetected, LimitExceeded, StageFailed
from ario.fsutil import RunLock
from ario.guardrails import Deadline, Guardrails
from ario.state import RunState, StateStore
from ario.store import Folder, TicketStore, validate_ticket_id
from ario.ticket import Ticket, TicketError, load_ticket
from ario.tools.git import GitRepo
from ario.tools.proc import ToolError
from ario.trace import Masker, TraceWriter, utc_now

__all__ = [
    "Orchestrator",
    "Stage",
    "StageContext",
    "StageFailed",
    "TicketAlreadyFinishedError",
    "TicketNotReopenableError",
    "TicketNotTakenError",
]


class TicketAlreadyFinishedError(RuntimeError):
    """Тикет уже в `done/` или `needs-human/`; повторный запуск не нужен."""


class TicketNotReopenableError(RuntimeError):
    """Тикет нельзя возобновить: он не в `needs-human/` или нет состояния прогона."""


class TicketNotTakenError(RuntimeError):
    """Тикет не взят в работу (проверка перед стартом не прошла); остаётся в `inbox/`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


Preflight = Callable[[], None]


@dataclass
class StageContext:
    ticket: Ticket
    state: RunState
    run_dir: Path
    trace: TraceWriter
    settings: Settings
    guardrails: Guardrails
    deadline: Deadline
    add_comment: Callable[[str, str, str], None]  # (агент, вердикт, текст) → <id>.comments.md
    mask: Callable[[str], str]  # скрывает секреты в тексте, который пишется в артефакты


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[StageContext], None]
    # условие запуска: `False` — этап не нужен (например, доработка после approve), он пропускается
    when: Callable[[StageContext], bool] | None = None


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
        preflight: Preflight | None = None,
    ) -> None:
        self._settings = settings
        self._guardrails = guardrails or Guardrails.load(settings.guardrails_path)
        self._preflight = preflight or self._default_preflight
        self._stages = list(stages) if stages is not None else default_stages()
        self._states = StateStore(settings.runs_dir)
        self._masker = Masker.from_environment(
            os.environ, self._guardrails.config.env, [settings.openrouter_api_key]
        )
        self._tickets = TicketStore(settings.tickets_dir, self._masker.mask_text)
        self._work_base = 0.0
        self._work_started = time.monotonic()

    def _trace(self, ticket_id: str, trace_id: str) -> TraceWriter:
        return TraceWriter(self._settings.traces_dir, ticket_id, trace_id, self._masker)

    def _default_preflight(self) -> None:
        """Чистое дерево RX: чужие незакоммиченные изменения не должны попасть в работу агента."""
        if self._settings.rx_path is not None:
            repo = GitRepo(
                self._settings.rx_path,
                self._guardrails.config.env,
                self._settings.base_branch,
                self._settings.branch_template,
            )
            try:
                repo.ensure_clean()
            except ToolError as e:  # неверный RX_PATH, нет git и т.п.
                raise StageFailed(f"Не удалось проверить рабочее дерево RX: {e}") from e

    def run(self, ticket_id: str) -> Folder:
        """Проводит тикет по этапам; возвращает итоговую папку (`done` или `needs-human`).

        Повторный вызов для тикета в `in-progress/` (например, после убитого процесса)
        продолжает с последнего готового этапа. Один тикет ведёт один процесс (`RunLock`).
        """
        validate_ticket_id(ticket_id)
        self._tickets.ensure_dirs()
        folder = self._tickets.locate(ticket_id)
        if folder in ("done", "needs-human"):
            raise TicketAlreadyFinishedError(f"Тикет {ticket_id} уже в {folder}/")
        with RunLock(self._states.run_dir(ticket_id)):
            # пока ждали блокировку, другой процесс мог уже перенести или завершить тикет
            folder = self._tickets.locate(ticket_id)
            if folder in ("done", "needs-human"):
                raise TicketAlreadyFinishedError(f"Тикет {ticket_id} уже в {folder}/")
            return self._run_locked(ticket_id, folder)

    def reopen(self, ticket_id: str) -> Folder:
        """Возобновляет тикет из `needs-human/` с упавшего этапа, не повторяя готовые.

        Это явное решение человека (например, после починки окружения): упавшие этапы
        становятся `pending`, причина и счётчик времени на тикет сбрасываются.
        """
        validate_ticket_id(ticket_id)
        self._tickets.ensure_dirs()
        if self._tickets.locate(ticket_id) != "needs-human":
            raise TicketNotReopenableError(f"Тикет {ticket_id} не в needs-human/")
        state = self._states.load(ticket_id)
        if state is None:
            raise TicketNotReopenableError(
                f"Нет состояния прогона runs/{ticket_id}/state.json: верните тикет в inbox/ "
                "и запустите заново"
            )
        with RunLock(self._states.run_dir(ticket_id)):
            self._tickets.move(ticket_id, "needs-human", "in-progress")
            for stage_state in state.stages.values():
                if stage_state.status in ("failed", "running"):
                    stage_state.status = "pending"
            state.status, state.reason, state.started_at = "running", None, utc_now()
            state.active_seconds = 0.0  # человек дал тикету новый запас времени
            self._states.save(state)
            self._trace(ticket_id, state.trace_id).event("run_reopened")
        return self.run(ticket_id)

    def _run_locked(self, ticket_id: str, folder: Folder) -> Folder:
        if folder == "inbox":
            others = [f for f in self._tickets.locations(ticket_id) if f != "inbox"]
            if others:
                raise TicketAlreadyFinishedError(
                    f"Тикет {ticket_id} есть и в inbox/, и в {others[0]}/: уберите дубликат"
                )
            state = RunState(ticket_id=ticket_id)  # новая подача: старый прогон не переиспользуем
            trace = self._trace(ticket_id, state.trace_id)
            try:
                self._preflight()
            except StageFailed as e:
                trace.event("ticket_not_taken", reason=e.reason)
                self._tickets.add_comment(ticket_id, "inbox", "orchestrator", "not_taken", e.reason)
                raise TicketNotTakenError(e.reason) from e
            self._tickets.move(ticket_id, "inbox", "in-progress")
        else:
            state = self._states.load(ticket_id) or RunState(ticket_id=ticket_id)
            trace = self._trace(ticket_id, state.trace_id)
        self._work_base, self._work_started = state.active_seconds, time.monotonic()
        trace.event("run_started", resumed=folder == "in-progress")

        failure = self._execute(ticket_id, state, trace)
        if failure is not None:
            failure = self._masker.mask_text(failure)
        if failure is None:
            state.status, state.reason = "done", None
            final: Folder = "done"
        else:
            state.status, state.reason = "needs_human", failure
            final = "needs-human"
            self._tickets.add_comment(
                ticket_id, "in-progress", "orchestrator", "needs_human", failure
            )
        self._save(state)
        self._tickets.move(ticket_id, "in-progress", final)
        trace.event(
            "run_finished",
            outcome=final,
            reason=failure,
            total_seconds=round(state.active_seconds, 2),
        )
        return final

    def _save(self, state: RunState) -> None:
        """Сохраняет состояние вместе с накопленным рабочим временем."""
        state.active_seconds = self._work_base + (time.monotonic() - self._work_started)
        self._states.save(state)

    def _execute(self, ticket_id: str, state: RunState, trace: TraceWriter) -> str | None:
        """Возвращает причину отказа или `None`, если все этапы выполнены."""
        path = self._tickets.path(ticket_id, "in-progress")
        try:
            ticket = load_ticket(path)
            if ticket.id != ticket_id:  # иначе тикет мог бы войти в ветку другого тикета
                raise TicketError(
                    f"id в файле ({ticket.id}) не совпадает с именем файла ({ticket_id})"
                )
            self._guardrails.scanner.check(path.read_text(encoding="utf-8"), "тексте тикета")
        except TicketError as e:
            trace.event("ticket_invalid", reason=str(e))
            return f"Некорректный файл тикета: {e}"
        except InjectionDetected as e:
            trace.event("guardrail_blocked", guard="injection", reason=e.reason)
            return e.reason

        # лимит на тикет считается по накопленному рабочему времени, простои между запусками
        # (убитый процесс, ночь) его не расходуют
        deadline = self._guardrails.deadline(state.active_seconds)
        ctx = StageContext(
            ticket,
            state,
            self._states.run_dir(ticket_id),
            trace,
            self._settings,
            self._guardrails,
            deadline,
            lambda agent, verdict, text: self._tickets.add_comment(
                ticket_id, "in-progress", agent, verdict, text
            ),
            self._masker.mask_text,
        )
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        for stage in self._stages:
            if state.stage(stage.name).status == "done":
                trace.event("stage_skipped", stage=stage.name)
                continue
            if stage.when is not None:
                try:
                    needed = stage.when(ctx)
                except (
                    Exception
                ) as e:  # повреждённый артефакт и т.п.: не оставляем тикет «в работе»
                    reason = (
                        f"Не удалось определить, нужен ли этап {stage.name}: "
                        f"{type(e).__name__}: {e}"
                    )
                    trace.event("stage_failed", stage=stage.name, reason=reason)
                    return reason
                if not needed:
                    state.stage(stage.name).status = "skipped"
                    self._save(state)
                    trace.event("stage_not_needed", stage=stage.name)
                    continue
            try:
                deadline.check()
            except LimitExceeded as e:
                trace.event("guardrail_blocked", guard="timeout", stage=stage.name, reason=e.reason)
                return e.reason
            state.start_stage(stage.name)
            self._save(state)
            trace.event("stage_started", stage=stage.name)
            stage_began = time.monotonic()
            try:
                stage.run(ctx)
            except StageFailed as e:
                reason = e.reason
            except Exception as e:  # сбой этапа не должен оставлять тикет «в работе»
                reason = f"Необработанная ошибка этапа {stage.name}: {type(e).__name__}: {e}"
            else:
                state.finish_stage(stage.name, ok=True)
                self._save(state)
                trace.event(
                    "stage_finished",
                    stage=stage.name,
                    seconds=round(time.monotonic() - stage_began, 2),
                )
                continue
            state.finish_stage(stage.name, ok=False)
            self._save(state)
            trace.event(
                "stage_failed",
                stage=stage.name,
                reason=reason,
                seconds=round(time.monotonic() - stage_began, 2),
            )
            return reason
        return None
