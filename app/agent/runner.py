"""Drives the agent graph, persisting interrupted runs so approval can resume
them later from a separate HTTP request (a different process tick entirely) —
including across an application restart, since the checkpointer is backed by
a durable store (SQLite file or Postgres) rather than kept in memory.
"""

import asyncio
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.agent.graph import compile_graph
from app.agent.mcp_session_cache import ticket_run_session
from app.config import get_settings

_graph = None
_checkpointer_cm = None
_init_lock = asyncio.Lock()


def _is_postgres_url(checkpoint_db_path: str) -> bool:
    return checkpoint_db_path.startswith("postgresql://") or checkpoint_db_path.startswith(
        "postgres://"
    )


async def _get_graph():
    global _graph, _checkpointer_cm
    if _graph is not None:
        return _graph
    async with _init_lock:
        if _graph is None:
            checkpoint_db_path = get_settings().checkpoint_db_path
            if _is_postgres_url(checkpoint_db_path):
                # Imported lazily so the sqlite-only default path never
                # requires psycopg's native libpq wrapper to be installed.
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                _checkpointer_cm = AsyncPostgresSaver.from_conn_string(checkpoint_db_path)
                checkpointer = await _checkpointer_cm.__aenter__()
                await checkpointer.setup()
            else:
                Path(checkpoint_db_path).parent.mkdir(parents=True, exist_ok=True)
                _checkpointer_cm = AsyncSqliteSaver.from_conn_string(checkpoint_db_path)
                checkpointer = await _checkpointer_cm.__aenter__()
            _graph = compile_graph(checkpointer=checkpointer)
        return _graph


def _thread_config(ticket_id: int) -> dict:
    return {"configurable": {"thread_id": f"ticket-{ticket_id}"}}


async def start_ticket_run(ticket_id: int, ticket_text: str) -> dict:
    """Runs the graph until completion or the first HITL interrupt."""
    graph = await _get_graph()
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
    config = _thread_config(ticket_id)
    async with ticket_run_session(ticket_id):
        result = await graph.ainvoke(initial_state, config=config)
    return await _summarize(graph, ticket_id, result, config)


async def resume_ticket_run(ticket_id: int) -> dict:
    """Resumes a graph previously paused at an await_approval interrupt.

    Safe to call even if the approval was rejected — execute_step / finalize
    will surface the rejection via require_approval raising inside the MCP
    tool call, which the executor already catches and records as a failure.

    Opens its own fresh MCP session (via ticket_run_session), separate from
    whatever session start_ticket_run used before the interrupt — a live
    subprocess/connection can't survive the pause, which may be hours long
    and cross a process restart, unlike the DB-backed checkpoint state.
    """
    graph = await _get_graph()
    config = _thread_config(ticket_id)
    async with ticket_run_session(ticket_id):
        result = await graph.ainvoke(Command(resume=True), config=config)
    return await _summarize(graph, ticket_id, result, config)


async def _summarize(graph, ticket_id: int, result: dict, config: dict) -> dict:
    snapshot = await graph.aget_state(config)
    interrupts = getattr(snapshot, "interrupts", None) or ()
    pending_approval = interrupts[0].value if interrupts else None

    return {
        "ticket_id": ticket_id,
        "done": result.get("done", False),
        "plan": result.get("plan", []),
        "results": result.get("results", []),
        "error": result.get("error"),
        "interrupted": bool(interrupts),
        "pending_approval": pending_approval,
    }
