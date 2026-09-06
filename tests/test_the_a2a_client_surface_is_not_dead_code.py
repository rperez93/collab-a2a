"""A protocol surface is not decided by who happens to call it.

`HubClient.agent_card` was removed by a dead-code sweep that was correct about
the reference graph and wrong about the question. `/.well-known/agent-card.json`
is A2A's discovery endpoint; the hub publishes the card and this class is the
client that talks to the hub, so the method belongs there whether or not
anything in this repository asks for it. Somebody writing against this client
should find it where the specification says it is.

This test exists so the next sweep — mine or anybody's — finds a reader.
"""

from __future__ import annotations

import inspect

from collab.client.hub_client import HubClient


def test_the_client_can_ask_for_the_agent_card():
    """The method exists, and asks the well-known path A2A defines."""
    assert hasattr(HubClient, "agent_card")
    source = inspect.getsource(HubClient.agent_card)
    assert "/.well-known/agent-card.json" in source
    assert '"GET"' in source


def test_it_asks_the_root_and_not_the_extension_prefix():
    """The card is discovered at the ROOT, not under collab's own prefix.

    Every other route on this client is namespaced under `EXT_PREFIX`, which is
    collab's extension to A2A rather than A2A itself. The card is not ours to
    namespace: a client that looked for it under our prefix would fail against
    any other A2A server, which is the whole point of a well-known path.
    """
    from collab.client.hub_client import EXT_PREFIX

    source = inspect.getsource(HubClient.agent_card)
    assert EXT_PREFIX not in source
