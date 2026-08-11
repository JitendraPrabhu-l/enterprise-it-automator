"""Tests for evals/report.py (result persistence) and
evals/render_dashboard.py (the static HTML trend view built from it) — the
"before/after numbers" half of the eval suite (see build_in_public's
2026-07-18 devlog and docs/RUNBOOKS.md for why this exists: every eval
script already computed a real score against the real model, but nothing
kept a record anywhere a human could see a trend).
"""

import json
import re

import pytest

from evals import render_dashboard, report
from evals.report import CaseSummary


@pytest.fixture
def isolated_history(tmp_path, monkeypatch):
    history_dir = tmp_path / "history"
    monkeypatch.setattr(report, "HISTORY_DIR", history_dir)
    monkeypatch.setattr(render_dashboard, "HISTORY_DIR", history_dir)
    monkeypatch.setattr(render_dashboard, "OUTPUT_PATH", history_dir / "dashboard.html")
    return history_dir


def test_record_run_appends_a_jsonl_line(isolated_history):
    path = report.record_run(
        "golden_live", score=0.83, passed=5, total=6, min_score=0.8, model="groq:test-model",
        cases=[CaseSummary(name="c1", passed=True), CaseSummary(name="c2", passed=False, detail="oops")],
    )
    assert path == isolated_history / "golden_live.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["score"] == 0.83
    assert record["passed"] == 5
    assert record["total"] == 6
    assert record["model"] == "groq:test-model"
    assert record["cases"] == [
        {"name": "c1", "passed": True, "detail": ""},
        {"name": "c2", "passed": False, "detail": "oops"},
    ]


def test_record_run_appends_not_overwrites(isolated_history):
    for score in (0.5, 0.9):
        report.record_run(
            "adversarial", score=score, passed=1, total=1, min_score=1.0, model="m", cases=[],
        )
    lines = (isolated_history / "adversarial.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["score"] == 0.5
    assert json.loads(lines[1])["score"] == 0.9


def test_record_run_writes_github_step_summary(isolated_history, monkeypatch, tmp_path):
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    report.record_run(
        "adversarial", score=1.0, passed=2, total=2, min_score=1.0, model="groq:x",
        cases=[CaseSummary(name="case-a", passed=True)],
    )
    content = summary_path.read_text(encoding="utf-8")
    assert "adversarial eval result" in content
    assert "case-a" in content
    assert "PASS" in content


def test_record_run_tolerates_unwritable_step_summary(isolated_history, monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "no" / "such" / "dir" / "summary.md"))
    # Must not raise even though the summary path's parent doesn't exist.
    report.record_run("adversarial", score=1.0, passed=1, total=1, min_score=1.0, model="m", cases=[])


def test_current_model_label(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    get_settings.cache_clear()
    try:
        assert report.current_model_label() == "groq:llama-3.3-70b-versatile"
    finally:
        get_settings.cache_clear()


# --- render_dashboard --------------------------------------------------


def test_render_with_no_history_shows_placeholder(isolated_history):
    path = render_dashboard.render()
    content = path.read_text(encoding="utf-8")
    assert "No eval runs recorded yet" in content
    assert "No runs yet" in content  # stat tiles


def test_render_places_all_points_within_the_viewbox(isolated_history):
    report.record_run(
        "golden_live", score=0.5, passed=3, total=6, min_score=0.8, model="m",
        cases=[CaseSummary(name="c", passed=False)],
    )
    report.record_run(
        "golden_live", score=1.0, passed=6, total=6, min_score=0.8, model="m",
        cases=[CaseSummary(name="c", passed=True)],
    )
    report.record_run(
        "adversarial", score=1.0, passed=10, total=10, min_score=1.0, model="m", cases=[],
    )
    path = render_dashboard.render()
    content = path.read_text(encoding="utf-8")

    cx_vals = [float(x) for x in re.findall(r'cx="([\-\d.]+)"', content)]
    cy_vals = [float(y) for y in re.findall(r'cy="([\-\d.]+)"', content)]
    assert cx_vals and cy_vals
    assert all(0 <= x <= 900 for x in cx_vals)
    assert all(0 <= y <= 300 for y in cy_vals)


def test_render_handles_a_single_point_series_without_crashing(isolated_history):
    report.record_run(
        "reasoning_quality", score=0.9, passed=1, total=1, min_score=0.7, model="m",
        cases=[CaseSummary(name="only-one", passed=True)],
    )
    path = render_dashboard.render()
    content = path.read_text(encoding="utf-8")
    assert "only-one" not in content  # case names live in tooltips, not raw text
    assert "reasoning_quality" in content or "Reasoning quality" in content


def test_render_legend_text_is_not_colored_with_series_hue(isolated_history):
    """Dataviz rule: text wears text tokens, never the series color — only
    a swatch/mark may carry series identity. Regression guard for the bug
    caught while building this (an earlier version put the series color
    directly on inline SVG label text)."""
    report.record_run("golden_live", score=1.0, passed=1, total=1, min_score=0.8, model="m", cases=[])
    report.record_run("adversarial", score=1.0, passed=1, total=1, min_score=1.0, model="m", cases=[])
    content = render_dashboard.render().read_text(encoding="utf-8")
    assert 'fill="var(--golden_live)"' not in re.sub(r"<circle[^>]*>", "", content)
    assert "legend-swatch" in content


def test_render_produces_a_table_view(isolated_history):
    report.record_run(
        "adversarial", score=0.9, passed=9, total=10, min_score=1.0, model="groq:x",
        cases=[CaseSummary(name="c1", passed=False, detail="forbidden tool appeared")],
    )
    content = render_dashboard.render().read_text(encoding="utf-8")
    assert "<table" in content
    assert "groq:x" in content
    assert "FAIL" in content
