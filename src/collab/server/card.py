"""The hub's A2A Agent Card.

The card is how a stock A2A client discovers us: the transport, the auth
scheme, and — via ``capabilities.extensions`` — the fact that this agent speaks
the collab multi-party extension on top of plain A2A.
"""

from __future__ import annotations

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    StringList,
)

from .. import __version__
from ..protocol import EXTENSION_URI, RPC_PATH

SKILLS = [
    AgentSkill(
        id="chat",
        name="Discuss",
        description=(
            "Send a message to a room or directly to another participant, and "
            "receive everyone else's in real time."
        ),
        tags=["chat", "collaboration", "multi-agent"],
        examples=["can you take the client side of the auth refactor?"],
    ),
    AgentSkill(
        id="task-board",
        name="Align on tasks",
        description=(
            "Propose, claim, update and complete shared tasks so two agents "
            "never start the same piece of work."
        ),
        tags=["tasks", "coordination"],
        examples=["propose 'migrate sessions to the new store'", "claim T-3"],
    ),
    AgentSkill(
        id="learnings",
        name="Share what you learn",
        description=(
            "Record something the next agent in this repository will need, "
            "look up what the others already found out, and ask them for "
            "theirs. Scoped to the repository this session is in."
        ),
        tags=["learnings", "knowledge", "collaboration"],
        examples=["what do we know about the staging bucket?",
                  "record that the eu-west key is the one that works"],
    ),
]


def build_agent_card(public_url: str, *, session_id: str, host_name: str) -> AgentCard:
    card = AgentCard(
        name=f"collab-hub/{session_id}",
        description=(
            f"A collab hub hosted by {host_name}. Coding agents join it to talk, "
            "align on tasks and discuss work in real time."
        ),
        version=__version__,
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        documentation_url="https://github.com/rperez93/collab-a2a",
    )
    card.supported_interfaces.append(
        AgentInterface(
            url=public_url.rstrip("/") + RPC_PATH,
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        )
    )
    card.capabilities.CopyFrom(AgentCapabilities(streaming=True))
    card.capabilities.extensions.append(
        AgentExtension(
            uri=EXTENSION_URI,
            description=(
                "Multi-party collaboration: rooms, participant registry, direct "
                "messages, a shared task board, and a per-participant SSE feed "
                "with Last-Event-ID resume."
            ),
            required=False,
        )
    )
    card.security_schemes["bearer"].http_auth_security_scheme.CopyFrom(
        HTTPAuthSecurityScheme(
            scheme="bearer",
            description="Per-participant token, issued by exchanging a session invite code.",
        )
    )
    requirement = SecurityRequirement()
    requirement.schemes["bearer"].CopyFrom(StringList())
    card.security_requirements.append(requirement)
    card.skills.extend(SKILLS)
    return card
