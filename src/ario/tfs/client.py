"""Клиент Azure DevOps Server (TFS) для чтения тикетов по REST API.

Только чтение: GET work item и комментариев, POST запроса WIQL (он ничего не меняет).
Авторизация по PAT (Basic с пустым именем). Токен нигде не попадает в сообщения об ошибках.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

API_VERSION = "7.1"
COMMENTS_API_VERSION = "7.1-preview.4"  # комментарии в Server 2022.2 только как preview
MAX_RESPONSE_BYTES = 5_000_000
MAX_COMMENT_PAGES = 10
COMMENTS_PAGE_SIZE = 200
DEFAULT_TIMEOUT = 30.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_RETRY_WAIT = (1.0, 3.0, 8.0)  # паузы между повторами, их число = число повторов


class TfsError(RuntimeError):
    """Сбой обращения к TFS (сеть, статус, неверный ответ)."""


class TfsAuthError(TfsError):
    """TFS отклонил PAT (401/403)."""


class TfsNotFoundError(TfsError):
    """Нет такого проекта или work item (404)."""


def _snippet(body: bytes) -> str:
    text = " ".join(body[:200].decode("utf-8", "replace").split())
    return f": {text}" if text else ""


class TfsClient:
    def __init__(
        self,
        base_url: str,
        project: str,
        pat: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry_wait: tuple[float, ...] = DEFAULT_RETRY_WAIT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("TFS_URL должен начинаться с https:// (или http://)")
        if not pat:
            raise ValueError("Не задан TFS_PAT")
        self._project = quote(project, safe="")
        self._retry_wait = retry_wait
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            auth=httpx.BasicAuth("", pat),
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,  # переход на чужой хост унёс бы заголовок Authorization
        )

    def __repr__(self) -> str:
        return f"TfsClient({self._http.base_url!s})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TfsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- публичные методы ---------------------------------------------------------------------
    def wiql(self, query: str, top: int) -> list[int]:
        """Выполняет запрос WIQL и возвращает id work items в порядке запроса (не больше `top`)."""
        data = self._request(
            "POST",
            f"{self._project}/_apis/wit/wiql",
            {"api-version": API_VERSION, "$top": top},
            payload={"query": query},
        )
        items = data.get("workItems")
        if not isinstance(items, list):
            raise TfsError("Ответ WIQL без списка workItems")
        ids: list[int] = []
        for item in items:
            raw = item.get("id") if isinstance(item, dict) else None
            if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
                raise TfsError("В ответе WIQL есть work item без числового id")
            ids.append(raw)
        return ids[:top]

    def work_item(self, item_id: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{self._project}/_apis/wit/workitems/{int(item_id)}",
            {"$expand": "all", "api-version": API_VERSION},
        )

    def comments(self, item_id: int) -> list[dict[str, Any]]:
        """Комментарии work item в порядке TFS (новые первыми), с постраничным чтением."""
        result: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_COMMENT_PAGES):
            params: dict[str, Any] = {
                "api-version": COMMENTS_API_VERSION,
                "$top": COMMENTS_PAGE_SIZE,
            }
            if token:
                params["continuationToken"] = token
            data = self._request(
                "GET", f"{self._project}/_apis/wit/workItems/{int(item_id)}/comments", params
            )
            page = data.get("comments")
            if not isinstance(page, list):
                raise TfsError("Ответ комментариев без списка comments")
            result += [c for c in page if isinstance(c, dict)]
            raw_token = data.get("continuationToken")
            token = str(raw_token) if raw_token else None
            if not token:
                return result
        raise TfsError(f"У work item {item_id} больше {MAX_COMMENT_PAGES} страниц комментариев")

    # --- транспорт ----------------------------------------------------------------------------
    def _request(
        self, method: str, path: str, params: dict[str, Any], payload: Any = None
    ) -> dict[str, Any]:
        attempts = len(self._retry_wait) + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                with self._http.stream(method, path, params=params, json=payload) as response:
                    body = self._read_limited(response)
            except httpx.TransportError as e:  # таймаут, обрыв, ошибка TLS
                if last:
                    raise TfsError(f"Нет связи с TFS ({type(e).__name__})") from None
                self._sleep(self._retry_wait[attempt])
                continue
            if response.status_code in RETRY_STATUSES and not last:
                self._sleep(self._retry_after(response, self._retry_wait[attempt]))
                continue
            return self._parse(response, body, path)
        raise AssertionError("недостижимо")  # цикл всегда возвращает или бросает

    @staticmethod
    def _retry_after(response: httpx.Response, default: float) -> float:
        try:
            wait = float(response.headers.get("Retry-After", default))
        except ValueError:  # дата HTTP или мусор
            return default
        return min(max(wait, 0.0), 60.0) if math.isfinite(wait) else default

    @staticmethod
    def _read_limited(response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise TfsError(f"Ответ TFS больше {MAX_RESPONSE_BYTES} байт")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse(response: httpx.Response, body: bytes, path: str) -> dict[str, Any]:
        status = response.status_code
        where = path.split("?")[0]
        if status in (401, 403):
            raise TfsAuthError(f"TFS отклонил доступ ({status}): проверьте TFS_PAT и его права")
        if status == 404:
            raise TfsNotFoundError(f"Не найдено в TFS: {where}")
        if 300 <= status < 400:
            raise TfsError(f"TFS перенаправил запрос ({status}): проверьте TFS_URL")
        if status >= 400:
            raise TfsError(f"TFS вернул {status} на {where}{_snippet(body)}")
        try:
            data = json.loads(body)
        except ValueError:
            raise TfsError(
                f"Ответ TFS на {where} не JSON: возможно, неверный TFS_URL или вход через SSO"
            ) from None
        if not isinstance(data, dict):
            raise TfsError(f"Неожиданный ответ TFS на {where}")
        return data
