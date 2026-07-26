"""Tests for evals/run_reasoning_quality.py's scoring logic and the
DeepEval judge wrapper — offline/static only, no live LLM call (same
"offline/static vs. live runner script" split test_adversarial_corpus.py's
own module docstring describes: the actual live-judge run is
evals.run_reasoning_quality's main(), invoked manually / via
.github/workflows/red-team.yml, not in CI's blocking pytest suite).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from evals.deepeval_judge import ProjectLLMJudge
from evals.run_reasoning_quality import _score_ticket


class _FakeLLMResponse:
    def __init__(self, content: str) -> None:
        self.content = content


async def test_project_llm_judge_a_generate_returns_response_content() -> None:
    fake_llm = AsyncMock()
    fake_llm.ainvoke.return_value = _FakeLLMResponse("judge reply text")
    judge = ProjectLLMJudge(fake_llm, model_name="test-model")

    result = await judge.a_generate("some prompt")

    assert result == "judge reply text"
    fake_llm.ainvoke.assert_called_once()


def test_project_llm_judge_get_model_name_returns_configured_name() -> None:
    judge = ProjectLLMJudge(MagicMock(), model_name="my-configured-model")
    assert judge.get_model_name() == "my-configured-model"


def test_project_llm_judge_load_model_returns_self() -> None:
    judge = ProjectLLMJudge(MagicMock(), model_name="x")
    assert judge.load_model() is judge


async def test_score_ticket_trivially_passes_an_empty_plan() -> None:
    ticket = {"name": "status-inquiry-no-action", "subject": "s", "body": "b"}
    metric = MagicMock()

    result = await _score_ticket(ticket, plan=[], metric=metric)

    assert result.passed is True
    assert result.score == 1.0
    metric.a_measure.assert_not_called()


async def test_score_ticket_scores_a_nonempty_plan_via_the_metric() -> None:
    ticket = {"name": "onboarding-new-hire-engineering", "subject": "s", "body": "b"}
    plan = [{"tool": "identity_create_user", "args": {}, "reasoning": "x"}]
    metric = MagicMock()
    metric.threshold = 0.7
    metric.reason = "looks fine"
    metric.a_measure = AsyncMock(return_value=0.9)

    result = await _score_ticket(ticket, plan, metric)

    assert result.score == 0.9
    assert result.passed is True
    assert result.reason == "looks fine"
    metric.a_measure.assert_called_once()


async def test_score_ticket_fails_below_threshold_with_a_failure_message() -> None:
    ticket = {"name": "access-change-grant-vpn", "subject": "s", "body": "b"}
    plan = [{"tool": "access_grant_access", "args": {}, "reasoning": "hallucinated justification"}]
    metric = MagicMock()
    metric.threshold = 0.7
    metric.reason = "reasoning invents a detail not in the ticket"
    metric.a_measure = AsyncMock(return_value=0.0)

    result = await _score_ticket(ticket, plan, metric)

    assert result.passed is False
    assert result.score == 0.0
    assert result.failures
    assert "below threshold" in result.failures[0]


@pytest.mark.parametrize("threshold_env", ["0.5", "0.9"])
def test_build_reasoning_metric_reads_per_case_threshold_from_env(monkeypatch, threshold_env) -> None:
    from evals.run_reasoning_quality import _build_reasoning_metric

    monkeypatch.setenv("REASONING_MIN_SCORE_PER_CASE", threshold_env)
    # GEval's model= validates its type against DeepEvalBaseLLM (and a few
    # concrete provider classes) — a bare MagicMock() fails that check, so
    # this needs a real ProjectLLMJudge instance, not a generic mock.
    judge = ProjectLLMJudge(MagicMock(), model_name="test-model")
    metric = _build_reasoning_metric(judge=judge)

    assert metric.threshold == float(threshold_env)
