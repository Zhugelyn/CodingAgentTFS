# ruff: noqa: E501  (длинные строки JSON в тестах вердиктов)
from __future__ import annotations

import json

import pytest

from ario.agents.verdicts import (
    VerdictError,
    extract_json,
    failure_text,
    parse_analysis,
    render_analysis_md,
)

SUITABLE = {
    "decision": "suitable",
    "plan": [{"step": "Добавить проверку", "files": ["src/A.cs"]}],
    "risk": "medium",
    "risk_reason": "Публичная функция",
}


def test_parse_bare_json() -> None:
    a = parse_analysis(json.dumps(SUITABLE))
    assert a.decision == "suitable"
    assert a.plan[0].files == ["src/A.cs"]
    assert a.risk == "medium"


def test_parse_fenced_json_with_text_around() -> None:
    text = f"Вот вердикт:\n```json\n{json.dumps(SUITABLE, ensure_ascii=False)}\n```\nГотово."
    assert parse_analysis(text).decision == "suitable"


def test_parse_json_embedded_in_text_without_fence() -> None:
    text = f"Результат: {json.dumps(SUITABLE)} конец"
    assert parse_analysis(text).decision == "suitable"


def test_extra_keys_are_ignored() -> None:
    data = {**SUITABLE, "confidence": 0.9}
    assert parse_analysis(json.dumps(data)).decision == "suitable"


@pytest.mark.parametrize(
    "text",
    [
        "просто текст без json",
        '{"decision": "suitable", "plan": [',  # оборван
        "[1, 2, 3]",  # не объект
        '{"decision": "maybe"}',  # значение вне списка
        '{"decision": "suitable"}',  # нет плана
        '{"decision": "unsuitable"}',  # нет причины
        '{"decision": "needs_info", "reason": "x"}',  # нет missing_info
        '{"decision": "suitable", "plan": [{"step": ""}]}',  # пустой шаг
    ],
)
def test_invalid_verdicts(text: str) -> None:
    with pytest.raises(VerdictError):
        parse_analysis(text)


def test_error_message_names_the_problem() -> None:
    with pytest.raises(VerdictError, match="plan"):
        parse_analysis('{"decision": "suitable"}')


def test_extract_json_returns_first_object() -> None:
    assert extract_json('a {"x": 1} b {"y": 2}') == {"x": 1}


def test_render_suitable_markdown() -> None:
    md = render_analysis_md(parse_analysis(json.dumps(SUITABLE)))
    assert md.startswith("# Анализ: Подходит")
    assert "1. Добавить проверку" in md
    assert "`src/A.cs`" in md
    assert "Риск: средний" in md


def test_render_and_failure_text_for_needs_info() -> None:
    a = parse_analysis(
        json.dumps(
            {"decision": "needs_info", "reason": "Нет шагов", "missing_info": ["Шаги", "Версия"]}
        )
    )
    assert "Что нужно дополнить" in render_analysis_md(a)
    text = failure_text(a)
    assert text.startswith("Нет шагов")
    assert "- Шаги" in text
    assert "- Версия" in text


# --- вердикт reviewer ---
from ario.agents.verdicts import parse_review, render_review_md  # noqa: E402


def test_review_approve_and_reject_parse() -> None:
    approve = parse_review(
        '{"verdict": "approve", "summary": "Всё верно", "issues": '
        '[{"severity": "minor", "file": "a.cs", "description": "стиль"}]}'
    )
    assert approve.verdict == "approve"
    reject = parse_review(
        '```json\n{"verdict": "reject", "summary": "Есть ошибка", "issues": '
        '[{"severity": "major", "file": "a.cs", "line": 7, "description": "неверное условие"}]}\n```'
    )
    assert reject.issues[0].line == 7


@pytest.mark.parametrize(
    "text",
    [
        '{"verdict": "reject", "summary": "плохо", "issues": []}',  # нет замечаний
        '{"verdict": "reject", "summary": "плохо", "issues": [{"severity": "minor", "description": "x"}]}',
        '{"verdict": "approve", "summary": "ок", "issues": [{"severity": "blocker", "description": "x"}]}',
        '{"verdict": "maybe", "summary": "?"}',
        '{"verdict": "approve"}',  # нет summary
        '{"verdict": "approve", "summary": "ок", "issues": [{"severity": "major"}]}',  # нет описания
    ],
)
def test_invalid_reviews(text: str) -> None:
    with pytest.raises(VerdictError):
        parse_review(text)


def test_review_markdown_lists_issues_with_location() -> None:
    md = render_review_md(
        parse_review(
            '{"verdict": "reject", "summary": "Нужна доработка", "issues": ['
            '{"severity": "major", "file": "src/A.cs", "line": 12, "description": "Нет проверки null"},'
            '{"severity": "minor", "description": "Опечатка"}]}'
        )
    )
    assert md.startswith("# Ревью: отклонено")
    assert "**существенное** `src/A.cs:12`: Нет проверки null" in md
    assert "**мелкое**: Опечатка" in md
