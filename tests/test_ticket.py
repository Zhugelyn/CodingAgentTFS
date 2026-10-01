from __future__ import annotations

import pytest

from ario.ticket import TicketError, parse_ticket
from tests.conftest import TICKET_TEXT


def test_parse_full_ticket() -> None:
    t = parse_ticket(TICKET_TEXT)
    assert t.id == "43031"
    assert t.type == "Bug"
    assert t.area == "SmartInstruments\\Antares"
    assert t.priority == 2
    assert t.tags == ["Applied"]
    assert t.description.startswith("ПОРЯДОК ДЕЙСТВИЙ")
    assert [c.author for c in t.discussion] == [
        "Галиева Эльвира (Galieva_EI)",
        "Бочкарев Дмитрий (Bochkarev_DS)",
    ]
    assert t.discussion[0].date == "2025-09-24"
    assert t.discussion[0].text == "Решили оставить как есть."


def test_minimal_ticket_without_optional_sections() -> None:
    t = parse_ticket("---\nid: 1\ntype: Task\ntitle: T\n---\n## Description\nD\n")
    assert t.discussion == []
    assert t.acceptance_criteria is None
    assert t.source == "folder"


@pytest.mark.parametrize(
    "text",
    [
        "## Description\nнет front matter",
        "---\nid: 1\ntype: Task\n---\n## Description\nD\n",  # нет title
        "---\nid: 1\ntype: Task\ntitle: T\n---\nбез секции Description\n",
        "---\nid: 1\ntype: Task\ntitle: T\n---\n## Description\nD\n## Discussion\n### без даты\n",
    ],
)
def test_invalid_ticket(text: str) -> None:
    with pytest.raises(TicketError):
        parse_ticket(text)


@pytest.mark.parametrize(
    "front",
    ["priority: high", "tags: foo", "tags: [unclosed", "description: дубль"],
)
def test_bad_front_matter_values_raise_ticket_error(front: str) -> None:
    text = f"---\nid: 1\ntype: Task\ntitle: T\n{front}\n---\n## Description\nD\n"
    if front.startswith("description"):
        assert parse_ticket(text).description == "D"  # лишний ключ игнорируется
    else:
        with pytest.raises(TicketError):
            parse_ticket(text)


def test_unknown_h2_heading_stays_inside_description() -> None:
    t = parse_ticket(
        "---\nid: 1\ntype: Bug\ntitle: T\n---\n## Description\nШаги\n## ПОЯСНЕНИЕ\nважный текст\n"
    )
    assert "важный текст" in t.description
