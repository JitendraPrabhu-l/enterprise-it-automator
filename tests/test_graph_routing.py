import json

from app.agent.graph import (
    _describe_result,
    _extract_json_array,
    _extract_username,
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
