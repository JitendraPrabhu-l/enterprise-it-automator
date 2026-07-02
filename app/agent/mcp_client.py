"""Agent-side MCP client.

Talks to the custom MCP server over stdio JSON-RPC by spawning it as a
subprocess — the same integration path a real orchestrator (e.g. watsonx
Orchestrate) would use to register an external MCP tool server, as opposed to
importing the tool functions directly in-process.
"""

import sys
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def mcp_session():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server.server"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def list_tools(session: ClientSession) -> list[dict]:
    result = await session.list_tools()
    return [
        {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
        for t in result.tools
    ]


async def call_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
    result = await session.call_tool(name, arguments)
    if result.isError:
        text = "; ".join(
            block.text for block in result.content if hasattr(block, "text")
        )
        raise RuntimeError(f"MCP tool {name!r} failed: {text}")
    for block in result.content:
        if hasattr(block, "text"):
            return block.text
    return None
