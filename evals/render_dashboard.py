"""Renders evals/history/dashboard.html from the accumulated
evals/history/*.jsonl files (written by evals/report.py's record_run(),
called from run_live.py / run_adversarial.py / run_reasoning_quality.py) —
a static, self-contained "before/after numbers" view over this project's
eval history: a stat tile + score-trend line per eval type, plus a full
table (every run, every case) for the values the chart's hover tooltips
already surface. No build step, no external JS/CSS — same vanilla-HTML
convention as app/static/index.html.

Usage:
    python -m evals.render_dashboard

CI (.github/workflows/red-team.yml) runs this after each eval and uploads
evals/history/ (the *.jsonl files plus this rendered HTML) as a workflow
artifact, rather than committing it back to the repo — see that workflow's
comment for why: the tool-baseline regeneration workflow already hit real
branch-protection friction auto-committing a much higher-stakes file, and
an eval-history nice-to-have doesn't warrant repeating that fight. Local
runs of the eval scripts DO append to git-tracked evals/history/*.jsonl,
since committing those is a deliberate human action like any other change.
"""

import html
import json
import sys
from pathlib import Path
from typing import Any

HISTORY_DIR = Path(__file__).resolve().parent / "history"
OUTPUT_PATH = HISTORY_DIR / "dashboard.html"

# Fixed categorical order (dataviz skill: "assign categorical hues in fixed
# order, never cycled") — slots 1/2/3 from the validated default palette,
# confirmed via scripts/validate_palette.js for this exact 3-color subset
# in both light and dark mode before use.
_SERIES_STYLE = {
    "golden_live": {"label": "Golden tickets (live model)", "light": "#2a78d6", "dark": "#3987e5"},
    "adversarial": {"label": "Adversarial (prompt-injection)", "light": "#008300", "dark": "#008300"},
    "reasoning_quality": {"label": "Reasoning quality (DeepEval)", "light": "#e87ba4", "dark": "#d55181"},
}
_SERIES_ORDER = ["golden_live", "adversarial", "reasoning_quality"]


def _load_history() -> dict[str, list[dict[str, Any]]]:
    history: dict[str, list[dict[str, Any]]] = {}
    for name in _SERIES_ORDER:
        path = HISTORY_DIR / f"{name}.jsonl"
        records = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        records.sort(key=lambda r: r["timestamp"])
        history[name] = records
    return history


def _stat_tile(name: str, records: list[dict[str, Any]]) -> str:
    style = _SERIES_STYLE[name]
    label = html.escape(style["label"])
    if not records:
        return (
            f'<div class="tile"><div class="tile-dot" style="background:var(--{name})"></div>'
            f'<div class="tile-label">{label}</div><div class="tile-value muted">No runs yet</div></div>'
        )
    latest = records[-1]
    status = "pass" if latest["score"] >= latest["min_score"] else "fail"
    return f"""<div class="tile">
      <div class="tile-dot" style="background:var(--{name})"></div>
      <div class="tile-label">{label}</div>
      <div class="tile-value {status}">{latest["score"]:.2f}</div>
      <div class="tile-meta">{latest["passed"]}/{latest["total"]} passed · {html.escape(latest["model"])}</div>
      <div class="tile-meta muted">{html.escape(latest["timestamp"])}</div>
    </div>"""


def _legend(history: dict[str, list[dict[str, Any]]]) -> str:
    """A real legend row — swatch carries the series color, text stays in
    ink (dataviz skill: "text wears text tokens, never the series color").
    Always present for the >=2 series this chart can show; the mandatory
    accessibility layer for identity that isn't color-alone.
    """
    items = []
    for name in _SERIES_ORDER:
        if not history[name]:
            continue
        items.append(
            f'<span class="legend-item"><span class="legend-swatch" '
            f'style="background:var(--{name})"></span>{html.escape(_SERIES_STYLE[name]["label"])}</span>'
        )
    return f'<div class="legend">{"".join(items)}</div>' if items else ""


def _chart_svg(history: dict[str, list[dict[str, Any]]]) -> str:
    all_timestamps = sorted({r["timestamp"] for records in history.values() for r in records})
    if len(all_timestamps) < 1:
        return '<p class="muted">No eval runs recorded yet — run any of evals/run_live.py, ' \
               "evals/run_adversarial.py, or evals/run_reasoning_quality.py to start a history.</p>"

    t_index = {t: i for i, t in enumerate(all_timestamps)}
    x_max = max(len(all_timestamps) - 1, 1)
    # pad_r has no reserved label space (labels live in the legend/tiles/
    # table now, not inline in the SVG) — the plot can use the full width,
    # which also sidesteps the overflow risk inline end-labels had for a
    # series whose last point sits near the right edge.
    width, height, pad_l, pad_r, pad_t, pad_b = 900, 300, 50, 16, 20, 30
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    def x_of(ts: str) -> float:
        return pad_l + (t_index[ts] / x_max) * plot_w if x_max else pad_l + plot_w / 2

    def y_of(score: float) -> float:
        return pad_t + (1 - max(0.0, min(1.0, score))) * plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart-svg" role="img" '
        f'aria-label="Eval score trend over time">'
    ]
    # Gridlines + y-axis labels at 0.0/0.5/1.0
    for frac in (0.0, 0.5, 1.0):
        y = y_of(frac)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="gridline"/>')
        parts.append(f'<text x="{pad_l - 10}" y="{y + 4:.1f}" class="axis-label" text-anchor="end">{frac:.1f}</text>')
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" y2="{pad_t + plot_h}" class="baseline"/>'
    )

    for name in _SERIES_ORDER:
        records = history[name]
        if not records:
            continue
        points = [(x_of(r["timestamp"]), y_of(r["score"]), r) for r in records]
        if len(points) > 1:
            path_d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y, _r) in enumerate(points))
            parts.append(
                f'<path d="{path_d}" fill="none" stroke="var(--{name})" stroke-width="2" '
                'stroke-linecap="round" class="series-line"/>'
            )
        for x, y, r in points:
            status = "pass" if r["score"] >= r["min_score"] else "fail"
            tip = (
                f"{_SERIES_STYLE[name]['label']}\n"
                f"Score: {r['score']:.2f} (min {r['min_score']:.2f}) — {status.upper()}\n"
                f"{r['passed']}/{r['total']} passed — model: {r['model']}\n"
                f"{r['timestamp']}"
            )
            parts.append(
                f'<g class="point-hit" tabindex="0">'
                f'<title>{html.escape(tip)}</title>'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="12" fill="transparent"/>'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="var(--{name})" class="point-mark"/>'
                f"</g>"
            )

    parts.append("</svg>")
    return "\n".join(parts)


def _table(history: dict[str, list[dict[str, Any]]]) -> str:
    rows = []
    for name in _SERIES_ORDER:
        for r in history[name]:
            status = "PASS" if r["score"] >= r["min_score"] else "FAIL"
            rows.append((r["timestamp"], name, r["score"], r["min_score"], r["passed"], r["total"], r["model"], status))
    rows.sort(key=lambda row: row[0], reverse=True)

    if not rows:
        return ""

    body = "\n".join(
        f"<tr><td>{html.escape(ts)}</td><td>{html.escape(_SERIES_STYLE[name]['label'])}</td>"
        f'<td class="num">{score:.2f}</td><td class="num">{min_score:.2f}</td>'
        f'<td class="num">{passed}/{total}</td><td>{html.escape(model)}</td>'
        f'<td class="status-{status.lower()}">{status}</td></tr>'
        for ts, name, score, min_score, passed, total, model, status in rows
    )
    return f"""<table class="history-table">
      <thead><tr><th>Timestamp</th><th>Eval</th><th>Score</th><th>Min</th><th>Passed</th><th>Model</th><th>Result</th></tr></thead>
      <tbody>{body}</tbody>
    </table>"""


_CSS = """
:root { color-scheme: light; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark; } }
:root[data-theme="dark"] { color-scheme: dark; }

body {
  --surface: #fcfcfb; --page: #f9f9f7; --text-primary: #0b0b0b; --text-secondary: #52514e;
  --muted: #898781; --gridline: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
  --good: #0ca30c; --critical: #d03b3b;
  --golden_live: #2a78d6; --adversarial: #008300; --reasoning_quality: #e87ba4;
}
@media (prefers-color-scheme: dark) {
  body:not([data-theme="light"]) {
    --surface: #1a1a19; --page: #0d0d0d; --text-primary: #ffffff; --text-secondary: #c3c2b7;
    --muted: #898781; --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
    --good: #0ca30c; --critical: #e66767;
    --golden_live: #3987e5; --adversarial: #008300; --reasoning_quality: #d55181;
  }
}
body[data-theme="dark"] {
  --surface: #1a1a19; --page: #0d0d0d; --text-primary: #ffffff; --text-secondary: #c3c2b7;
  --muted: #898781; --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
  --good: #0ca30c; --critical: #e66767;
  --golden_live: #3987e5; --adversarial: #008300; --reasoning_quality: #d55181;
}

* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px 64px; background: var(--page); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
h1 { font-size: 20px; margin: 0 0 4px; }
.subtitle { color: var(--text-secondary); font-size: 13px; margin: 0 0 28px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 28px; }
.tile {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px;
  position: relative;
}
.tile-dot { width: 8px; height: 8px; border-radius: 50%; position: absolute; top: 16px; right: 16px; }
.tile-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; padding-right: 20px; }
.tile-value { font-size: 28px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile-value.pass { color: var(--good); }
.tile-value.fail { color: var(--critical); }
.tile-meta { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
.muted { color: var(--muted); }

.chart-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 20px; margin-bottom: 28px;
}
.legend { display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 16px; font-size: 12px; color: var(--text-secondary); }
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.legend-swatch { width: 14px; height: 2px; border-radius: 1px; display: inline-block; }
.chart-svg { width: 100%; height: auto; overflow: visible; }
.gridline { stroke: var(--gridline); stroke-width: 1; }
.baseline { stroke: var(--baseline); stroke-width: 1; }
.axis-label { fill: var(--muted); font-size: 11px; }
.series-end-label { font-size: 12px; font-weight: 500; }
.point-hit { cursor: pointer; }
.point-hit:hover .point-mark, .point-hit:focus .point-mark { r: 6.5; }
.point-mark { transition: r 0.1s ease; }
.point-hit:focus { outline: none; }

.history-table { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.history-table th, .history-table td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--gridline); }
.history-table th { color: var(--text-secondary); font-weight: 500; font-size: 12px; }
.history-table td.num { font-variant-numeric: tabular-nums; }
.history-table .status-pass { color: var(--good); font-weight: 600; }
.history-table .status-fail { color: var(--critical); font-weight: 600; }
.section-label { font-size: 13px; color: var(--text-secondary); margin: 0 0 10px; }
"""


def render() -> Path:
    history = _load_history()
    tiles = "\n".join(_stat_tile(name, history[name]) for name in _SERIES_ORDER)
    legend = _legend(history)
    chart = _chart_svg(history)
    table = _table(history)
    table_section = (
        f'<p class="section-label">Every run</p>{table}' if table else '<p class="muted">No runs recorded yet.</p>'
    )

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eval history — Enterprise IT Automator</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Eval history</h1>
<p class="subtitle">Score trend across evals/run_live.py, evals/run_adversarial.py, and
evals/run_reasoning_quality.py — generated by evals/render_dashboard.py from evals/history/*.jsonl.</p>

<div class="tiles">
{tiles}
</div>

<div class="chart-card">
{legend}
{chart}
</div>

{table_section}

</body>
</html>
"""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html_doc, encoding="utf-8")
    return OUTPUT_PATH


def main() -> int:
    path = render()
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
