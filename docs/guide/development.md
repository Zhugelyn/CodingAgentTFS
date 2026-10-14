# Evals и проверка проекта

## Набор тикетов с ожиданиями (evals)

Прогнать все кейсы из `evals/cases/` по ожиданиям `evals/expected.yaml`:

```bash
uv run ario eval
```

Отдельные кейсы:

```bash
uv run ario eval --only 90102 90103
```

Отчёт с метриками (время, шаги, токены, повторы запросов) пишется в `evals/runs/<время>/report.md`. Кейсы с правками кода требуют чистого дерева RX; после такого кейса откатите RX.

## Проверка самого проекта

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
