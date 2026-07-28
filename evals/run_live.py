"""Live golden-ticket eval against the REAL configured LLM:

    python -m evals.run_live            # uses LLM_PROVIDER/.env as-is
    EVAL_MIN_SCORE=1.0 python -m evals.run_live

Exit code 0 when the pass-rate meets EVAL_MIN_SCORE (default 0.8 — live
models are allowed one flaky miss out of six; CI's recorded replay allows
zero), nonzero otherwise, so this slots into a release checklist or a
scheduled workflow as a gate.

Run this before: bumping GROQ_MODEL/ANTHROPIC_MODEL pins, switching
LLM_PROVIDER, or editing anything under app/agent/prompts/ — those are
exactly the changes CI's recorded replay cannot judge.

Uses an isolated throwaway SQLite DB (never your real data/ files): gated
tickets write real Approval rows through the graph's own code paths.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    # Isolate the DB BEFORE importing anything that touches settings.
    scratch = Path(tempfile.mkdtemp(prefix="eval-live-")) / "eval.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{scratch.as_posix()}"

    from app.config import get_settings

    get_settings.cache_clear()

    from app.agent.llm import get_llm
    from app.db.session import init_db
    from evals.report import CaseSummary, current_model_label, record_run
    from evals.runner import evaluate

    async def _run():
        await init_db()
        real_llm = get_llm()
        return await evaluate(lambda ticket: real_llm)

    report = asyncio.run(_run())
    for line in report.summary_lines():
        print(line)

    record_run(
        "golden_live",
        score=report.score,
        passed=report.passed,
        total=report.total,
        min_score=float(os.environ.get("EVAL_MIN_SCORE", "0.8")),
        model=current_model_label(),
        cases=[
            CaseSummary(
                name=r.name, passed=r.passed,
                detail="; ".join(r.failures) if r.failures else f"category={r.category}",
            )
            for r in report.results
        ],
    )

    min_score = float(os.environ.get("EVAL_MIN_SCORE", "0.8"))
    if report.score < min_score:
        print(f"FAIL: score {report.score:.2f} < required {min_score:.2f}")
        return 1
    print(f"OK: score {report.score:.2f} >= required {min_score:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
