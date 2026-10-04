"""Процесс-рабочий для теста возобновления после убитого процесса (запускается из pytest).

Три этапа пишут в `calls.txt`. Если рядом лежит файл `hang`, этап B после старта зависает:
тест убивает процесс именно в этот момент.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ario.config import Settings
from ario.guardrails import Guardrails
from ario.orchestrator import Orchestrator, Stage, StageContext


def main(data_dir: Path, guardrails_path: Path) -> int:
    calls = data_dir / "calls.txt"

    def log(text: str) -> None:
        with calls.open("a", encoding="utf-8") as f:
            f.write(text + "\n")

    def stage_a(ctx: StageContext) -> None:
        log("A")
        (ctx.run_dir / "a.txt").write_text("результат A", encoding="utf-8")

    def stage_b(ctx: StageContext) -> None:
        log("B-start")
        if (data_dir / "hang").exists():
            log("B-hanging")
            time.sleep(120)
        log("B-done")

    def stage_c(ctx: StageContext) -> None:
        log("C")

    settings = Settings(
        data_dir=data_dir,
        rx_path=None,
        llm_model="stub",
        openrouter_api_key=None,
        guardrails_path=guardrails_path,
    )
    stages = [Stage("a", stage_a), Stage("b", stage_b), Stage("c", stage_c)]
    guardrails = Guardrails.load(guardrails_path)
    outcome = Orchestrator(settings, stages, guardrails, preflight=lambda: None).run("43031")
    log(f"OUTCOME {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2])))
