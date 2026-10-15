"""Замечания из обсуждения pull request для доработки (S19, D08).

Берутся ветки обсуждения в статусе «active» или «pending» (закрытые и исправленные ревьюер уже
принял), без системных, удалённых и собственных ответов агента. Чтобы повторная доработка не
брала те же замечания заново, запоминается номер последнего учтённого комментария каждой ветки
(`runs/<id>/rework-handled.json`): ветка попадает в доработку снова, только если в ней появился
новый комментарий.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ario.fsutil import atomic_write_text

HANDLED_FILE = "rework-handled.json"
OPEN_STATUSES = frozenset({"active", "pending"})
MAX_COMMENT_CHARS = 2000


def find_ticket_by_pr(runs_dir: Path, pr_id: int) -> str | None:
    """Тикет, для которого открыт PR `pr_id` (по `runs/<id>/publish.json`); `None`, если чужой."""
    if not runs_dir.is_dir():
        return None
    for path in sorted(runs_dir.glob("*/publish.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("pr_id") == pr_id:
            return path.parent.name
    return None


def load_handled(run_dir: Path) -> dict[str, int]:
    try:
        data = json.loads((run_dir / HANDLED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, int) and not isinstance(v, bool)}


def save_handled(run_dir: Path, handled: dict[str, int]) -> None:
    merged = {**load_handled(run_dir), **handled}
    atomic_write_text(run_dir / HANDLED_FILE, json.dumps(merged, ensure_ascii=False, indent=2))


def _author(comment: dict[str, Any]) -> str:
    author = comment.get("author")
    name = author.get("displayName") if isinstance(author, dict) else None
    return " ".join(str(name or "ревьюер").split())


def _place(thread: dict[str, Any]) -> str:
    context = thread.get("threadContext")
    if not isinstance(context, dict) or not context.get("filePath"):
        return "общее замечание"
    start = context.get("rightFileStart") or context.get("leftFileStart")
    line = start.get("line") if isinstance(start, dict) else None
    where = str(context["filePath"]).lstrip("/")
    return f"{where}, строка {line}" if isinstance(line, int) else where


def collect_remarks(
    threads: list[dict[str, Any]],
    pr_id: int,
    handled: dict[str, int],
    *,
    agent_mark: str,
) -> tuple[str, dict[str, int]]:
    """Текст замечаний из новых веток обсуждения и обновлённый журнал учтённых комментариев.

    `agent_mark` — метка собственных ответов агента (такие комментарии замечаниями не считаются).
    Пустой текст: новых замечаний нет.
    """
    blocks: list[str] = []
    updated: dict[str, int] = {}
    for thread in threads:
        status = str(thread.get("status") or "active")
        if thread.get("isDeleted") or status not in OPEN_STATUSES:
            continue
        thread_id = str(thread.get("id"))
        comments = [
            c
            for c in thread.get("comments") or []
            if isinstance(c, dict)
            and not c.get("isDeleted")
            and c.get("commentType") != "system"
            and str(c.get("content") or "").strip()
            and not str(c["content"]).lstrip().startswith(agent_mark)
        ]
        ids = [c["id"] for c in comments if isinstance(c.get("id"), int)]
        if not comments or (ids and max(ids) <= handled.get(thread_id, 0)):
            continue
        lines = [f"### Замечание {len(blocks) + 1}: {_place(thread)}"]
        for comment in comments:
            text = " ".join(str(comment["content"]).split())[:MAX_COMMENT_CHARS]
            lines.append(f"- {_author(comment)}: {text}")
        blocks.append("\n".join(lines))
        if ids:
            updated[thread_id] = max(ids)
    if not blocks:
        return "", {}
    header = f"Замечания из обсуждения pull request {pr_id}:"
    return header + "\n\n" + "\n\n".join(blocks), updated
