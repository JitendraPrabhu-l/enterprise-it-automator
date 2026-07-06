"""Ticketing domain: posting status updates back to an external
ticketing/ITSM system (Jira, ServiceNow, etc.).

This is explicitly a SIMULATION — a real deployment would have this server
call out to Jira's/ServiceNow's actual REST API. The tool contract (post a
comment, look up current status) is representative of what a real
ticket-sync integration looks like, and lives in its own domain server on
purpose: an orchestration agent legitimately needs to update the *system of
record for the ticket itself*, not just the identity/access backends,
mirroring how real IT environments separate "the ticketing tool" from "the
directory service" as different systems entirely.

Standalone FastMCP instance so this domain could run as its own process
with its own deploy/scale profile — see server.py, which composes this
alongside identity_server and access_server under one gateway process for
local/dev use via add_tool() namespacing.
"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.db.session import session_scope
from app.mcp_server import tools as t

ticketing_mcp = FastMCP("ticketing")

# openWorldHint=False on both tools below reflects the CURRENT simulated
# implementation (this domain only touches this app's own local Ticket
# table — see module docstring). If this simulation is ever replaced with
# a real Jira/ServiceNow API call, openWorldHint should flip to True, since
# the tool would then interact with an external system this app doesn't
# control.


@ticketing_mcp.tool(
    # Not idempotentHint: each call appends another comment
    # (tools.py's add_ticket_comment does `f"{existing}\n{comment}"`) — two
    # identical calls produce two comments, a different effect than one.
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
async def add_ticket_comment(ticket_id: int, comment: str) -> dict:
    """Post a status comment back to the external ticketing system for this
    ticket. Not sensitive — informational only, doesn't mutate identity or
    access state."""
    async with session_scope() as session:
        return await t.add_ticket_comment(session, ticket_id, comment)


@ticketing_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def get_ticket_status(ticket_id: int) -> dict:
    """Look up the current status of a ticket in the external ticketing
    system (simulated: reflects this app's own Ticket record, which is
    where a real sync job would have last written it)."""
    async with session_scope() as session:
        return await t.get_ticket_status(session, ticket_id)
