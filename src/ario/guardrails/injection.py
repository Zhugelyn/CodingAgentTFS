"""Injection-сканер: проверка недоверенного текста по шаблонам из `guardrails.yaml`."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ario.errors import InjectionDetected
from ario.guardrails.config import InjectionConfig


@dataclass(frozen=True)
class Finding:
    rule_id: str
    source: str
    excerpt: str


class InjectionScanner:
    def __init__(self, config: InjectionConfig) -> None:
        self._rules = [(r.id, re.compile(r.regex)) for r in config.patterns]

    def scan(self, text: str, source: str) -> list[Finding]:
        findings: list[Finding] = []
        for rule_id, regex in self._rules:
            match = regex.search(text)
            if match is not None:
                findings.append(Finding(rule_id, source, match.group(0)[:120]))
        return findings

    def check(self, text: str, source: str) -> None:
        """Бросает `InjectionDetected` с понятной причиной, если сработало хоть одно правило."""
        findings = self.scan(text, source)
        if findings:
            details = "; ".join(f"{f.rule_id}: «{f.excerpt}»" for f in findings)
            raise InjectionDetected(
                f"Возможная prompt injection в {source}. Сработали правила: {details}"
            )
