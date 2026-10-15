"""Автоматический режим: опрос TFS и последовательная обработка очереди (S18, D08).

Очередь — папка `tickets/inbox/`: опрос кладёт туда новые тикеты, обработчик берёт самый старый и
ведёт его по потоку, затем следующий. Ничего нового в хранилищах нет: состояние тикета, повтор
после падения и защита от двойной обработки остаются теми же, что у `ario run`.

Правила цикла:
- сбой опроса (TFS недоступен) не мешает обработать то, что уже лежит в очереди;
- тикет, который не взяли (например, в клоне RX чужие правки), оставляет очередь до следующего
  цикла: остальные тикеты упёрлись бы в ту же причину;
- любой тикет за цикл берётся не более одного раза;
- остановка: между тикетами (текущий дорабатывает до конца), ожидание прерывается сразу.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ario.fsutil import atomic_write_text
from ario.orchestrator import TicketNotTakenError
from ario.store import TicketStore

WAIT_SLICE = (
    1.0  # секунд: на Windows долгое ожидание не прерывается Ctrl+C, ждём короткими отрезками
)


class SeenLedger:
    """Журнал тикетов, которые наблюдатель уже брал: переживает чистку папок состояний.

    Без него убранный из `done/` файл возвращал бы тикет в очередь (запрос TFS всё ещё его
    показывает) и тот запускался бы заново. Вернуть тикет вручную: `ario fetch --id N`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> set[str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {str(item) for item in data} if isinstance(data, list) else set()

    def add(self, ids: Iterable[str]) -> None:
        known = self.load()
        merged = known | {str(i) for i in ids}
        if merged != known:
            atomic_write_text(self._path, json.dumps(sorted(merged), ensure_ascii=False))


Fetch = Callable[[], int]  # забирает новые тикеты из TFS, возвращает сколько взято
Process = Callable[[str], str]  # ведёт тикет по потоку, возвращает исход (`done`, `needs-human`)


@dataclass
class CycleResult:
    fetched: int | None = None  # None: опрос не удался
    processed: list[tuple[str, str]] = field(default_factory=list)
    blocked: str | None = None  # причина, по которой очередь остановлена до следующего цикла


class Watcher:
    def __init__(
        self,
        *,
        store: TicketStore,
        fetch: Fetch,
        process: Process,
        interval_seconds: float,
        log: Callable[[str], None] = print,
        mask: Callable[[str], str] = lambda text: text,
        stop: threading.Event | None = None,
    ) -> None:
        self._store = store
        self._fetch = fetch
        self._process = process
        self._interval = interval_seconds
        self._log = log
        self._mask = mask
        self.stop = stop or threading.Event()

    def cycle(self) -> CycleResult:
        """Один оборот: опрос, затем обработка очереди."""
        result = CycleResult()
        try:
            result.fetched = self._fetch()
            self._log(f"Опрос TFS: новых тикетов {result.fetched}")
        except Exception as e:  # доска недоступна: очередь всё равно разбираем
            self._log(f"Опрос TFS не удался: {self._mask(f'{type(e).__name__}: {e}')}")
        self._drain(result)
        return result

    def _drain(self, result: CycleResult) -> None:
        tried: set[str] = set()
        while not self.stop.is_set():
            # сначала тикеты, оставшиеся в работе после сбоя (живую блокировку чужого процесса
            # оркестратор отклонит), затем очередь
            queue = [*self._store.list_ids("in-progress"), *self._store.list_ids("inbox")]
            pending = [t for t in queue if t not in tried]
            if not pending:
                return
            ticket_id = pending[0]
            tried.add(ticket_id)
            self._log(f"Беру тикет {ticket_id} (в очереди ещё {len(pending) - 1})")
            try:
                outcome = self._process(ticket_id)
            except TicketNotTakenError as e:
                result.blocked = self._mask(e.reason)
                self._log(f"Тикет {ticket_id} не взят: {result.blocked}. Повторю в следующем цикле")
                return
            except Exception as e:  # один тикет не должен останавливать наблюдателя
                self._log(f"Тикет {ticket_id}: ошибка {self._mask(f'{type(e).__name__}: {e}')}")
                continue
            result.processed.append((ticket_id, outcome))

    def run(self, *, once: bool = False) -> None:
        """Работает, пока не попросят остановиться; `once`: один цикл и выход."""
        while True:
            self.cycle()
            if once or self.stop.is_set():
                return
            self._log(f"Следующий опрос через {self._interval / 60:g} мин (Ctrl+C: остановить)")
            if self._sleep():
                return

    def _sleep(self) -> bool:
        """Ждёт интервал короткими отрезками; `True`, если попросили остановиться."""
        end = time.monotonic() + self._interval
        while (left := end - time.monotonic()) > 0:
            if self.stop.wait(min(left, WAIT_SLICE)):
                return True
        return self.stop.is_set()
