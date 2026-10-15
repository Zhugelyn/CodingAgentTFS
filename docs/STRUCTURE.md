# Карта проекта: где что лежит

Одна страница, чтобы не искать по папкам. Подробности по каждой теме — в ссылках.

## Что это

Харнесс из трёх агентов (analyst → developer → reviewer) на OpenHands SDK. Берёт тикет с доски TFS,
находит место проблемы в коде, правит, собирает, проверяет, отправляет ветку и открывает PR.
Оркестратор (`src/ario/orchestrator.py`) сам модель не использует: этапы, лимиты и состояние
задаёт код. Агенты обмениваются только файлами в `runs/<id>/`.

## Поток тикета

```
TFS (доска) ─ ario fetch / ario watch ─► tickets/inbox/<id>.md
   ─► оркестратор: analysis ─► development ─► review ─► (reject: development-2 ─► review-2) ─► publish
   ─► ветка + PR в репозитории кода;  на доске: состояния и комментарии (нужен человек, готово)
```

Папки состояний тикета: `inbox/` → `in-progress/` → `done/` | `needs-human/`.

## Код (`src/ario/`)

| Что | Файлы |
|---|---|
| Команды | `cli.py` (run, watch, fetch, resume, trace, eval, export-otel) |
| Ядро потока | `orchestrator.py` (машина состояний), `pipeline.py` (порядок этапов), `stage_names.py`, `state.py` (`runs/<id>/state.json`), `store.py` (папки тикета), `watch.py` (опрос и очередь) |
| Агенты | `agents/`: `analyst.py`, `developer.py`, `reviewer.py` (этапы), `backend.py` (цикл агента на OpenHands SDK), `readonly_tools.py` и `dev_tools.py` (инструменты), `prompts.py`, `spec.py` (AGENTS.md, навыки), `verdicts.py` (формат вердиктов) |
| Инструменты | `tools/`: `git.py` (без commit и push), `build.py`, `edit.py`, `proc.py` |
| Репозитории | `repos.py` (профили из `repos.yaml`: путь, ветки, сборка, знания, публикация), `branches.py` |
| TFS и доска | `tfs/` (`client.py`, `convert.py`, `fetch.py`), `ticket.py` (формат тикета), `ticket_system.py` (запись на доску), `reporting.py` (состояния и комментарии) |
| Публикация | `publish.py` (коммит, push без force, PR) |
| Защита | `guardrails/` (лимиты, пути, injection), `llm_guard.py` (предел на запрос к модели) |
| Наблюдаемость | `trace.py` (`traces/<id>/events.jsonl`), `trace_report.py` (`ario trace`), `otel_export.py` и `llm_telemetry.py` (Phoenix), `evals.py` (`ario eval`) |
| Общее | `config.py` (настройки из `.env`), `errors.py`, `fsutil.py` |

## Данные при работе (не в git)

| Где | Что |
|---|---|
| `tickets/{inbox,in-progress,done,needs-human}/` | тикеты `<id>.md` и комментарии агентов `<id>.comments.md` |
| `runs/<id>/` | артефакты этапов: `analysis.*`, `development-N.*`, `review-N.*`, `publish.json`, `state.json` |
| `traces/<id>/events.jsonl` | полная трасса прогона |
| `<каталог данных>/watch-seen.json` | журнал тикетов, которые уже брал `ario watch` |
| `.phoenix/` | локальный Phoenix (графики) |
| `evals/runs/` | отчёты `ario eval` |

## Настройки

| Файл | Зачем |
|---|---|
| `.env` (образец `config/.env.example`) | ключи, пути, модели по ролям, флаги `TFS_WRITE`, `ARIO_PUBLISH` |
| `config/guardrails.yaml` | лимиты шагов, времени и контекста, запрещённые пути, правила injection |
| `config/repos.yaml` (образец `.example`, путь в `ARIO_REPOS`) | профили репозиториев и публикация |
| `config/ticket-flow.yaml` (образец `.example`, путь в `ARIO_TICKET_FLOW`) | какое состояние ставить на доске по ходу работы |

## Содержимое для агентов (не инструкции разработчику)

`AGENTS.md` в корне, `agents/<роль>/` (AGENTS.md и tools.yaml роли), `skills/` (знания о RX).
Это то, что агенты читают при работе, правит их заказчик.

## Тесты и проверки

`tests/test_<модуль>.py`: на заглушках модели, без сети (`uv run pytest`). `evals/`: тикеты с
ожиданиями (`cases/`, `expected.yaml`) для `ario eval`. Линтер и типы: `uv run ruff check src tests`,
`uv run mypy`.

## Документация

| Что | Где | В git |
|---|---|---|
| Быстрый старт | `README.md` | да |
| Руководства (настройка, метрики, Phoenix, перезапуск, тесты) | `docs/guide/` | да |
| Эта карта | `docs/STRUCTURE.md` | да |
| Особенности OpenHands SDK | `docs/spike.md` | да |
| Перенос в LibreChat | `docs/librechat/` | да |
| Правила разработки для Claude | `CLAUDE.md` | да |
| Журнал решений (главнее остального) | `memory.md` | нет |
| Решения D* и истории S*, карта `MAP.md` | `docs/story/` | нет |
| Базовый план MVP (историческая запись) | `docs/PLAN.md` | нет |

Правило этапа: тесты зелёные, `/code-review`, апрув заказчика, запись в `memory.md`, коммит.
