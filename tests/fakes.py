"""Заглушка бэкенда агента: отдаёт заготовленные ответы вместо вызова модели."""

from __future__ import annotations

from ario.agents.backend import AgentRequest, AgentResult
from ario.agents.verdicts import VerdictError
from ario.errors import StageFailed


class FakeBackend:
    """Повторяет контракт реального бэкенда: проверка ответа и один повтор при VerdictError."""

    def __init__(self, *answers: str | Exception) -> None:
        self._answers = list(answers)
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        attempts = 0
        while True:
            answer = self._answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            attempts += 1
            if request.validate_final is None:
                return AgentResult(answer, 100, 50, 3)
            try:
                request.validate_final(answer)
            except VerdictError as e:
                if attempts >= 2:
                    raise StageFailed(f"Агент вернул невалидный вердикт: {e}") from e
                continue
            return AgentResult(answer, 100, 50, 3)


class RoleBackend:
    """Бэкенд для сквозных тестов: у каждой роли своя очередь ответов.

    Ответ — строка (итог агента), исключение (сбой) или функция от `toolset` (сценарий
    действий developer через его инструменты, возвращает итоговое резюме).
    """

    def __init__(self, **queues: list[object]) -> None:
        self._queues = {role: list(items) for role, items in queues.items()}
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        item = self._queues[request.role].pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(request.toolset)
        assert isinstance(item, str)
        return AgentResult(item, 100, 20, 3)
