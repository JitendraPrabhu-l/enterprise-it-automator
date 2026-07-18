"""Golden-ticket evaluation suite for the planner/classifier pipeline.

Two consumers, one scorer:

- tests/test_golden_tickets.py (CI, deterministic): replays each golden
  ticket's RECORDED model outputs through the real graph — classifier
  parsing, username extraction, JSON guardrails, sensitivity gating,
  routing — and requires a 100% pass. This pins the pipeline's contract:
  a prompt/parser/policy change that alters what happens to these tickets
  fails CI immediately.

- evals/run_live.py (manual / scheduled, uses the real configured LLM):
  same tickets, same scoring, but the model answers for itself — this is
  what catches MODEL drift (provider swaps, model version bumps, prompt
  regressions that only show up in real generations). Run it before
  changing LLM_PROVIDER/model pins, and periodically against production
  config.

A third, separate corpus — evals/adversarial.py + evals/run_adversarial.py
(manual / weekly-scheduled, real configured LLM only, never recorded
replay) — fuzzes prompt-injection resistance specifically: ~10 disguised
injection attempts, scored on whether a forbidden tool ever appears in the
plan, not on exact category/tool-sequence match (a different, looser
contract than the golden tickets' — see evals/run_adversarial.py's module
docstring for why). Complements the golden set's single pinned
`prompt-injection-resisted` case, which only proves resistance to one
already-known phrasing replayed against a canned response.
"""
