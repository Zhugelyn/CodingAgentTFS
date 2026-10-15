"""Жёсткий предел времени одного запроса к модели.

Таймаут клиента (`timeout=180`) считается на каждую операцию чтения сокета: если провайдер
держит соединение и подсылает служебные байты (OpenRouter так делает), запрос не завершается
и не обрывается, а проверка лимита времени на тикет идёт только между событиями агента. Так один
зависший запрос мог держать прогон бесконечно.

Здесь запрос выполняется в отдельном потоке, а ждём мы не дольше предела и оставшегося времени
тикета. По истечении предела бросается `litellm.Timeout` (SDK повторяет такой сбой по общим
правилам), а если время тикета уже вышло, то `LimitExceeded`. Брошенный поток доживает свой запрос
в фоне и его результат отбрасывается.
"""

from __future__ import annotations

import contextlib
import contextvars
import importlib
import threading
from collections.abc import Callable, Iterator
from typing import Any

from litellm.exceptions import Timeout

from ario.errors import LimitExceeded
from ario.guardrails import Deadline

DEFAULT_CALL_TIMEOUT = 420.0


def guarded(
    function: Callable[..., Any], limit: float, remaining: Callable[[], float]
) -> Callable[..., Any]:
    """Оборачивает запрос к модели жёстким пределом `limit` секунд (и остатком времени тикета)."""

    def call(*args: Any, **kwargs: Any) -> Any:
        left = remaining()
        if left <= 0:
            raise LimitExceeded("Превышено время на тикет: запрос к модели не отправлен")
        wait = min(limit, left)
        outcome: dict[str, Any] = {}
        finished = threading.Event()
        context = contextvars.copy_context()  # сессия и метки телеметрии идут в поток запроса

        def work() -> None:
            try:
                outcome["value"] = context.run(function, *args, **kwargs)
            except BaseException as e:  # noqa: BLE001  (исключение передаётся вызывающему)
                outcome["error"] = e
            finally:
                finished.set()

        threading.Thread(target=work, name="llm-call", daemon=True).start()
        if not finished.wait(wait):
            if remaining() <= 0:
                raise LimitExceeded("Превышено время на тикет: запрос к модели не завершился")
            raise Timeout(
                message=f"Запрос к модели не завершился за {wait:g} с",
                model=str(kwargs.get("model", "")),
                llm_provider="openrouter",
            )
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    return call


@contextlib.contextmanager
def hard_call_limit(limit: float, deadline: Deadline) -> Iterator[None]:
    """На время работы агента ставит предел на каждый запрос SDK к модели.

    SDK держит свою ссылку на `completion`, поэтому подменяется она (как и в `llm_telemetry`);
    оборачивается то, что установлено сейчас, и после выхода возвращается как было.
    """
    module: Any = importlib.import_module("openhands.sdk.llm.llm")
    original = module.litellm_completion
    module.litellm_completion = guarded(original, limit, deadline.remaining)
    try:
        yield
    finally:
        module.litellm_completion = original
