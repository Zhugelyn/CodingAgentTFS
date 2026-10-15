"""Приёмник вебхуков TFS (service hooks): будит наблюдателя (S21, D08).

Вебхук ничего не решает по содержимому. Событие work item (создан, изменён) запускает внеочередной
цикл `ario watch` с обычным запросом отбора, журналом взятых и лимитами; подделанное, но
авторизованное событие в худшем случае даёт лишний опрос. Событие PR с голосом «Waiting for
author» (-5) или «Rejected» (-10) просит доработку по замечаниям; есть ли новые замечания, решает
сбор из обсуждения PR, а не вебхук.

Защита: сервер на стандартной библиотеке; без секрета не стартует; Basic-авторизация, пароль
сравнивается за постоянное время; один путь и только POST; тело ограничено; ответ на любое
авторизованное корректное событие `200`, чтобы TFS не считал подписку сбойной и не повторял.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal
from urllib.parse import urlsplit

MAX_BODY_BYTES = 1_000_000
CONNECTION_TIMEOUT = 15.0  # секунд на чтение запроса: недочитанное тело не держит поток вечно
MIN_SECRET_LENGTH = 12
DEFAULT_PATH = "/ario/tfs"
WORK_ITEM_EVENTS = frozenset({"workitem.created", "workitem.updated"})
PULL_REQUEST_EVENTS = frozenset({"git.pullrequest.created", "git.pullrequest.updated"})
RETURN_VOTES = frozenset({-5, -10})  # Waiting for author, Rejected
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class Signal:
    kind: Literal["poll", "rework"]
    pr_id: int | None = None
    detail: str = ""


def classify(payload: Any) -> Signal | None:
    """Что просит событие: опрос TFS, доработку по PR или ничего (событие игнорируется)."""
    if not isinstance(payload, dict):
        return None
    event = payload.get("eventType")
    if not isinstance(event, str):
        return None
    if event in WORK_ITEM_EVENTS:
        return Signal("poll", detail=event)
    resource = payload.get("resource")
    if event in PULL_REQUEST_EVENTS and isinstance(resource, dict):
        pr_id = resource.get("pullRequestId")
        if isinstance(pr_id, bool) or not isinstance(pr_id, int):
            return None
        if resource.get("status") not in (None, "active"):
            return None
        reviewers = resource.get("reviewers")
        votes = [r.get("vote") for r in reviewers if isinstance(r, dict)] if reviewers else []
        if any(isinstance(v, int) and v in RETURN_VOTES for v in votes):
            return Signal("rework", pr_id, event)
    return None


def check_secret(secret: str | None) -> str:
    """Секрет обязателен и не короткий: приёмник может быть доступен из сети."""
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(
            f"ARIO_WEBHOOK_SECRET не задан или короче {MIN_SECRET_LENGTH} символов: "
            "вебхук без секрета не запускается"
        )
    return secret


def _basic_password(header: str | None) -> str | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return decoded.partition(":")[2]


class WebhookServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        secret: str,
        on_signal: Callable[[Signal], None],
        path: str = DEFAULT_PATH,
        log: Callable[[str], None] = print,
    ) -> None:
        self._secret = check_secret(secret).encode("utf-8")
        self._on_signal = on_signal
        self._path = path
        self._log = log
        self._server = ThreadingHTTPServer((host, port), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="webhook", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _authorized(self, header: str | None) -> bool:
        password = _basic_password(header)
        return password is not None and hmac.compare_digest(password.encode("utf-8"), self._secret)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            timeout = CONNECTION_TIMEOUT

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return  # адреса и пути не пишем: свои строки журнала ниже

            def _length(self) -> int:
                try:
                    return int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return -1

            def _drain(self) -> None:
                """Тело читаем до ответа: иначе закрытие соединения с непрочитанными данными даёт
                клиенту сброс вместо кода ответа. Слишком большое тело не читаем, а закрываем."""
                length = self._length()
                if 0 < length <= MAX_BODY_BYTES:
                    self.rfile.read(length)
                elif length != 0:
                    self.close_connection = True

            def _reply(self, status: int, body: str = "", *, challenge: bool = False) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                if challenge:
                    self.send_header("WWW-Authenticate", 'Basic realm="ario"')
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                length = self._length()
                if urlsplit(self.path).path != outer._path:  # query-строка пути не меняет
                    self._drain()
                    self._reply(404)
                    return
                if not outer._authorized(self.headers.get("Authorization")):
                    self._drain()
                    outer._log("Вебхук: запрос отклонён (авторизация)")
                    self._reply(401, challenge=True)
                    return
                if length < 0 or length > MAX_BODY_BYTES:
                    self.close_connection = True
                    self._reply(413)
                    return
                try:
                    payload = json.loads(self.rfile.read(length) or b"null")
                except ValueError:
                    self._reply(400)
                    return
                if not isinstance(payload, dict):
                    self._reply(400)
                    return
                signal = classify(payload)
                if signal is None:
                    event = _CONTROL.sub("?", str(payload.get("eventType")))[:60]
                    outer._log(f"Вебхук: событие {event} игнорируется")
                else:
                    outer._log(f"Вебхук: {signal.detail} → {signal.kind}")
                    try:
                        outer._on_signal(signal)
                    except Exception as e:  # честный 500: TFS повторит доставку
                        outer._log(f"Вебхук: сбой обработки сигнала ({type(e).__name__})")
                        self._reply(500)
                        return
                self._reply(200, '{"ok": true}')

            def _not_allowed(self) -> None:
                self._drain()
                self._reply(405)

            do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = _not_allowed  # noqa: N815

        return Handler
