"""`batch.OPEN` names a value in the data model, not a branch in the code.

A dead-code sweep removed it: only `CLOSED` is ever compared against here — a
batch is open by being not closed — so the reference graph found no reader and
was right about that and wrong about the question, in the same way it was wrong
about `HubClient.agent_card`.

The string `"open"` is the schema's DEFAULT, it is what every `status --json`
payload carries, and it is how the knowledge bundle describes the board. The
vocabulary is read whether or not this spelling of it is, and a constant that
names a value somebody else can see is not dead because this package happens to
compare against the other one.
"""

from __future__ import annotations

from collab import batch
from collab.server.store import SCHEMA, Store


def test_both_states_are_named():
    assert batch.OPEN == "open"
    assert batch.CLOSED == "closed"


def test_the_names_match_what_the_schema_writes():
    """The constant and the DEFAULT must not be able to drift apart."""
    assert f"DEFAULT '{batch.OPEN}'" in SCHEMA


def test_a_new_batch_is_stored_in_the_state_the_constant_names():
    store = Store(":memory:")
    try:
        made = store.add_batch("B_1", name="a batch", opened_by="p_1")
        assert made is not None
        assert made["state"] == batch.OPEN
        closed = store.close_batch("B_1")
        assert closed is not None and closed["state"] == batch.CLOSED
    finally:
        store.close()


def test_the_two_states_are_the_whole_vocabulary():
    """A third would need a reader here, and there is nowhere to add one quietly."""
    assert {batch.OPEN, batch.CLOSED} == {"open", "closed"}
