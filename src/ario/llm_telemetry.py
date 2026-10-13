"""Живая выгрузка запросов к модели в локальный Phoenix, с текстом (вариант Б решения D07).

Включается только явно (`ARIO_OTLP_LLM_CONTENT=1`) и только для локального приёмника: в
спаны попадают полные сообщения запросов и ответов модели, то есть код RX и тексты тикетов.
Каждый вызов `litellm.completion` превращается в LLM-спан (OpenInference) со всей историей
запроса, ответом, вызовами инструментов и токенами. Спаны группируются в Phoenix по сессиям
(`session.id` = номер тикета). Секреты маскируются перед отправкой.

Зависимости необязательные: `uv sync --extra telemetry`.
"""

from __future__ import annotations

import contextlib
import importlib
import socket
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event as SpanEvent
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from ario.trace import Masker

LLM_PROJECT = "ArioCodingAgent-LLM"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class LlmTracingError(RuntimeError):
    """Живую выгрузку запросов к модели включить нельзя."""


def endpoint_is_local(endpoint: str) -> bool:
    host = (urlparse(endpoint).hostname or "").lower()
    return host in LOCAL_HOSTS


def check_llm_content_settings(endpoint: str | None, *, allow_remote: bool) -> str:
    """Текст запросов уходит только на локальный сервер, если заказчик не разрешил иное."""
    if not endpoint:
        raise LlmTracingError("ARIO_OTLP_LLM_CONTENT требует ARIO_OTLP_ENDPOINT")
    if not endpoint_is_local(endpoint) and not allow_remote:
        raise LlmTracingError(
            "ARIO_OTLP_LLM_CONTENT отправляет код RX и тексты тикетов: приёмник должен быть "
            "локальным (localhost). Осознанный обход для чужого сервера: "
            "ARIO_ALLOW_REMOTE_LLM_CONTENT=1."
        )
    return endpoint


def check_reachable(endpoint: str, timeout: float = 2.0) -> None:
    """Приёмник должен отвечать на старте: иначе выгрузка молча падает в фоне."""
    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except OSError:
        raise LlmTracingError(
            f"Приёмник {host}:{port} не отвечает: запустите Phoenix или проверьте "
            "ARIO_OTLP_ENDPOINT"
        ) from None


class MaskingExporter(SpanExporter):
    """Маскирует секреты в атрибутах (строки и списки строк) и событиях спанов перед отправкой."""

    def __init__(self, inner: SpanExporter, masker: Masker) -> None:
        self._inner = inner
        self._masker = masker

    def _mask_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._masker.mask_text(value)
        if isinstance(value, tuple | list):
            return tuple(self._mask_value(item) for item in value)
        return value

    def _mask_attrs(self, attrs: Mapping[str, Any] | None) -> dict[str, Any]:
        return {key: self._mask_value(value) for key, value in (attrs or {}).items()}

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        masked = [
            ReadableSpan(
                name=span.name,
                context=span.context,
                parent=span.parent,
                resource=span.resource,
                attributes=self._mask_attrs(span.attributes),
                events=[
                    SpanEvent(e.name, self._mask_attrs(e.attributes), timestamp=e.timestamp)
                    for e in span.events
                ],
                links=span.links,
                kind=span.kind,
                status=span.status,
                start_time=span.start_time,
                end_time=span.end_time,
                instrumentation_scope=span.instrumentation_scope,
            )
            for span in spans
        ]
        return self._inner.export(masked)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


@dataclass
class _Active:
    provider: TracerProvider
    sdk_module: Any | None  # модуль SDK, где мы подменили ссылку на completion
    sdk_original: Any | None


_ACTIVE: _Active | None = None


def is_active() -> bool:
    return _ACTIVE is not None


def enable(
    endpoint: str,
    masker: Masker,
    *,
    project: str = LLM_PROJECT,
    exporter: SpanExporter | None = None,
    batch: bool = True,
) -> None:
    """Включает инструментацию `litellm.completion`; вызывать до первого запроса к модели."""
    global _ACTIVE
    if _ACTIVE is not None:
        return
    try:
        import litellm
        from openinference.instrumentation.litellm import LiteLLMInstrumentor
    except ImportError as e:
        raise LlmTracingError(
            "Не установлены пакеты телеметрии: выполните `uv sync --extra telemetry`"
        ) from e
    if exporter is None:
        check_reachable(endpoint)
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=endpoint, timeout=15)
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": "ario-coding-agent", "openinference.project.name": project}
        )
    )
    safe = MaskingExporter(exporter, masker)
    provider.add_span_processor(BatchSpanProcessor(safe) if batch else SimpleSpanProcessor(safe))
    # SDK держит свою ссылку на completion с момента импорта: импортируем его ДО подмены, чтобы
    # ссылка всегда была исходной функцией, и перепривязываем её к обёртке
    sdk_module: Any | None = None
    with contextlib.suppress(ImportError):
        sdk_module = importlib.import_module("openhands.sdk.llm.llm")
    original = litellm.completion
    instrumentor = LiteLLMInstrumentor()
    try:
        instrumentor.instrument(tracer_provider=provider)
    except Exception as e:
        provider.shutdown()
        raise LlmTracingError(f"Не удалось включить инструментацию ({type(e).__name__})") from None
    if not instrumentor.is_instrumented_by_opentelemetry:  # конфликт версий: молча не включилась
        provider.shutdown()
        raise LlmTracingError(
            "Инструментация litellm не включилась (вероятно, конфликт версий пакетов): "
            "см. вывод выше"
        )
    sdk_original: Any | None = None
    if sdk_module is not None and getattr(sdk_module, "litellm_completion", None) is original:
        sdk_original = original
        sdk_module.litellm_completion = litellm.completion
    _ACTIVE = _Active(provider, sdk_module if sdk_original is not None else None, sdk_original)


def shutdown() -> None:
    """Сбрасывает накопленные спаны, снимает инструментацию и возвращает исходные функции."""
    global _ACTIVE
    active, _ACTIVE = _ACTIVE, None
    if active is None:
        return
    with contextlib.suppress(Exception):
        active.provider.force_flush(10000)
        active.provider.shutdown()
    with contextlib.suppress(Exception):
        from openinference.instrumentation.litellm import LiteLLMInstrumentor

        LiteLLMInstrumentor().uninstrument()
    if active.sdk_module is not None:
        active.sdk_module.litellm_completion = active.sdk_original


@contextlib.contextmanager
def _attributes(ticket_id: str, stage: str, role: str) -> Iterator[None]:
    from openinference.instrumentation import using_attributes

    with using_attributes(
        session_id=ticket_id,
        metadata={"ticket": ticket_id, "stage": stage, "agent": role},
        tags=[stage, role],
    ):
        yield


def llm_context(ticket_id: str, stage: str, role: str) -> AbstractContextManager[None]:
    """Контекст, который помечает LLM-спаны тикетом (сессия), этапом и ролью; без выгрузки пуст."""
    if _ACTIVE is None:
        return contextlib.nullcontext()
    return _attributes(ticket_id, stage, role)
