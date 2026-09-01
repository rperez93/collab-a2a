"""A participant's self-declared text is bounded before it reaches the roster.

Everything a joiner says about itself — its name, its focus, its repo, a task
title — is stored and then replayed to every participant's roster every few
seconds. Left unbounded, a megabyte of display name is amplified across the
whole room on a timer, and a nested object smuggled in through `hello` would
reach the roster without passing the sanitiser its own field has. These tests
pin the caps that stop both.
"""

from __future__ import annotations

from collab.protocol import (MAX_META_KEYS, MAX_META_VALUE, MAX_NAME,
                             bounded_meta, clip)


def test_clip_trims_and_bounds_a_field():
    """A field is stripped and cut to its limit, whatever arrives."""
    assert clip("  alice  ", MAX_NAME) == "alice"
    assert len(clip("x" * 10_000, MAX_NAME)) == MAX_NAME


def test_bounded_meta_caps_each_string_value():
    """A megabyte focus does not get to ride every roster snapshot."""
    out = bounded_meta({"focus": "x" * 100_000})
    assert len(out["focus"]) == MAX_META_VALUE


def test_bounded_meta_caps_the_number_of_keys():
    """A thousand junk keys are not a thousand columns on everyone's roster."""
    out = bounded_meta({f"k{i}": "v" for i in range(1000)})
    assert len(out) <= MAX_META_KEYS


def test_bounded_meta_drops_nested_objects():
    """`stats` and `activity` have their own sanitisers; hello is scalars only.

    A joiner could otherwise smuggle an unbounded, unsanitised stats blob into
    its meta through the join handshake and have every roster carry it.
    """
    out = bounded_meta({"focus": "real", "stats": {"cost_usd": "x" * 10_000},
                        "activity": {"what": "y" * 10_000}, "files": ["a"] * 999})
    assert out == {"focus": "real"}


def test_bounded_meta_keeps_scalars_of_the_right_shape():
    """Booleans and numbers are legitimate hello values and survive intact."""
    out = bounded_meta({"repo": "collab", "dirty": True, "ahead": 3})
    assert out == {"repo": "collab", "dirty": True, "ahead": 3}


def test_bounded_meta_ignores_a_non_dict():
    assert bounded_meta("not a dict") == {}
    assert bounded_meta(None) == {}


def test_join_bounds_a_hostile_name_and_hello(client, session):
    """A joiner cannot store a megabyte name or a nested blob in the roster.

    The join handshake reaches the store directly, so the cap has to hold at the
    endpoint and not only in the helper: an unbounded name would be replayed to
    every roster, and a nested `stats` in hello would sidestep its sanitiser.
    """
    r = client.post("/ext/collab/v1/join", json={
        "invite": session["invite"],
        "name": "z" * 5000,
        "hello": {"focus": "f" * 5000, "stats": {"cost_usd": "9" * 5000}},
    })
    assert r.status_code == 200, r.text
    joined = r.json()
    token = joined["token"]

    snap = client.get("/ext/collab/v1/snapshot",
                      headers={"Authorization": f"Bearer {token}"}).json()
    me = next(p for p in snap["participants"] if p["id"] == joined["id"])
    assert len(me["name"]) <= MAX_NAME
    assert len(me["focus"]) <= MAX_META_VALUE
    # The smuggled stats object never made it onto the roster as declared meta.
    assert me["stats"] == {}
