# ArioCodingAgent

Харнесс из трёх агентов (analyst → developer → reviewer) на OpenHands SDK. Берёт тикет, находит место проблемы в коде Directum RX, вносит правку, собирает решение и проверяет её. Результат остаётся в репозитории RX на отдельной ветке тикета **без коммита и push**: их делает человек.

Оркестратор (`src/ario/orchestrator.py`) сам модель не использует: этапы, лимиты и состояние задаёт код. Агенты обмениваются только артефактами в `runs/<id>/`.

Все команды выполняются из папки проекта через `uv run ario ...`.

## 1. Подготовка (один раз)

```bash
uv sync
```

Создать `.env` по образцу `config/.env.example`. Для первого запуска нужны:

| Переменная | Что это |
|---|---|
| `RX_PATH` | Путь к репозиторию RX (рабочее дерево должно быть **чистым**, иначе тикет не будет взят) |
| `OPENROUTER_API_KEY` | Ключ OpenRouter |
| `ARIO_BUILD_COMMAND` | Штатная команда сборки RX (Directum Launcher: `do dirx build`) |
| `TFS_URL`, `TFS_PROJECT`, `TFS_PAT` | Доступ к TFS, если тикеты берутся оттуда (PAT с правом Work Items: Read) |

Остальные переменные (ветки, модели по ролям, запись на доску, графики) и их значения: [docs/guide/configuration.md](docs/guide/configuration.md). `.env` в репозиторий не попадает.

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

## 3. Если тикет упал

Продолжить с этапа, на котором остановились (после исправления причины: сеть, модель, дерево RX):

```bash
uv run ario resume 43702
```

Начать заново, откатить правки в RX: [docs/guide/restart.md](docs/guide/restart.md).

## Документация

| Тема | Файл |
|---|---|
| Все переменные `.env`, модели по ролям, несколько репозиториев, запись на доску, настройка агентов | [docs/guide/configuration.md](docs/guide/configuration.md) |
| Токены, кэш, цена, как читать `ario trace` | [docs/guide/metrics.md](docs/guide/metrics.md) |
| Графики в Phoenix, тексты запросов к модели | [docs/guide/phoenix.md](docs/guide/phoenix.md) |
| Перезапуск и откат тикета | [docs/guide/restart.md](docs/guide/restart.md) |
| Evals, тесты, линтер, типы | [docs/guide/development.md](docs/guide/development.md) |
