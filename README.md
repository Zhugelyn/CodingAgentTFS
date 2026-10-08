# ArioCodingAgent

Харнесс из трёх агентов (analyst → developer → reviewer) на OpenHands SDK. Берёт тикет, находит место проблемы в коде Directum RX, вносит правку, собирает решение и проверяет её. Результат остаётся в репозитории RX на отдельной ветке тикета **без коммита и push**: их делает человек.

Оркестратор (`src/ario/orchestrator.py`) сам модель не использует: этапы, лимиты и состояние задаёт код. Агенты обмениваются только артефактами в `runs/<id>/`.

Все команды выполняются из папки проекта через `uv run ario ...`.

## 1. Подготовка (один раз)

Установить зависимости:

```bash
uv sync
```

Создать `.env` по образцу `config/.env.example` и заполнить:

| Переменная | Что это |
|---|---|
| `RX_PATH` | Путь к репозиторию RX |
| `BASE_BRANCH` | Ветка, от которой создаётся ветка тикета (по умолчанию `master`) |
| `BRANCH_TEMPLATE` | Имя ветки тикета, обязателен `{id}`, например `antares/ai-{id}` |
| `ARIO_BUILD_COMMAND` | Штатная команда сборки RX (Directum Launcher: `do dirx build`) |
| `OPENROUTER_API_KEY` | Ключ OpenRouter |
| `LLM_MODEL` | Модель по умолчанию (формат LiteLLM, например `openrouter/qwen/qwen3.8-27b`) |
| `ANALYST_MODEL`, `DEVELOPER_MODEL`, `REVIEWER_MODEL` | Модель по ролям; пусто: берётся `LLM_MODEL` |
| `TFS_URL`, `TFS_PROJECT`, `TFS_PAT` | Доступ к TFS (PAT с правом Work Items: Read) |
| `TFS_WIQL`, `TFS_MAX_ITEMS` | Свой запрос отбора и лимит тикетов за один `fetch` (необязательно) |

`.env` в репозиторий не попадает. Секреты (`OPENROUTER_API_KEY`, `TFS_PAT`) нигде, кроме `.env`, не хранятся.

Перед запуском рабочее дерево RX должно быть **чистым**: иначе тикет не будет взят.

## 2. Запуск тикета

### Шаг 1. Положить тикет в `tickets/inbox/`

Из TFS по номеру:

```bash
uv run ario fetch --id 43702
```

Из TFS по запросу (Bug, тег `AI dev`, area `SmartInstruments\Antares`, не закрытые, до `TFS_MAX_ITEMS` новых):

```bash
uv run ario fetch
```

Показать, что будет взято, ничего не записывая:

```bash
uv run ario fetch --dry-run
```

Вручную: файл формата D01 `tickets/inbox/<id>.md` (пример: `evals/cases/90101.md`). Тикет, уже лежащий в любой папке состояний, повторно не скачивается и не перезаписывается.

### Шаг 2. Запустить поток

```bash
uv run ario run 43702
```

Порядок: analyst (локализует проблему и проверяет, хватает ли информации) → developer (правки и сборка) → reviewer. При `reject` ревьюера developer дорабатывает один раз, затем второе ревью. Прогон идёт от 1 до 10 минут.

Коды выхода:

| Код | Значение |
|---|---|
| 0 | Тикет готов (`done`) |
| 1 | Нужен человек (`needs-human`), причина в `tickets/needs-human/<id>.comments.md` |
| 2 | Ошибка настройки или TFS |
| 3 | Тикет не взят (например, RX не чистый) |

### Шаг 3. Посмотреть результат

Правка лежит в RX на ветке тикета, без коммита:

```bash
git -C <путь к RX> diff
```

Коммит и push выполняет человек.

Где что лежит:

| Что | Где |
|---|---|
| Тикет и комментарии агентов | `tickets/{inbox,in-progress,done,needs-human}/<id>.md` и `<id>.comments.md` |
| Результаты этапов | `runs/<id>/`: `analysis.md`, `development-N.md`, `review-N.md`, `state.json` |
| Полная трасса | `traces/<id>/events.jsonl` |

### Шаг 4. Разобрать, что делали агенты

Сводка по этапам (шаги, токены, доля кэша, вердикты):

```bash
uv run ario trace 43702
```

Пошаговая лента вызовов одного этапа (`analysis`, `development`, `review`):

```bash
uv run ario trace 43702 --steps --stage development
```

Ключ `--full` добавляет задачу, мысли модели и длинные тексты.

## 3. Если тикет упал или нужен перезапуск

Продолжить с этапа, на котором остановились (после исправления причины: сеть, модель, дерево RX):

```bash
uv run ario resume 43702
```

Начать тикет заново (команды сброса нет): вернуть `tickets/<папка>/<id>.md` в `tickets/inbox/`, убрать `runs/<id>` и `traces/<id>` (удобно переносить в `runs/_archive/` и `traces/_archive/`), откатить правки в RX и снова запустить `ario run`.

Откатить правки в RX:

```bash
git -C <путь к RX> restore .
```

После этого переключиться на свою рабочую ветку и при желании удалить ветку тикета (`git -C <путь к RX> branch -D <ветка тикета>`).

## 4. Набор тикетов с ожиданиями (evals)

Прогнать все кейсы из `evals/cases/` по ожиданиям `evals/expected.yaml`:

```bash
uv run ario eval
```

Отдельные кейсы:

```bash
uv run ario eval --only 90102 90103
```

Отчёт с метриками (время, шаги, токены, повторы запросов) пишется в `evals/runs/<время>/report.md`. Кейсы с правками кода требуют чистого дерева RX; после такого кейса откатите RX.

## 5. Проверка самого проекта

Тесты идут на заглушках модели, без сети и без расхода токенов:

```bash
uv run pytest
```

Линтер и формат:

```bash
uv run ruff check src tests
```

```bash
uv run ruff format .
```

Типы:

```bash
uv run mypy
```

## 6. Настройка поведения агентов

| Что менять | Где |
|---|---|
| Роли агентов | `agents/<роль>/AGENTS.md` и `agents/<роль>/tools.yaml` |
| Общие правила RX | `AGENTS.md` в корне |
| Навыки по RX | `skills/<имя>/SKILL.md` |
| Лимиты шагов, времени, контекста, запрещённые пути, правила injection | `config/guardrails.yaml` |

Не коммитить: `.env`, `runs/`, `traces/`, содержимое `tickets/*/`.
