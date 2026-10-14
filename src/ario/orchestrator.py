"""Оркестратор: машина состояний тикета. Сам LLM не использует."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ario.config import Settings
from ario.errors import InjectionDetected, LimitExceeded, StageFailed
from ario.fsutil import RunLock
from ario.guardrails import Deadline, Guardrails
from ario.repos import RepoProfile, RepoRegistry, RepoSelectionError
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


class RunObserver(Protocol):
    """Наблюдатель за прогоном (например, отчёт на доске). Его сбои прогон не роняют."""

    def stage_started(self, ctx: StageContext, stage: str) -> None: ...

    def stage_finished(self, ctx: StageContext, stage: str) -> None: ...

    def run_finished(self, ctx: StageContext, outcome: str, reason: str | None) -> None: ...


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
    repo: RepoProfile | None = None  # репозиторий тикета; `None`: репозиторий не настроен


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
        repos: RepoRegistry | None = None,
        repo_override: str | None = None,
        observer: RunObserver | None = None,
    ) -> None:
        self._settings = settings
        self._observer = observer
        self._ctx: StageContext | None = None  # контекст последнего этапа: для итогового отчёта
        self._repos = repos if repos is not None else RepoRegistry.from_settings(settings)
        self._repo_override = repo_override
        self._guardrails = guardrails or Guardrails.load(settings.guardrails_path)
        self._preflight = preflight
        self._stages = list(stages) if stages is not None else default_stages()
        self._states = StateStore(settings.runs_dir)
        self._masker = Masker.from_environment(
            os.environ,
            self._guardrails.config.env,
            [settings.openrouter_api_key, settings.tfs_pat, settings.tfs_write_pat],
        )
        self._tickets = TicketStore(settings.tickets_dir, self._masker.mask_text)
        self._work_base = 0.0
        self._work_started = time.monotonic()

    def _trace(self, ticket_id: str, trace_id: str) -> TraceWriter:
        return TraceWriter(self._settings.traces_dir, ticket_id, trace_id, self._masker)

    def _check_repo(self, repo: RepoProfile | None) -> None:
        """Чистое дерево репозитория: чужие незакоммиченные изменения не должны попасть в работу."""
        if self._preflight is not None:
            self._preflight()
            return
        if repo is not None:
            git = GitRepo(
                repo.path, self._guardrails.config.env, repo.base_branch, repo.branch_template
            )
            try:
                git.ensure_clean()
            except ToolError as e:  # неверный путь, нет git и т.п.
                raise StageFailed(
                    f"Не удалось проверить рабочее дерево репозитория {repo.name}: {e}"
                ) from e

    def _repo_of_run(self, state: RunState) -> RepoProfile | None:
        """Профиль начатого прогона: из состояния; прогон, начатый до профилей, — по умолчанию."""
        return self._repos.get(state.repo) if state.repo is not None else self._repos.default

    def _repo_for_new_ticket(self, ticket_id: str) -> RepoProfile | None:
        """Профиль нового тикета по правилам или `--repo`."""
        try:
            ticket = load_ticket(self._tickets.path(ticket_id, "inbox"))
        except (TicketError, OSError):
            return self._repos.default  # некорректный тикет отклонит `_execute`
        return self._repos.select(ticket, self._repo_override)

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
                repo = self._repo_for_new_ticket(ticket_id)
                state.repo = repo.name if repo is not None else None
                self._check_repo(repo)
            except (StageFailed, RepoSelectionError) as e:
                trace.event("ticket_not_taken", reason=e.reason)
                self._tickets.add_comment(ticket_id, "inbox", "orchestrator", "not_taken", e.reason)
                raise TicketNotTakenError(e.reason) from e
            self._states.save(state)  # выбор профиля не должен пропасть, если процесс убьют сейчас
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
        if self._ctx is not None:
            ctx = self._ctx
            self._notify(trace, "run_finished", ctx, final, failure)
        trace.event(
            "run_finished",
            outcome=final,
            reason=failure,
            total_seconds=round(state.active_seconds, 2),
        )
        return final

    def _notify(self, trace: TraceWriter, event: str, *args: object) -> None:
        """Вызывает метод наблюдателя `event`; любой его сбой — только событие в трассе."""
        if self._observer is None:
            return
        try:
            getattr(self._observer, event)(*args)
        except Exception as e:
            trace.event(
                "observer_failed", reason=self._masker.mask_text(f"{type(e).__name__}: {e}")
            )

    def _save(self, state: RunState) -> None:
        """Сохраняет состояние вместе с накопленным рабочим временем."""
        state.active_seconds = self._work_base + (time.monotonic() - self._work_started)
        self._states.save(state)

    def _execute(self, ticket_id: str, state: RunState, trace: TraceWriter) -> str | None:
        """Возвращает причину отказа или `None`, если все этапы выполнены."""
        self._ctx = None  # итоговый отчёт только по контексту этого тикета, не прошлого
        path = self._tickets.path(ticket_id, "in-progress")
        try:
            ticket = load_ticket(path)
            if ticket.id != ticket_id:  # иначе тикет мог бы войти в ветку другого тикета
                raise TicketError(
                    f"id в файле ({ticket.id}) не совпадает с именем файла ({ticket_id})"
                )
        except TicketError as e:
            trace.event("ticket_invalid", reason=str(e))
            return f"Некорректный файл тикета: {e}"

        # контекст создаётся до проверок тикета, чтобы и отказ на входе (injection, пропавший
        # профиль) попал в итоговый отчёт на доске
        repo_error: str | None = None
        try:
            repo = self._repo_of_run(state)
        except RepoSelectionError as e:  # профиль убрали из repos.yaml посреди работы над тикетом
            repo, repo_error = None, e.reason
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
            repo,
        )
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        self._ctx = ctx
        try:
            self._guardrails.scanner.check(path.read_text(encoding="utf-8"), "тексте тикета")
        except InjectionDetected as e:
            trace.event("guardrail_blocked", guard="injection", reason=e.reason)
            return e.reason
        if repo_error is not None:
            trace.event("guardrail_blocked", guard="repo", reason=repo_error)
            return repo_error
        trace.event("repo_selected", repo=repo.name if repo is not None else None)
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
            self._notify(trace, "stage_started", ctx, stage.name)
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
                self._notify(trace, "stage_finished", ctx, stage.name)
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
