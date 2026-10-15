"""Этап developer: ветка тикета (BRANCH_TEMPLATE), правки по плану analyst, сборка. Без коммитов."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ario.agents.backend import AgentBackend, AgentRequest, AgentResult
from ario.agents.dev_tools import DeveloperToolset
from ario.agents.prompts import developer_task
from ario.agents.spec import SpecError, load_agent_spec
from ario.agents.verdicts import Analysis, VerdictError, load_analysis, render_analysis_md
from ario.errors import StageFailed
from ario.fsutil import atomic_write_text
from ario.guardrails import PathGuard
from ario.guardrails.config import EnvConfig
from ario.orchestrator import Stage, StageContext
from ario.repos import NO_REPO, RepoProfile
from ario.stage_names import development_stage
from ario.ticket import Ticket
from ario.tools.build import BuildTool as ProjectBuilder
from ario.tools.git import GitRepo, Runner
from ario.tools.proc import ToolError, run_process
from ario.trace import TraceWriter

ROLE = "developer"


@dataclass
class DevelopmentReport:
    branch: str
    attempt: int
    build_ok: bool
    build_runs: int
    files_edited: list[str]
    side_effect_files: list[
        str
    ]  # изменились, но агент их не правил (например, побочный эффект сборки)
    summary: str
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    problems: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        status = "успешна" if self.build_ok else "не подтверждена"
        lines = [
            f"# Разработка (попытка {self.attempt})",
            "",
            f"Ветка: `{self.branch}`. Сборка: {status} (запусков: {self.build_runs}).",
            "",
            "## Изменённые файлы",
            *[f"- `{f}`" for f in self.files_edited],
        ]
        if not self.files_edited:
            lines.append("- (агент ничего не менял)")
        if self.side_effect_files:
            lines += ["", "## Изменились без правок агента (побочные эффекты)"]
            lines += [f"- `{f}`" for f in self.side_effect_files]
        if self.problems:
            lines += ["", "## Проблемы", *[f"- {p}" for p in self.problems]]
        lines += ["", "## Резюме агента", self.summary.strip() or "(пусто)"]
        return "\n".join(lines) + "\n"


def _prepare_branch(
    git: GitRepo, ticket: Ticket, attempt: int, trace: TraceWriter, stage: str
) -> str:
    name = git.branch_for(ticket.id, ticket.type)
    try:
        current = git.current_branch()
        if current == name:  # возобновление после падения или доработка: продолжаем в своей ветке
            trace.event("branch_reused", stage=stage, agent=ROLE, branch=name)
            return name
        if attempt > 1:
            raise StageFailed(
                f"Для доработки ожидалась ветка {name}, сейчас {current or 'detached HEAD'}"
            )
        git.ensure_clean()  # между analyst и developer дерево могли испортить
        if git.reuse_empty_ticket_branch(ticket.id, ticket.type):  # осталась от прошлого запуска
            trace.event("branch_reused", stage=stage, agent=ROLE, branch=name, was_empty=True)
            return name
        created = git.create_ticket_branch(ticket.id, ticket.type)
    except ToolError as e:
        raise StageFailed(f"Git: {e}") from e
    trace.event("branch_created", stage=stage, agent=ROLE, branch=created)
    return created


def _load_analysis(ctx: StageContext) -> Analysis:
    path = ctx.run_dir / "analysis.json"
    if not path.is_file():
        raise StageFailed("Нет analysis.json: developer запускается только после analyst")
    try:
        return load_analysis(path)
    except VerdictError as e:
        raise StageFailed(str(e)) from e


def _make_builder(
    repo: RepoProfile, guard: PathGuard, env: EnvConfig, runner: Runner
) -> ProjectBuilder:
    command = repo.build_command
    cwd = Path(command[0]).parent if command and Path(command[0]).is_absolute() else None
    return ProjectBuilder(
        repo.path, guard, env, runner=runner, command=command or None, command_cwd=cwd
    )


def make_developer_stage(
    backend: AgentBackend, *, attempt: int = 1, runner: Runner = run_process
) -> Stage:
    """`attempt` > 1: доработка по замечаниям reviewer (`review-<attempt-1>.md`)."""
    name = development_stage(attempt)

    def run(ctx: StageContext) -> None:
        repo = ctx.repo
        if repo is None:
            raise StageFailed(f"{NO_REPO}: developer не знает, где править код")
        rx = repo.path
        try:
            spec = load_agent_spec(repo.knowledge_dir, ROLE)
        except SpecError as e:
            raise StageFailed(f"Ошибка конфигурации агента {ROLE}: {e}") from e
        analysis = _load_analysis(ctx)

        feedback: str | None = None
        if attempt > 1:
            review = ctx.run_dir / f"review-{attempt - 1}.md"
            if not review.is_file():
                raise StageFailed(f"Нет замечаний reviewer для доработки: {review.name}")
            feedback = review.read_text(encoding="utf-8")

        plan_md = render_analysis_md(analysis)
        scanner = ctx.guardrails.scanner
        scanner.check(plan_md, "плане analyst")  # вторичная инъекция через артефакты
        if feedback:
            scanner.check(feedback, "замечаниях reviewer")

        env = ctx.guardrails.config.env
        guard = ctx.guardrails.path_guard(rx, repo.write_deny_extra)
        git = GitRepo(rx, env, repo.base_branch, repo.branch_template, runner=runner)
        branch = _prepare_branch(git, ctx.ticket, attempt, ctx.trace, name)
        toolset = DeveloperToolset(
            rx, guard, git, _make_builder(repo, guard, env, runner), ctx.trace, name
        )
        request = AgentRequest(
            role=ROLE,
            root=rx,
            toolset=toolset,
            instructions=spec.instructions,
            task=developer_task(ctx.ticket, plan_md, feedback),
            tool_names=spec.tools,
            skill_paths=spec.skill_paths,
            max_steps=ctx.guardrails.max_steps(ROLE),
            token_budget=ctx.guardrails.token_budget(),
            deadline=ctx.deadline,
            scanner=ctx.guardrails.scanner,
            trace=ctx.trace,
            stage=name,
            model_retries=ctx.guardrails.config.limits.model_retries,
            call_timeout=ctx.guardrails.config.limits.model_call_timeout_seconds,
        )
        result = backend.run(request)
        report = _make_report(branch, attempt, toolset, git, result)
        atomic_write_text(
            ctx.run_dir / f"development-{attempt}.json",
            ctx.mask(json.dumps(report.__dict__, ensure_ascii=False, indent=2)),
        )
        atomic_write_text(ctx.run_dir / f"development-{attempt}.md", ctx.mask(report.to_markdown()))
        ctx.trace.event(
            "development_report",
            stage=name,
            agent=ROLE,
            build_ok=report.build_ok,
            files_edited=report.files_edited,
            problems=report.problems,
        )
        if report.problems:
            raise StageFailed(
                "Разработка не завершена: " + "; ".join(report.problems) + f"\n\n{report.summary}"
            )
        ctx.add_comment(ROLE, "done", report.to_markdown())

    return Stage(name, run)


def _make_report(
    branch: str, attempt: int, toolset: DeveloperToolset, git: GitRepo, result: AgentResult
) -> DevelopmentReport:
    edited = sorted(toolset.edited)
    try:
        changed = git.changed_files()
        prefix = git.toplevel_prefix()  # git даёт пути от корня репозитория, агент — от RX_PATH
    except ToolError as e:
        raise StageFailed(f"Git: {e}") from e
    side_effects = sorted(set(changed) - {prefix + path for path in edited})
    problems: list[str] = []
    if not edited:
        problems.append("агент не внёс изменений в файлы")
    if toolset.last_build_ok is None:
        problems.append("сборка ни разу не запускалась")
    elif toolset.last_build_ok is False:
        problems.append("последняя сборка не удалась")
    elif toolset.edits_since_build > 0:
        problems.append("после последней сборки были правки, а сборка не повторена")
    return DevelopmentReport(
        branch=branch,
        attempt=attempt,
        build_ok=toolset.build_is_green,
        build_runs=toolset.build_runs,
        files_edited=edited,
        side_effect_files=side_effects,
        summary=result.final_message,
        steps=result.steps,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        problems=problems,
    )
