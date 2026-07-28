"""Shared eval-result persistence — the "before/after numbers" half of this
project's eval suite. evals/run_live.py, run_adversarial.py, and
run_reasoning_quality.py each already compute a real score against the
real configured model; until now nothing kept a record of it anywhere a
human could see a trend — only stdout/CI logs for that one run, gone the
moment the terminal/log scrolls away.

record_run() appends one JSON line per eval run to evals/history/<name>.jsonl
(git-tracked — this accumulates real history as the project's own evals
actually run over time, not synthetic demo data) and, when running inside
GitHub Actions (GITHUB_STEP_SUMMARY set), also writes a markdown table
straight into that run's own step summary, so the result is visible in the
Actions UI immediately, no extra infrastructure required.

evals/render_dashboard.py reads the accumulated *.jsonl history and renders
a small static HTML trend view — see that module for why CI uploads it as
a workflow artifact rather than committing it back to the repo.
"""

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

HISTORY_DIR = Path(__file__).resolve().parent / "history"


@dataclass
class CaseSummary:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class RunRecord:
    timestamp: str
    score: float
    passed: int
    total: int
    min_score: float
    model: str
    cases: list[CaseSummary] = field(default_factory=list)


def current_model_label() -> str:
    """Best-effort model identifier for attribution — reads the configured
    provider/model pair directly from Settings rather than introspecting a
    LangChain chat-model object, since every eval script here already
    calls get_llm() with no FallbackLLM wrapper (see app/agent/llm.py) and
    Settings is the actual source of truth for "which model is pinned."
    """
    from app.config import get_settings

    settings = get_settings()
    provider = settings.llm_provider
    model = {
        "groq": settings.groq_model,
        "anthropic": settings.anthropic_model,
        "watsonx": settings.watsonx_model,
        "openrouter": settings.openrouter_model,
    }.get(provider, "?")
    return f"{provider}:{model}"


def record_run(
    eval_name: str,
    *,
    score: float,
    passed: int,
    total: int,
    min_score: float,
    cases: list[CaseSummary],
    model: str | None = None,
) -> Path:
    """Appends one run's result to evals/history/<eval_name>.jsonl and, in
    CI, also writes it to $GITHUB_STEP_SUMMARY. Never raises on the
    step-summary half — a missing/unwritable summary file must not fail
    the eval run itself, only the persisted history matters for the
    caller's own exit-code decision.
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{eval_name}.jsonl"
    record = RunRecord(
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        score=round(score, 4),
        passed=passed,
        total=total,
        min_score=min_score,
        model=model or current_model_label(),
        cases=cases,
    )
    line = json.dumps(
        {
            "timestamp": record.timestamp,
            "score": record.score,
            "passed": record.passed,
            "total": record.total,
            "min_score": record.min_score,
            "model": record.model,
            "cases": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in record.cases],
        }
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            _write_step_summary(Path(summary_path), eval_name, record)
        except OSError:
            pass
    return path


def _write_step_summary(summary_path: Path, eval_name: str, record: RunRecord) -> None:
    lines = [
        f"## {eval_name} eval result",
        f"**Score:** {record.score:.2f} (required: {record.min_score:.2f}) — "
        f"{record.passed}/{record.total} passed — model: `{record.model}`",
        "",
        "| Case | Result | Detail |",
        "|---|---|---|",
    ]
    for case in record.cases:
        mark = "PASS" if case.passed else "FAIL"
        detail = (case.detail or "—").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {case.name} | {mark} | {detail} |")
    with summary_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")
