import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class PlannedAction(TypedDict):
    tool: str
    args: dict[str, Any]
    reasoning: str


class StepResult(TypedDict):
    tool: str
    args: dict[str, Any]
    result: str
    ok: bool


class BatchStepInput(TypedDict):
    ticket_id: int
    tool: str
    args: dict[str, Any]
    reasoning: str


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    ticket_id: int
    ticket_text: str
    category: str
    plan: list[PlannedAction]
    plan_index: int
    pending_approval_id: int | None
    results: Annotated[list[StepResult], operator.add]
    done: bool
    error: str | None
    replan_count: int
