"""Клиент Azure DevOps Server (TFS) по REST API: чтение тикетов и запись на доску.

Чтение: GET work item, комментариев и состояний типа, POST запроса WIQL (он ничего не меняет).
Запись (S16): комментарий и смена состояния. Запись не повторяется при обрыве и 5xx (запрос мог
дойти, и комментарий задвоится), повторяется только 429. Состояние меняется с проверкой ревизии:
если тикет за это время изменил человек, TFS откажет, и чужая правка не перезапишется.
Авторизация по PAT (Basic с пустым именем). Токен нигде не попадает в сообщения об ошибках.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import quote

import httpx

API_VERSION = "7.1"
COMMENTS_API_VERSION = "7.1-preview.4"  # комментарии в Server 2022.2 только как preview
MAX_RESPONSE_BYTES = 5_000_000
MAX_COMMENT_PAGES = 10
COMMENTS_PAGE_SIZE = 200
DEFAULT_TIMEOUT = 30.0
BOARD_TIMEOUT = (
    10.0  # запись на доску идёт внутри прогона: недоступная доска не должна его тормозить
)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
WRITE_RETRY_STATUSES = frozenset(
    {429}
)  # запрос точно не выполнен; остальное для записи не повторяем
JSON_PATCH = "application/json-patch+json"
BRIEF_FIELDS = "System.State,System.WorkItemType,System.CreatedBy,System.Title"
DEFAULT_RETRY_WAIT = (1.0, 3.0, 8.0)  # паузы между повторами, их число = число повторов


class TfsError(RuntimeError):
    """Сбой обращения к TFS (сеть, статус, неверный ответ)."""


class TfsAuthError(TfsError):
    """TFS отклонил PAT (401/403)."""


class TfsNotFoundError(TfsError):
    """Нет такого проекта или work item (404)."""


class TfsConflictError(TfsError):
    """Тикет изменился с момента чтения (проверка ревизии не прошла: 409/412)."""


def _snippet(body: bytes) -> str:
    text = " ".join(body[:200].decode("utf-8", "replace").split())
    return f": {text}" if text else ""


Kind = Literal["board", "git"]


class TfsClient:
    """Клиент одного сервера. `kind` закрепляет его роль: сервер доски (work items) или сервер
    кода (pull request). Номер тикета с доски на сервере кода означает совсем другой элемент,
    поэтому методы чужой роли не выполняются, а не отправляются на не тот сервер."""

    def __init__(
        self,
        base_url: str,
        project: str,
        pat: str,
        *,
        kind: Kind = "board",
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry_wait: tuple[float, ...] = DEFAULT_RETRY_WAIT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("TFS_URL должен начинаться с https:// (или http://)")
        if not pat:
            raise ValueError("Не задан TFS_PAT")
        self._kind = kind
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
        return f"TfsClient({self._kind}, {self._http.base_url!s})"

    def _require(self, kind: Kind, action: str) -> None:
        if self._kind != kind:
            raise TfsError(
                f"{action}: клиент сервера «{self._kind}», а действие относится к серверу «{kind}»"
            )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TfsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- публичные методы ---------------------------------------------------------------------
    def wiql(self, query: str, top: int) -> list[int]:
        """Выполняет запрос WIQL и возвращает id work items в порядке запроса (не больше `top`)."""
        self._require("board", "запрос WIQL")
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
        self._require("board", "чтение work item")
        return self._request(
            "GET",
            f"{self._project}/_apis/wit/workitems/{int(item_id)}",
            {"$expand": "all", "api-version": API_VERSION},
        )

    def comments(self, item_id: int) -> list[dict[str, Any]]:
        """Комментарии work item в порядке TFS (новые первыми), с постраничным чтением."""
        self._require("board", "чтение комментариев")
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

    def work_item_brief(self, item_id: int) -> dict[str, Any]:
        """Только нужные для записи поля (состояние, тип, автор) и ревизия `rev`."""
        self._require("board", "чтение work item")
        return self._request(
            "GET",
            f"{self._project}/_apis/wit/workitems/{int(item_id)}",
            {"fields": BRIEF_FIELDS, "api-version": API_VERSION},
        )

    def type_states(self, item_type: str) -> list[str]:
        """Имена состояний типа work item (для проверки карты «этап → состояние»)."""
        self._require("board", "чтение состояний")
        data = self._request(
            "GET",
            f"{self._project}/_apis/wit/workitemtypes/{quote(item_type, safe='')}/states",
            {"api-version": API_VERSION},
        )
        items = data.get("value")
        if not isinstance(items, list):
            raise TfsError("Ответ состояний без списка value")
        return [str(i["name"]) for i in items if isinstance(i, dict) and i.get("name")]

    def add_comment(self, item_id: int, html: str) -> int:
        """Добавляет комментарий (HTML) и возвращает его id. Не повторяется при обрыве."""
        self._require("board", "комментарий к work item")
        data = self._request(
            "POST",
            f"{self._project}/_apis/wit/workItems/{int(item_id)}/comments",
            {"api-version": COMMENTS_API_VERSION},
            payload={"text": html},
            idempotent=False,
        )
        comment_id = data.get("id")
        if not isinstance(comment_id, int) or isinstance(comment_id, bool):
            raise TfsError("Ответ на добавление комментария без числового id")
        return comment_id

    def set_state(self, item_id: int, state: str, rev: int) -> None:
        """Меняет состояние при условии, что ревизия тикета всё ещё `rev` (иначе конфликт)."""
        self._require("board", "смена состояния work item")
        patch = [
            {"op": "test", "path": "/rev", "value": int(rev)},
            {"op": "add", "path": "/fields/System.State", "value": state},
        ]
        self._request(
            "PATCH",
            f"{self._project}/_apis/wit/workitems/{int(item_id)}",
            {"api-version": API_VERSION},
            payload=patch,
            content_type=JSON_PATCH,
            idempotent=False,
            conflict=True,
        )

    # --- git: pull request (S17) --------------------------------------------------------------
    def find_pull_request(self, repository: str, source: str, target: str) -> dict[str, Any] | None:
        """Активный PR из ветки `source` в `target` (его повторно не создаём), если он есть."""
        self._require("git", "поиск pull request")
        data = self._request(
            "GET",
            f"{self._project}/_apis/git/repositories/{quote(repository, safe='')}/pullrequests",
            {
                "searchCriteria.sourceRefName": f"refs/heads/{source}",
                "searchCriteria.targetRefName": f"refs/heads/{target}",
                "searchCriteria.status": "active",
                "api-version": API_VERSION,
            },
        )
        found = data.get("value")
        if not isinstance(found, list):
            raise TfsError("Ответ поиска PR без списка value")
        return next((pr for pr in found if isinstance(pr, dict)), None)

    def create_pull_request(
        self,
        repository: str,
        source: str,
        target: str,
        title: str,
        description: str,
        *,
        draft: bool = False,
    ) -> dict[str, Any]:
        """Открывает PR. Не повторяется при обрыве: дубль PR хуже ошибки."""
        self._require("git", "создание pull request")
        data = self._request(
            "POST",
            f"{self._project}/_apis/git/repositories/{quote(repository, safe='')}/pullrequests",
            {"api-version": API_VERSION},
            payload={
                "sourceRefName": f"refs/heads/{source}",
                "targetRefName": f"refs/heads/{target}",
                "title": title,
                "description": description,
                "isDraft": draft,
            },
            idempotent=False,
        )
        return data

    def pull_request_threads(self, repository: str, pr_id: int) -> list[dict[str, Any]]:
        """Ветки обсуждения PR (комментарии ревьюеров) для доработки по замечаниям."""
        self._require("git", "чтение обсуждения pull request")
        data = self._request(
            "GET",
            f"{self._project}/_apis/git/repositories/{quote(repository, safe='')}"
            f"/pullrequests/{int(pr_id)}/threads",
            {"api-version": API_VERSION},
        )
        threads = data.get("value")
        if not isinstance(threads, list):
            raise TfsError("Ответ обсуждения PR без списка value")
        return [t for t in threads if isinstance(t, dict)]

    def add_pull_request_comment(self, repository: str, pr_id: int, text: str) -> None:
        """Новая закрытая ветка в PR с итогом доработки. Не повторяется при обрыве."""
        self._require("git", "комментарий к pull request")
        self._request(
            "POST",
            f"{self._project}/_apis/git/repositories/{quote(repository, safe='')}"
            f"/pullrequests/{int(pr_id)}/threads",
            {"api-version": API_VERSION},
            payload={
                "comments": [{"parentCommentId": 0, "content": text, "commentType": "text"}],
                "status": "closed",
            },
            idempotent=False,
        )

    # --- транспорт ----------------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        payload: Any = None,
        *,
        content_type: str | None = None,
        idempotent: bool = True,
        conflict: bool = False,
    ) -> dict[str, Any]:
        retry_statuses = RETRY_STATUSES if idempotent else WRITE_RETRY_STATUSES
        attempts = len(self._retry_wait) + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            body_args: dict[str, Any] = {"json": payload}
            if content_type is not None:  # json-patch: тело то же, тип другой
                body_args = {
                    "content": json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "headers": {"Content-Type": content_type},
                }
            try:
                with self._http.stream(method, path, params=params, **body_args) as response:
                    body = self._read_limited(response)
            except httpx.TransportError as e:  # таймаут, обрыв, ошибка TLS
                if last or not idempotent:  # запись могла дойти: повтор задвоил бы её
                    raise TfsError(f"Нет связи с TFS ({type(e).__name__})") from None
                self._sleep(self._retry_wait[attempt])
                continue
            if response.status_code in retry_statuses and not last:
                self._sleep(self._retry_after(response, self._retry_wait[attempt]))
                continue
            return self._parse(response, body, path, conflict)
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
    def _parse(
        response: httpx.Response, body: bytes, path: str, conflict: bool = False
    ) -> dict[str, Any]:
        status = response.status_code
        where = path.split("?")[0]
        if status in (401, 403):
            raise TfsAuthError(f"TFS отклонил доступ ({status}): проверьте TFS_PAT и его права")
        if status == 404:
            raise TfsNotFoundError(f"Не найдено в TFS: {where}")
        if conflict and status in (409, 412):  # только PATCH с проверкой ревизии
            raise TfsConflictError(f"Тикет изменён с момента чтения ({status}): {where}")
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
