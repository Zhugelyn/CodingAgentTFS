"""Заглушка модели: подменяет litellm.completion заготовленными ответами."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from typing import Any

import pytest
from litellm import ModelResponse

_ids = itertools.count(1)


def response(
    tool: str | None = None,
    args: dict[str, Any] | None = None,
    *,
    content: str | None = None,
    prompt: int = 1000,
    completion: int = 50,
) -> ModelResponse:
    calls = None
    if tool is not None:
        calls = [
            {
                "id": f"call_{next(_ids)}",
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args or {})},
            }
        ]
    return ModelResponse(
        id=f"r{next(_ids)}",
        model="stub",
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls" if calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": calls},
            }
        ],
        usage={
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    )


def finish(payload: dict[str, Any] | str, *, prompt: int = 1000) -> ModelResponse:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return response("finish", {"message": text}, prompt=prompt)


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[dict[str, Any]]]:
    """Подставляет заготовленные ответы вместо litellm.completion; возвращает журнал запросов."""

    def install(*items: ModelResponse | Exception) -> list[dict[str, Any]]:
        queue = list(items)
        calls: list[dict[str, Any]] = []

        def fake(**kwargs: Any) -> ModelResponse:
            calls.append(kwargs)
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", fake)
        return calls

    return install
