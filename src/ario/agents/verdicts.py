"""Вердикты агентов (схемы из решения D01), разбор JSON из ответа модели и Markdown-рендер."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class VerdictError(ValueError):
    """Ответ агента не удалось разобрать или он не соответствует схеме."""


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    step: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list)


class Analysis(BaseModel):
    """Вердикт analyst (`analysis.json`)."""

    model_config = ConfigDict(extra="ignore")

    decision: Literal["suitable", "unsuitable", "needs_info"]
    reason: str = ""
    missing_info: list[str] = Field(default_factory=list)
    plan: list[PlanStep] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "low"
    risk_reason: str = ""

    @model_validator(mode="after")
    def _check_consistency(self) -> Analysis:
        if self.decision == "suitable" and not self.plan:
            raise ValueError("при decision=suitable нужен непустой plan")
        if self.decision != "suitable" and not self.reason.strip():
            raise ValueError(f"при decision={self.decision} нужна непустая reason")
        if self.decision == "needs_info" and not self.missing_info:
            raise ValueError("при decision=needs_info нужен непустой missing_info")
        return self


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> object:
    """Достаёт JSON-объект из ответа модели: из блока ```json или первый `{...}` в тексте."""
    fenced = _FENCE.search(text)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        if start < 0:
            raise VerdictError("В ответе нет JSON-объекта")
        candidate = text[start:]
    try:
        value, _ = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError as e:
        raise VerdictError(f"Некорректный JSON: {e}") from e
    return value


def load_analysis(path: Path) -> Analysis:
    """Читает `analysis.json`; повреждённый файл (обрыв записи, ручная правка) — VerdictError."""
    try:
        return Analysis.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # ValidationError — потомок ValueError
        raise VerdictError(f"{path.name} повреждён: {e}") from e


def _parse_model[T: BaseModel](text: str, model: type[T]) -> T:
    data = extract_json(text)
    if not isinstance(data, dict):
        raise VerdictError("JSON должен быть объектом")
    try:
        return model.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(map(str, err['loc'])) or 'вердикт'}: {err['msg']}" for err in e.errors()
        )
        raise VerdictError(f"Вердикт не соответствует схеме: {problems}") from e


def parse_analysis(text: str) -> Analysis:
    return _parse_model(text, Analysis)


_DECISION_TITLES = {
    "suitable": "Подходит для автоматической разработки",
    "unsuitable": "Не подходит для автоматической разработки",
    "needs_info": "Нужны дополнительные данные",
}
_RISK_TITLES = {"low": "низкий", "medium": "средний", "high": "высокий"}


def render_analysis_md(a: Analysis) -> str:
    """Читаемый рендер вердикта; производный файл, источник правды — JSON."""
    lines = [f"# Анализ: {_DECISION_TITLES[a.decision]}", ""]
    if a.reason:
        lines += ["## Причина", a.reason, ""]
    if a.missing_info:
        lines += ["## Что нужно дополнить", *[f"- {m}" for m in a.missing_info], ""]
    if a.plan:
        lines += ["## План"]
        for i, step in enumerate(a.plan, 1):
            lines.append(f"{i}. {step.step}")
            lines += [f"   - `{f}`" for f in step.files]
        lines.append("")
    lines += [f"## Риск: {_RISK_TITLES[a.risk]}"]
    if a.risk_reason:
        lines.append(a.risk_reason)
    return "\n".join(lines).rstrip() + "\n"


def failure_text(a: Analysis) -> str:
    """Текст для комментария тикета, когда analyst отказал."""
    parts = [a.reason.strip()]
    if a.missing_info:
        parts.append("Что нужно дополнить:\n" + "\n".join(f"- {m}" for m in a.missing_info))
    return "\n\n".join(parts)


# --- вердикт reviewer ---------------------------------------------------------------------------
class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    severity: Literal["blocker", "major", "minor"]
    description: str = Field(min_length=1)
    file: str = ""
    line: int | None = None


class Review(BaseModel):
    """Вердикт reviewer (`review-<n>.json`)."""

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["approve", "reject"]
    summary: str = Field(min_length=1)
    issues: list[ReviewIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_consistency(self) -> Review:
        if self.verdict == "reject" and not any(
            i.severity in ("blocker", "major") for i in self.issues
        ):
            raise ValueError(
                "при verdict=reject нужно хотя бы одно замечание уровня blocker или major"
            )
        if self.verdict == "approve" and any(i.severity == "blocker" for i in self.issues):
            raise ValueError("при verdict=approve не может быть замечаний уровня blocker")
        return self


def parse_review(text: str) -> Review:
    return _parse_model(text, Review)


_SEVERITY_TITLES = {"blocker": "блокирующее", "major": "существенное", "minor": "мелкое"}


def render_review_md(review: Review) -> str:
    title = "одобрено" if review.verdict == "approve" else "отклонено"
    lines = [f"# Ревью: {title}", "", review.summary.strip(), ""]
    if review.issues:
        lines.append("## Замечания")
        for issue in review.issues:
            where = ""
            if issue.file:
                where = f" `{issue.file}" + (f":{issue.line}" if issue.line else "") + "`"
            lines.append(
                f"- **{_SEVERITY_TITLES[issue.severity]}**{where}: {issue.description.strip()}"
            )
    return "\n".join(lines).rstrip() + "\n"
