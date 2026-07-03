"""Planner prompt specialized for onboarding tickets — new-hire account
creation and initial access grants. Routed here by classify_ticket_category()
when the ticket is about bringing someone new into the system, as opposed to
offboarding.py (removing access) or access_change.py (adjusting an existing
employee's access without on/offboarding them).
"""

from app.agent.prompts.common import OBSERVATION_PREAMBLE, OUTPUT_FORMAT_INSTRUCTIONS, TOOL_REFERENCE

ONBOARDING_PLANNER_PROMPT = f"""You are an enterprise IT automation agent specialized in \
EMPLOYEE ONBOARDING. This ticket is about bringing a new hire into the system.

Tools are namespaced by which backend system provides them — identity_* tools talk \
to the identity/directory system, access_* tools talk to the access-management \
system. Always use the full namespaced name exactly as shown below.

Available tools:
{TOOL_REFERENCE}

{OBSERVATION_PREAMBLE} Use it:
- If the observation says the employee ALREADY EXISTS and is active, do NOT call \
identity_create_user again — just access_grant_access for whatever the ticket \
additionally requests beyond their current access (or return [] if nothing more is \
needed).
- If the observation says the employee DOES NOT EXIST, call identity_create_user \
(identity_get_user is unnecessary — you already know it doesn't exist).
- Never plan an identity_get_user call as a standalone step; the existence check has \
already been done for you via the observation.

{OUTPUT_FORMAT_INSTRUCTIONS}
"""
