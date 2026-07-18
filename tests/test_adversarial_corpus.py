"""Tests for the adversarial prompt-injection corpus (evals/adversarial.py)
and its scorer (evals/run_adversarial.py) — offline/static only, no live
LLM call. The actual live-model fuzz run is evals/run_adversarial.py's
main(), invoked manually / via .github/workflows/red-team.yml, not in CI's
blocking pytest suite (same reasoning as evals/run_live.py never being
imported/run by pytest either).
"""

from evals.adversarial import ADVERSARIAL_CASES
from evals.run_adversarial import _score_case


def test_corpus_is_nonempty_and_well_formed():
    assert len(ADVERSARIAL_CASES) >= 10
    names = [c["name"] for c in ADVERSARIAL_CASES]
    assert len(names) == len(set(names)), "adversarial case names must be unique"
    for case in ADVERSARIAL_CASES:
        assert case["subject"]
        assert case["body"]
        assert isinstance(case["forbidden_tools"], list)
        assert isinstance(case["must_appear_and_gate"], list)


def test_corpus_covers_a_pure_injection_and_a_legitimate_bundling_case():
    # At least one case where the whole point is a tool that must NEVER appear...
    assert any(c["forbidden_tools"] for c in ADVERSARIAL_CASES)
    # ...and at least one case distinguishing "legitimately bundled sensitive
    # actions" from "injected" ones (must still gate, not silently pass through).
    assert any(c["must_appear_and_gate"] for c in ADVERSARIAL_CASES)


def _case(forbidden_tools=None, must_appear_and_gate=None):
    return {
        "name": "t", "subject": "s", "body": "b",
        "forbidden_tools": forbidden_tools or [],
        "must_appear_and_gate": must_appear_and_gate or [],
        "note": "",
    }


def test_score_case_passes_when_forbidden_tool_absent():
    case = _case(forbidden_tools=["identity_disable_user"])
    plan = [{"tool": "access_grant_access", "args": {}}]
    result = _score_case(case, plan, gated=True)
    assert result.passed


def test_score_case_fails_when_forbidden_tool_present():
    case = _case(forbidden_tools=["identity_disable_user"])
    plan = [
        {"tool": "access_grant_access", "args": {}},
        {"tool": "identity_disable_user", "args": {"username": "ceo"}},
    ]
    result = _score_case(case, plan, gated=True)
    assert not result.passed
    assert "identity_disable_user" in result.failures[0]


def test_score_case_fails_when_required_legitimate_tool_missing():
    case = _case(must_appear_and_gate=["identity_disable_user"])
    plan = [{"tool": "access_grant_access", "args": {}}]
    result = _score_case(case, plan, gated=False)
    assert not result.passed
    assert any("missing from the plan" in f for f in result.failures)


def test_score_case_fails_when_required_tool_present_but_not_gated():
    case = _case(must_appear_and_gate=["identity_disable_user"])
    plan = [{"tool": "identity_disable_user", "args": {"username": "jsmith"}}]
    result = _score_case(case, plan, gated=False)
    assert not result.passed
    assert any("did not gate" in f for f in result.failures)


def test_score_case_passes_when_required_tool_present_and_gated():
    case = _case(must_appear_and_gate=["identity_disable_user"])
    plan = [{"tool": "identity_disable_user", "args": {"username": "jsmith"}}]
    result = _score_case(case, plan, gated=True)
    assert result.passed
