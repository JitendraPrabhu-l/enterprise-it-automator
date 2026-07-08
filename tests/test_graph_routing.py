import json

import pytest

from app.agent.graph import (
    MAX_PLAN_LENGTH,
    _describe_result,
    _extract_json_array,
    _extract_username,
    _mask_pii_for_prompt,
    _required_action_missing,
    finalize_node,
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
    """add_ticket_comment, not grant_access — grant_access joined the
    sensitive set after a security review found it ran with zero human
    review (see is_sensitive's tests in test_tools.py)."""
    state = {"plan": [{"tool": "add_ticket_comment", "args": {"ticket_id": 1, "comment": "note"}}], "plan_index": 0}
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


# --- _required_action_missing / finalize_node: OFFBOARDING tickets that
# never actually called disable_user must be marked FAILED, not COMPLETED
# --------------------------------------------------------------------
#
# Found live: an offboarding ticket for an employee already disabled (or
# never existing at all) resolved as COMPLETED having never called
# disable_user at all — indistinguishable at a glance in the dashboard from
# a ticket that genuinely offboarded someone. The employee's end-state may
# already match what was asked, but the ticket's own stated task was never
# carried out THIS run.


def test_required_action_missing_true_when_disable_user_never_ran():
    assert _required_action_missing("OFFBOARDING", []) is True


def test_required_action_missing_false_when_disable_user_succeeded():
    results = [{"tool": "identity_disable_user", "args": {}, "result": "ok", "ok": True}]
    assert _required_action_missing("OFFBOARDING", results) is False


def test_required_action_missing_false_when_disable_user_succeeded_bare_name():
    results = [{"tool": "disable_user", "args": {}, "result": "ok", "ok": True}]
    assert _required_action_missing("OFFBOARDING", results) is False


def test_required_action_missing_true_when_disable_user_only_failed():
    """A FAILED disable_user attempt doesn't count as having run it — only
    a successful one satisfies the required action."""
    results = [{"tool": "identity_disable_user", "args": {}, "result": "already disabled", "ok": False}]
    assert _required_action_missing("OFFBOARDING", results) is True


def test_required_action_missing_true_when_only_a_comment_was_posted():
    results = [
        {"tool": "ticketing_add_ticket_comment", "args": {}, "result": "posted", "ok": True},
    ]
    assert _required_action_missing("OFFBOARDING", results) is True


def test_required_action_missing_not_applicable_to_access_change():
    """ACCESS_CHANGE genuinely has no single required action (grant vs.
    revoke vs. neither, if access already matches) — must never flag."""
    assert _required_action_missing("ACCESS_CHANGE", []) is False


def test_required_action_missing_not_applicable_to_onboarding():
    assert _required_action_missing("ONBOARDING", []) is False


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.db import session as db_session_module

    db_path = tmp_path / "finalize_node_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield db_session_module
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


def _base_state(**overrides) -> dict:
    state = {
        "messages": [], "ticket_id": 1, "ticket_text": "irrelevant",
        "category": "OFFBOARDING", "plan": [], "plan_index": 0,
        "pending_approval_id": None, "results": [], "done": False, "error": None,
    }
    state.update(overrides)
    return state


async def test_finalize_marks_offboarding_ticket_failed_when_disable_user_never_ran(isolated_db):
    from app.db.models import Ticket, TicketStatus

    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.EXECUTING))

    results = [
        {"tool": "ticketing_add_ticket_comment", "args": {}, "result": "posted", "ok": True},
    ]
    await finalize_node(_base_state(results=results))

    async with isolated_db.session_scope() as session:
        ticket = await session.get(Ticket, 1)
        assert ticket.status == TicketStatus.FAILED
        assert "offboard" in ticket.result_summary.lower()


async def test_finalize_completes_offboarding_ticket_when_disable_user_succeeded(isolated_db):
    from app.db.models import Ticket, TicketStatus

    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.EXECUTING))

    results = [
        {"tool": "identity_disable_user", "args": {"username": "jsmith"}, "result": "ok", "ok": True},
    ]
    await finalize_node(_base_state(results=results))

    async with isolated_db.session_scope() as session:
        ticket = await session.get(Ticket, 1)
        assert ticket.status == TicketStatus.COMPLETED


async def test_finalize_still_completes_access_change_ticket_with_no_actions(isolated_db):
    """The fix is deliberately scoped to OFFBOARDING — an ACCESS_CHANGE
    ticket resolving with zero actions (e.g. requested access already
    granted) must remain COMPLETED, unchanged from before."""
    from app.db.models import Ticket, TicketStatus

    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.EXECUTING))

    await finalize_node(_base_state(category="ACCESS_CHANGE", results=[]))

    async with isolated_db.session_scope() as session:
        ticket = await session.get(Ticket, 1)
        assert ticket.status == TicketStatus.COMPLETED


async def test_finalize_still_fails_ticket_on_real_error_regardless_of_category(isolated_db):
    from app.db.models import Ticket, TicketStatus

    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.EXECUTING))

    await finalize_node(_base_state(error="Planner returned malformed JSON"))

    async with isolated_db.session_scope() as session:
        ticket = await session.get(Ticket, 1)
        assert ticket.status == TicketStatus.FAILED
        assert ticket.result_summary == "Planner returned malformed JSON"
