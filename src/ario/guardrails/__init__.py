"""Guardrails: лимиты, injection-сканер, границы инструментов."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

from ario.guardrails.boundaries import PathGuard, clean_env
from ario.guardrails.config import GuardrailsConfig, load_config
from ario.guardrails.injection import Finding, InjectionScanner
from ario.guardrails.limits import Clock, Deadline, TokenBudget, call_with_retry, is_retryable

__all__ = [
    "Deadline",
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

    def path_guard(self, root: Path, extra_write_deny: Sequence[str] = ()) -> PathGuard:
        """Границы путей; `extra_write_deny` — правила профиля репозитория сверх общих."""
        paths = self.config.paths
        if extra_write_deny:
            paths = paths.model_copy(update={"write_deny": [*paths.write_deny, *extra_write_deny]})
        return PathGuard(root, paths)

    def deadline(self, elapsed: float = 0.0) -> Deadline:
        return Deadline(self.config.limits.ticket_timeout_seconds, self._clock, elapsed)

    def token_budget(self) -> TokenBudget:
        return TokenBudget(self.config.limits.max_context_tokens)

    def max_steps(self, agent: str) -> int:
        return self.config.limits.max_steps[agent]
