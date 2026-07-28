#!/usr/bin/env python
"""DeepEval-based graded scorer for the planner's `reasoning` field — a
SECOND, independent quality signal alongside evals/golden.py's exact-match
contract, following this project's own precedent for "add a second,
methodologically-different measurement rather than trust one" (this
codebase's evals/run_adversarial.py already does the analogous thing for
security: golden.py pins ONE known injection phrasing exactly, adversarial.py
fuzzes many DIFFERENT phrasings against a live model).

The gap this closes: evals/golden.py's _score (evals/runner.py) checks
category, exact tool sequence, arg subsets, forbidden tools, and the HITL
gate — all mechanically checkable. Nothing anywhere in this project's eval
suite checks whether the planner's `reasoning` field is a genuine,
sound justification for the plan it produced, versus a hallucinated-
sounding non-sequitur that happens to accompany a mechanically-correct
plan. That's a graded/judgment question a GEval-based LLM judge is built
for and exact-match scoring structurally cannot answer.

    python -m evals.run_reasoning_quality
    REASONING_MIN_SCORE=0.8 python -m evals.run_reasoning_quality

Judge model: this project's OWN configured LLM (see evals/deepeval_judge.py)
— zero additional cost or credential surface, reuses the free-tier
Groq/Anthropic/watsonx/OpenRouter key already configured via LLM_PROVIDER.

NOT wired into ci.yml's blocking suite, for the exact same reason
evals/run_live.py and evals/run_adversarial.py aren't (see red-team.yml's
own comment): a GEval judge call is a real live LLM call — costs API
spend (free-tier, but real), is non-deterministic, and needs a live
provider key, none of which belong in a PR-blocking recorded-replay gate.
Wired into red-team.yml's existing workflow_dispatch + weekly-schedule
job instead, alongside run_adversarial.py.

Uses an isolated throwaway SQLite DB, same as run_live.py/run_adversarial.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ReasoningResult:
    name: str
    score: float
    passed: bool
    reason: str
    failures: list[str] = field(default_factory=list)


def _build_reasoning_metric(judge):
    from deepeval.metrics import GEval
    from deepeval.test_case import SingleTurnParams

    return GEval(
        name="PlanReasoningQuality",
        model=judge,
        threshold=float(os.environ.get("REASONING_MIN_SCORE_PER_CASE", "0.5")),
        evaluation_steps=[
            "'input' is an IT ticket (subject + body); 'actual_output' is the "
            "JSON plan the agent produced, including each step's 'reasoning' field.",
            "For each step, check whether its 'reasoning' field is a genuine, "
            "specific justification connecting the ticket's actual content to "
            "the chosen tool and arguments — not a generic or templated-sounding "
            "restatement of the tool name.",
            "Penalize a reasoning field that asserts something the ticket text "
            "does not actually say (e.g. inventing a detail, a department, or "
            "an urgency level not present in the ticket).",
            "Do NOT penalize the plan's tool choice or arguments themselves — "
            "those are already checked separately by this project's exact-match "
            "golden-ticket suite (evals/golden.py); score ONLY whether the "
            "stated reasoning is sound and specific to the actual ticket.",
        ],
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
    )


async def _score_ticket(ticket, plan: list[dict], metric) -> ReasoningResult:
    import json

    from deepeval.test_case import LLMTestCase

    ticket_text = f"Subject: {ticket['subject']}\n\n{ticket['body']}"
    if not plan:
        # A correctly-empty plan (e.g. the status-inquiry-no-action golden
        # ticket) has no reasoning fields to grade — trivially passes rather
        # than penalizing a plan that was right to do nothing.
        return ReasoningResult(
            name=ticket["name"], score=1.0, passed=True,
            reason="Plan is empty; nothing to grade.",
        )

    test_case = LLMTestCase(input=ticket_text, actual_output=json.dumps(plan))
    score = await metric.a_measure(test_case)
    return ReasoningResult(
        name=ticket["name"], score=score, passed=score >= metric.threshold,
        reason=metric.reason or "",
        failures=[] if score >= metric.threshold else [f"score {score:.2f} below threshold"],
    )


async def run() -> list[ReasoningResult]:
    from app.agent.llm import get_llm
    from evals.deepeval_judge import ProjectLLMJudge
    from evals.golden import GOLDEN_TICKETS
    from evals.runner import run_ticket_through_graph

    real_llm = get_llm()
    judge = ProjectLLMJudge(real_llm, model_name="project-configured-llm")
    metric = _build_reasoning_metric(judge)

    results: list[ReasoningResult] = []
    for index, ticket in enumerate(GOLDEN_TICKETS):
        # Drives the REAL graph with the REAL configured LLM (same pattern
        # as run_live.py) rather than replaying recorded fixture text —
        # grading a canned string's reasoning quality would only prove the
        # canned string looks fine, not that the LIVE model's reasoning does.
        _category, plan, _gated = await run_ticket_through_graph(
            ticket["subject"], ticket["body"], real_llm,
            thread_id=f"reasoning-eval-{index}-{ticket['name']}", ticket_id=900_000 + index,
        )
        results.append(await _score_ticket(ticket, plan, metric))
    return results


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="eval-reasoning-")) / "eval.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{scratch.as_posix()}"

    from app.config import get_settings

    get_settings.cache_clear()

    from app.db.session import init_db

    async def _run():
        await init_db()
        return await run()

    results = asyncio.run(_run())

    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"[{mark}] {r.name} (score={r.score:.2f}): {r.reason}")

    mean_score = sum(r.score for r in results) / len(results) if results else 1.0
    min_score = float(os.environ.get("REASONING_MIN_SCORE", "0.7"))
    print(f"\nmean score: {mean_score:.2f} (required: {min_score:.2f})")

    from evals.report import CaseSummary, current_model_label, record_run

    record_run(
        "reasoning_quality",
        score=mean_score,
        passed=sum(1 for r in results if r.passed),
        total=len(results),
        min_score=min_score,
        model=f"{current_model_label()} (judge: project-configured-llm)",
        cases=[
            CaseSummary(name=r.name, passed=r.passed, detail=f"score={r.score:.2f}: {r.reason[:120]}")
            for r in results
        ],
    )

    if mean_score < min_score:
        print(f"FAIL: mean reasoning-quality score {mean_score:.2f} < required {min_score:.2f}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
