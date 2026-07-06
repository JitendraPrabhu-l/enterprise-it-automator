"""Tests for the MCP discovery phase actually feeding the planner (Stage
X, closes an audit gap): app.agent.graph.discover_tool_reference() calls
the real tools/list endpoint and formats whatever the server currently
exposes, instead of a hand-maintained static string that could silently
drift from what the server actually serves.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.agent import graph as graph_module
from app.agent.graph import discover_tool_reference


@dataclass
class _FakeTool:
    name: str
    description: str
    inputSchema: dict


@dataclass
class _FakeListToolsResult:
    tools: list


class _FakeSession:
    def __init__(self, tools: list[_FakeTool]):
        self._tools = tools

    async def list_tools(self) -> _FakeListToolsResult:
        return _FakeListToolsResult(tools=self._tools)


def _patch_session(monkeypatch, tools: list[_FakeTool]):
    @asynccontextmanager
    async def _fake_mcp_session(tool_name=None):
        yield _FakeSession(tools)

    monkeypatch.setattr(graph_module, "mcp_session", _fake_mcp_session)


async def test_discover_tool_reference_lists_required_and_optional_args(monkeypatch):
    _patch_session(
        monkeypatch,
        [
            _FakeTool(
                name="identity_create_user",
                description="Provision a new employee identity.\nMore detail on a second line.",
                inputSchema={
                    "properties": {
                        "username": {"type": "string"},
                        "full_name": {"type": "string"},
                        "email": {"type": "string"},
                        "department": {"type": "string", "default": ""},
                    },
                    "required": ["username", "full_name", "email"],
                },
            )
        ],
    )
    ref = await discover_tool_reference()
    assert ref == "- identity_create_user(username, full_name, email, department?) -> Provision a new employee identity."


async def test_discover_tool_reference_hides_approval_id_and_ticket_id(monkeypatch):
    _patch_session(
        monkeypatch,
        [
            _FakeTool(
                name="identity_disable_user",
                description="Disable an employee's account.",
                inputSchema={
                    "properties": {
                        "username": {"type": "string"},
                        "approval_id": {"type": "integer"},
                        "ticket_id": {"type": "integer"},
                    },
                    "required": ["username", "approval_id"],
                },
            )
        ],
    )
    ref = await discover_tool_reference()
    assert ref == "- identity_disable_user(username) -> Disable an employee's account."
    assert "approval_id" not in ref
    assert "ticket_id" not in ref


async def test_discover_tool_reference_excludes_non_plannable_meta_tools(monkeypatch):
    _patch_session(
        monkeypatch,
        [
            _FakeTool(
                name="is_sensitive_action",
                description="Report whether a tool name requires human approval.",
                inputSchema={"properties": {"tool_name": {"type": "string"}}, "required": ["tool_name"]},
            ),
            _FakeTool(
                name="identity_get_user",
                description="Look up an employee record.",
                inputSchema={"properties": {"username": {"type": "string"}}, "required": ["username"]},
            ),
        ],
    )
    ref = await discover_tool_reference()
    assert "is_sensitive_action" not in ref
    assert "identity_get_user(username)" in ref


async def test_discover_tool_reference_tool_with_no_args(monkeypatch):
    _patch_session(
        monkeypatch,
        [
            _FakeTool(
                name="ticketing_get_ticket_status",
                description="Look up ticket status.",
                inputSchema={"properties": {"ticket_id": {"type": "integer"}}, "required": ["ticket_id"]},
            )
        ],
    )
    ref = await discover_tool_reference()
    # ticket_id is executor-injected and filtered out — this tool ends up
    # with no visible args at all, which is correct given nothing today
    # populates ticket_id for it (see graph.py's _EXECUTOR_INJECTED_ARGS
    # comment) — not something this discovery formatting should paper over.
    assert ref == "- ticketing_get_ticket_status() -> Look up ticket status."


async def test_discover_tool_reference_empty_when_no_tools(monkeypatch):
    _patch_session(monkeypatch, [])
    ref = await discover_tool_reference()
    assert ref == ""
