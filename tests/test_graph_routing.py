import json

import pytest

from app.agent.graph import (
    MAX_PLAN_LENGTH,
    _describe_result,
    _extract_json_array,
    _extract_username,
    _mask_pii_for_prompt,
    route_after_plan,
    route_after_step_check,
)


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    async def ainvoke(self, messages):
        return _FakeResponse(self.reply)


async def test_extract_username_plain():
    assert await _extract_username(_FakeLLM("jsmith"), "ticket text") == "jsmith"


async def test_extract_username_strips_quotes_and_whitespace():
    assert await _extract_username(_FakeLLM('  "jsmith"  '), "ticket text") == "jsmith"


async def test_extract_username_takes_first_line_only():
    assert await _extract_username(_FakeLLM("jsmith\nextra prose"), "ticket text") == "jsmith"


async def test_extract_username_none_returns_none():
    assert await _extract_username(_FakeLLM("NONE"), "ticket text") is None


async def test_extract_username_empty_returns_none():
    assert await _extract_username(_FakeLLM("   "), "ticket text") is None


async def test_extract_username_rejects_malformed_looking_username():
    """A response that doesn't look like a plausible username (e.g. an
    injection attempt or garbage the LLM echoed back) must be treated as
    unidentified, not passed through to _observe_user/the planner."""
    assert await _extract_username(_FakeLLM("'; DROP TABLE users; --"), "ticket text") is None


async def test_extract_username_accepts_dotted_and_hyphenated_usernames():
    assert await _extract_username(_FakeLLM("j.smith-2"), "ticket text") == "j.smith-2"


def test_extract_json_array_plain():
    assert _extract_json_array('[{"tool": "get_user", "args": {}}]') == [
        {"tool": "get_user", "args": {}}
    ]


def test_extract_json_array_with_markdown_fence():
    raw = '```json\n[{"tool": "get_user", "args": {}}]\n```'
    assert _extract_json_array(raw) == [{"tool": "get_user", "args": {}}]


def test_extract_json_array_with_surrounding_prose():
    raw = 'Here is my plan:\n[{"tool": "get_user", "args": {}}]\nLet me know if this works.'
    assert _extract_json_array(raw) == [{"tool": "get_user", "args": {}}]


def test_extract_json_array_empty_plan():
    assert _extract_json_array("[]") == []


def test_extract_json_array_allows_plan_at_max_length():
    steps = [{"tool": "get_user", "args": {"n": i}} for i in range(MAX_PLAN_LENGTH)]
    assert _extract_json_array(json.dumps(steps)) == steps


def test_extract_json_array_rejects_plan_over_max_length():
    steps = [{"tool": "get_user", "args": {"n": i}} for i in range(MAX_PLAN_LENGTH + 1)]
    with pytest.raises(ValueError, match="exceeding the"):
        _extract_json_array(json.dumps(steps))


def test_extract_json_array_rejects_step_with_malformed_username():
    raw = json.dumps([{"tool": "identity_disable_user", "args": {"username": "'; DROP TABLE users; --"}}])
    with pytest.raises(ValueError, match="doesn't look like a"):
        _extract_json_array(raw)


def test_extract_json_array_accepts_step_with_valid_username():
    steps = [{"tool": "identity_disable_user", "args": {"username": "j.smith-2"}}]
    assert _extract_json_array(json.dumps(steps)) == steps


def test_extract_json_array_allows_step_with_no_username_arg():
    steps = [{"tool": "ticketing_get_ticket_status", "args": {"ticket_id": 1}}]
    assert _extract_json_array(json.dumps(steps)) == steps


def test_extract_json_array_raises_on_garbage():
    import pytest

    with pytest.raises(ValueError):
        _extract_json_array("I refuse to produce JSON today.")


def test_route_after_plan_goes_to_finalize_on_error():
    state = {"error": "boom", "plan": []}
    assert route_after_plan(state) == "finalize"


def test_route_after_plan_goes_to_finalize_on_empty_plan():
    state = {"error": None, "plan": []}
    assert route_after_plan(state) == "finalize"


def test_route_after_plan_goes_to_route_step_when_plan_exists():
    state = {"error": None, "plan": [{"tool": "get_user", "args": {}, "reasoning": ""}]}
    assert route_after_plan(state) == "route_step"


def test_route_after_step_check_finalizes_when_plan_exhausted():
    state = {"plan": [{"tool": "get_user", "args": {}}], "plan_index": 1}
    assert route_after_step_check(state) == "finalize"


def test_route_after_step_check_awaits_approval_for_sensitive_tool():
    state = {"plan": [{"tool": "disable_user", "args": {"username": "x"}}], "plan_index": 0}
    assert route_after_step_check(state) == "await_approval"


def test_route_after_step_check_executes_non_sensitive_tool_directly():
    state = {"plan": [{"tool": "grant_access", "args": {"username": "x", "resource": "vpn"}}], "plan_index": 0}
    assert route_after_step_check(state) == "execute_step"


def test_describe_result_create_user_lists_access():
    raw = json.dumps({"username": "tuser", "status": "active", "access_grants": ["vpn", "github:engineering"]})
    desc = _describe_result("create_user", {"username": "tuser"}, raw)
    assert desc == "Created account for tuser with access to vpn, github:engineering."


def test_describe_result_disable_user():
    raw = json.dumps({"username": "jsmith", "status": "disabled"})
    desc = _describe_result("disable_user", {"username": "jsmith"}, raw)
    assert desc == "Disabled account for jsmith."


def test_describe_result_grant_access():
    raw = json.dumps({"username": "bwayne", "access_grants": ["vpn"]})
    desc = _describe_result("grant_access", {"username": "bwayne", "resource": "vpn"}, raw)
    assert desc == "Granted vpn access to bwayne."


def test_describe_result_revoke_access():
    raw = json.dumps({"username": "bwayne", "access_grants": []})
    desc = _describe_result("revoke_access", {"username": "bwayne", "resource": "vpn"}, raw)
    assert desc == "Revoked vpn access from bwayne."


def test_describe_result_falls_back_on_unparseable_result():
    desc = _describe_result("get_user", {"username": "jsmith"}, "not json")
    assert desc == "Looked up jsmith."


def test_describe_result_unknown_tool_uses_generic_message():
    raw = json.dumps({"username": "jsmith"})
    desc = _describe_result("some_future_tool", {"username": "jsmith"}, raw)
    assert desc == "some_future_tool completed for jsmith."


def test_mask_pii_for_prompt_strips_full_name_and_email():
    raw = json.dumps({
        "username": "jsmith", "full_name": "Jane Smith", "email": "jsmith@example.com",
        "department": "Engineering", "status": "active", "access_grants": ["vpn"],
    })
    masked = json.loads(_mask_pii_for_prompt(raw))
    assert masked == {
        "username": "jsmith", "department": "Engineering",
        "status": "active", "access_grants": ["vpn"],
    }
    assert "full_name" not in masked
    assert "email" not in masked


def test_mask_pii_for_prompt_leaves_planning_relevant_fields_intact():
    raw = json.dumps({"username": "jsmith", "status": "disabled", "access_grants": []})
    masked = json.loads(_mask_pii_for_prompt(raw))
    assert masked == {"username": "jsmith", "status": "disabled", "access_grants": []}


def test_mask_pii_for_prompt_passes_through_non_json_unchanged():
    assert _mask_pii_for_prompt("User already exists: 'jsmith'") == "User already exists: 'jsmith'"


def test_mask_pii_for_prompt_passes_through_non_dict_json_unchanged():
    assert _mask_pii_for_prompt("[1, 2, 3]") == "[1, 2, 3]"
