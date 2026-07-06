"""Translates a LangGraph ticket run into a stream of AG-UI protocol events
(https://docs.ag-ui.com — spec version current as of 2026-07), so a real
AG-UI-compliant frontend (or any SSE client) can watch a ticket's plan
execute live instead of polling GET /tickets every few seconds.

This is an additional transport for the SAME agent run app/agent/runner.py
already drives — it does not replace start_ticket_run/resume_ticket_run or
the plain POST /tickets JSON endpoint, which stay exactly as they are for
callers that just want a final result.

Event mapping, chosen to reflect what this graph actually does rather than
force every AG-UI event type in for its own sake:
  RUN_STARTED             - run begins (thread_id = f"ticket-{id}", matching
                             runner.py's LangGraph thread_id so both transports
                             address the same checkpointed run)
  STEP_STARTED/FINISHED    - one pair per graph node LangGraph reports via
                             stream_mode="updates" (classify, plan, execute_step,
                             execute_batch_step, replan, finalize, ...)
  TOOL_CALL_START/RESULT   - one pair per MCP tool call that shows up in a
                             node's `results` delta (execute_step/execute_batch_step) —
                             no ARGS/END streaming since tool args/results arrive
                             whole from LangGraph, not token-by-token
  STATE_DELTA              - JSON Patch (RFC 6902) fragments for plan_index/done
                             progress after each node, so a client can render
                             "step 2 of 5" without re-deriving it from tool events
  RUN_FINISHED             - normal completion (outcome=success) OR the graph
                             paused for approval (outcome=interrupt, carrying an
                             ag_ui.core.Interrupt built from the same payload
                             await_approval_node passed to LangGraph's interrupt())
  RUN_ERROR                - an exception escaped the graph run entirely
                             (distinct from a tool call's own ok:False result,
                             which is reported via TOOL_CALL_RESULT instead)

Approval-interrupt shape: AG-UI's Interrupt type (id, reason, message,
tool_call_id, metadata) maps directly onto the dict await_approval_node
already passes to interrupt() in app/agent/graph.py — reusing those exact
keys (not inventing a parallel schema) keeps this bridge a pure translation
layer rather than a second source of truth for what an approval means.
"""

import json
import logging
from typing import Any, AsyncIterator

from ag_ui.core import (
    Interrupt,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StateDeltaEvent,
    StepFinishedEvent,
    StepStartedEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from app.agent.mcp_session_cache import ticket_run_session

logger = logging.getLogger(__name__)


def _thread_config(ticket_id: int) -> dict:
    return {"configurable": {"thread_id": f"ticket-{ticket_id}"}}


def _state_delta_ops(node_name: str, delta: dict[str, Any]) -> list[dict[str, Any]]:
    """A minimal, honest JSON Patch fragment — only the two fields a client
    actually needs to render live progress (plan position, completion),
    added via "add" (not "replace") since AG-UI clients start from an empty
    shared-state document for each run rather than one this bridge has to
    know the prior shape of.
    """
    ops = []
    if "plan_index" in delta:
        ops.append({"op": "add", "path": "/plan_index", "value": delta["plan_index"]})
    if "done" in delta:
        ops.append({"op": "add", "path": "/done", "value": delta["done"]})
    if "category" in delta:
        ops.append({"op": "add", "path": "/category", "value": delta["category"]})
    if not ops:
        ops.append({"op": "add", "path": "/last_node", "value": node_name})
    return ops


async def _tool_events_for_delta(delta: dict[str, Any]) -> list[Any]:
    """execute_step/execute_batch_step report completed tool calls whole
    (LangGraph's `results` reducer, see app/agent/state.py) rather than
    streaming — so each becomes one TOOL_CALL_START immediately followed by
    one TOOL_CALL_RESULT, not a separately-observed start then a later
    result the way a token-streamed LLM tool call would.
    """
    events: list[Any] = []
    for step_result in delta.get("results", []):
        tool_call_id = f"{step_result['tool']}-{id(step_result)}"
        events.append(ToolCallStartEvent(tool_call_id=tool_call_id, tool_call_name=step_result["tool"]))
        content = step_result["result"] if step_result["ok"] else f"ERROR: {step_result['result']}"
        events.append(
            ToolCallResultEvent(
                message_id=tool_call_id,
                tool_call_id=tool_call_id,
                content=content if isinstance(content, str) else json.dumps(content),
            )
        )
    return events


def _interrupt_from_payload(payload: dict[str, Any]) -> Interrupt:
    """Builds an ag_ui.core.Interrupt straight from the dict
    await_approval_node passes to LangGraph's interrupt() — same field
    names, no parallel schema (see module docstring)."""
    return Interrupt(
        id=str(payload.get("approval_id", "")),
        reason=payload.get("reason", "sensitive_action_requires_approval"),
        message=(
            f"Approval required for {payload.get('tool')}({payload.get('args')}): "
            f"{payload.get('agent_reasoning', '')}"
        ),
        tool_call_id=payload.get("tool"),
        metadata={
            "ticket_id": payload.get("ticket_id"),
            "approval_id": payload.get("approval_id"),
            "tool": payload.get("tool"),
            "args": payload.get("args"),
        },
    )


async def _stream_graph_run(
    ticket_id: int, run_id: str, graph_input: Any
) -> AsyncIterator[Any]:
    """Shared core for both the initial run and a resume: drives the graph
    via stream_mode="updates" (one dict per completed node, keyed by node
    name) and yields AG-UI events for that node before moving to the next.

    Opens its own ticket_run_session the same way runner.py's
    start_ticket_run/resume_ticket_run do — this is a genuinely separate
    graph.astream() call, not a wrapper around those functions, since
    neither offers a hook to observe intermediate node completions.
    """
    from app.agent.runner import _get_graph

    graph = await _get_graph()
    config = _thread_config(ticket_id)

    yield RunStartedEvent(thread_id=f"ticket-{ticket_id}", run_id=run_id)

    try:
        async with ticket_run_session(ticket_id):
            async for update in graph.astream(graph_input, config=config, stream_mode="updates"):
                for node_name, delta in update.items():
                    if node_name == "__interrupt__":
                        continue
                    yield StepStartedEvent(step_name=node_name)
                    if isinstance(delta, dict):
                        for event in await _tool_events_for_delta(delta):
                            yield event
                        for op in _state_delta_ops(node_name, delta):
                            yield StateDeltaEvent(delta=[op])
                    yield StepFinishedEvent(step_name=node_name)
    except Exception as exc:
        leaf = exc
        while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
            leaf = leaf.exceptions[0]
        logger.exception("Graph run failed for ticket %s during AG-UI stream", ticket_id)
        yield RunErrorEvent(message=str(leaf), code=type(leaf).__name__)
        return

    snapshot = await graph.aget_state(config)
    interrupts = getattr(snapshot, "interrupts", None) or ()
    if interrupts:
        interrupt_obj = _interrupt_from_payload(interrupts[0].value)
        yield RunFinishedEvent(
            thread_id=f"ticket-{ticket_id}",
            run_id=run_id,
            outcome=RunFinishedInterruptOutcome(interrupts=[interrupt_obj]),
        )
        return

    result = snapshot.values
    yield RunFinishedEvent(
        thread_id=f"ticket-{ticket_id}",
        run_id=run_id,
        outcome=RunFinishedSuccessOutcome(),
        result={
            "done": result.get("done", False),
            "plan": result.get("plan", []),
            "results": result.get("results", []),
            "error": result.get("error"),
        },
    )


async def stream_ticket_run(ticket_id: int, ticket_text: str, run_id: str) -> AsyncIterator[Any]:
    """AG-UI event stream for a brand-new ticket run — the streaming
    counterpart to app.agent.runner.start_ticket_run."""
    initial_state = {
        "messages": [],
        "ticket_id": ticket_id,
        "ticket_text": ticket_text,
        "category": "",
        "plan": [],
        "plan_index": 0,
        "pending_approval_id": None,
        "results": [],
        "done": False,
        "error": None,
        "replan_count": 0,
    }
    async for event in _stream_graph_run(ticket_id, run_id, initial_state):
        yield event


async def stream_resume_run(ticket_id: int, run_id: str) -> AsyncIterator[Any]:
    """AG-UI event stream resuming a run previously paused at an
    await_approval interrupt — the streaming counterpart to
    app.agent.runner.resume_ticket_run."""
    from langgraph.types import Command

    async for event in _stream_graph_run(ticket_id, run_id, Command(resume=True)):
        yield event
