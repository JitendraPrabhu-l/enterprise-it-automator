"""Tests for the supervisor/classifier step (Stage 2.5)."""

from app.agent.graph import CATEGORY_PROMPTS, classify_ticket_category


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    async def ainvoke(self, messages):
        return _FakeResponse(self.reply)


async def test_classify_recognizes_onboarding():
    assert await classify_ticket_category(_FakeLLM("ONBOARDING"), "onboard jsmith") == "ONBOARDING"


async def test_classify_recognizes_offboarding():
    assert await classify_ticket_category(_FakeLLM("OFFBOARDING"), "disable jsmith") == "OFFBOARDING"


async def test_classify_recognizes_access_change():
    assert await classify_ticket_category(_FakeLLM("ACCESS_CHANGE"), "grant vpn") == "ACCESS_CHANGE"


async def test_classify_strips_whitespace_and_quotes():
    assert await classify_ticket_category(_FakeLLM('  "ONBOARDING"  '), "x") == "ONBOARDING"


async def test_classify_case_insensitive():
    assert await classify_ticket_category(_FakeLLM("onboarding"), "x") == "ONBOARDING"


async def test_classify_defaults_to_access_change_on_garbage():
    """An unparseable/unexpected classifier response defaults to
    ACCESS_CHANGE — the least destructive category (no account
    creation/disabling) — rather than erroring out or guessing wrong."""
    assert await classify_ticket_category(_FakeLLM("I'm not sure, could be anything"), "x") == "ACCESS_CHANGE"


async def test_classify_defaults_to_access_change_on_empty_response():
    assert await classify_ticket_category(_FakeLLM(""), "x") == "ACCESS_CHANGE"


def test_all_three_categories_have_prompts():
    assert set(CATEGORY_PROMPTS.keys()) == {"ONBOARDING", "OFFBOARDING", "ACCESS_CHANGE"}


def test_onboarding_prompt_does_not_mention_disable():
    """Each category prompt should only expose the tools relevant to its
    category — the onboarding prompt shouldn't invite the planner to call
    identity_disable_user, which belongs to the offboarding prompt."""
    assert "identity_disable_user(username)" not in CATEGORY_PROMPTS["ONBOARDING"].split("Available tools:")[0]


def test_offboarding_prompt_forbids_create_and_focuses_on_disable():
    prompt = CATEGORY_PROMPTS["OFFBOARDING"]
    assert "identity_disable_user" in prompt
    assert "OFFBOARDING" in prompt.upper()


def test_access_change_prompt_forbids_create_and_disable():
    prompt = CATEGORY_PROMPTS["ACCESS_CHANGE"]
    assert "Never plan identity_create_user or identity_disable_user" in prompt
