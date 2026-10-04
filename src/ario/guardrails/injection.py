"""Injection-сканер: проверка недоверенного текста по шаблонам из `guardrails.yaml`."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ario.errors import InjectionDetected
from ario.guardrails.config import InjectionConfig


@dataclass(frozen=True)
class Finding:
    rule_id: str
    source: str
    excerpt: str


_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]")


def normalize(text: str) -> str:
    """Приводит текст к виду, в котором шаблоны работают: NFKC (полноширинные и похожие
    символы), без невидимых и управляющих символов направления текста."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))


class InjectionScanner:
    def __init__(self, config: InjectionConfig) -> None:
        # DOTALL: `.{0,30}` пересекает перевод строки, иначе «ignore\nall previous…» проходит
        self._rules = [(r.id, re.compile(r.regex, re.DOTALL)) for r in config.patterns]

    def scan(self, text: str, source: str) -> list[Finding]:
        findings: list[Finding] = []
        text = normalize(text)
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
