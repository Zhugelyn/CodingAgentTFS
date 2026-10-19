"""Guardrails: лимиты, injection-сканер, границы инструментов."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

from ario.guardrails.boundaries import PathGuard, clean_env
from ario.guardrails.config import FileRules, GuardrailsConfig, load_config
from ario.guardrails.injection import Finding, InjectionScanner
from ario.guardrails.limits import Clock, Deadline, TokenBudget, call_with_retry, is_retryable

__all__ = [
    "Deadline",
    "FileRules",
    "Finding",
    "Guardrails",
    "GuardrailsConfig",
    "InjectionScanner",
    "PathGuard",
    "TokenBudget",
    "call_with_retry",
    "clean_env",
    "is_retryable",
    "load_config",
]


class Guardrails:
    """Фасад: собирает все проверки из одного `GuardrailsConfig`."""

    def __init__(self, config: GuardrailsConfig, clock: Clock = time.monotonic) -> None:
        self.config = config
        self._clock = clock
        self.scanner = InjectionScanner(config.injection)

    @classmethod
    def load(cls, path: Path) -> Guardrails:
        return cls(load_config(path))

    def path_guard(self, root: Path, extra: FileRules | Sequence[str] = ()) -> PathGuard:
        """Границы путей; `extra` — правила репозитория сверх общих (правила только добавляются).

        Последовательность строк понимается как дополнительные запреты записи.
        """
        rules = FileRules(write_deny=list(extra)) if not isinstance(extra, FileRules) else extra
        merged = self.config.paths.merged(rules)
        return PathGuard(
            root,
            self.config.paths.model_copy(
                update={
                    "write_deny": merged.write_deny,
                    "read_deny": merged.read_deny,
                    "hide": merged.hide,
                }
            ),
            extra_write_allow=[rules.write_allow],  # список репозитория сужает общий
        )

    def deadline(self, elapsed: float = 0.0) -> Deadline:
        return Deadline(self.config.limits.ticket_timeout_seconds, self._clock, elapsed)

    def token_budget(self) -> TokenBudget:
        return TokenBudget(self.config.limits.max_context_tokens)

    def max_steps(self, agent: str) -> int:
        return self.config.limits.max_steps[agent]
