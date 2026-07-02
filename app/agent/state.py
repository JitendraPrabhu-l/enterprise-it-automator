from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class PlannedAction(TypedDict):
    tool: str
    args: dict[str, Any]
    reasoning: str


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    ticket_id: int
    ticket_text: str
    plan: list[PlannedAction]
    plan_index: int
    pending_approval_id: int | None
    results: list[dict]
    done: bool
    error: str | None
