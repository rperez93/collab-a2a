"""Bind real core output to the source before opening either approval adapter."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from collab.client.consent import PreparedSkill
from collab.client.passkey_adapter import AdapterError
from collab.client.passkey_dispatch import _Prepared
from collab.server.approval_crypto import encode
from collab.server.approval_skills import SkillApprovals
from test_approval_store import hub
from test_approval_skills import source


@pytest.fixture
def binding(hub):
    original = source(hub)
    prepared = SkillApprovals(hub.api).prepare("test-bob", source_seq=original.seq)
    reviewed = hub.api.review("test-bob", prepared["request_id"])
    profile = SimpleNamespace(session_id="s_test", participant_id=hub.people["bob"].id)
    return profile, original, prepared, reviewed


def test_actual_core_review_passes_both_window_contracts(binding, hub):
    item = PreparedSkill.bind(*binding, now=hub.now[0])
    window = item.window_request(encode(b"passkey credential"))
    parsed = _Prepared.parse(window)
    assert parsed.canonical == item.canonical
    assert parsed.requester_id == binding[1].sender_id
    assert window["review_text"].encode() == item.canonical
    # The redundant object cannot substitute the authenticated canonical bytes.
    binding[3]["review"] = {"payload": "a different operation"}
    assert PreparedSkill.bind(*binding, now=hub.now[0]).canonical == item.canonical


@pytest.mark.parametrize("field,value", [
    ("request_id", "different"), ("digest", "0" * 64),
    ("source_seq", True), ("source_seq", 999), ("expires_at", True),
    ("expires_at", 999), ("api_version", True),
])
def test_prepare_substitution_cannot_open_a_window(binding, hub, field, value):
    binding[2][field] = value
    with pytest.raises(AdapterError):
        PreparedSkill.bind(*binding, now=hub.now[0])


@pytest.mark.parametrize("field,value", [
    ("session_id", "other"), ("target_id", "p_other"), ("requester_id", "p_other"),
    ("authority", "run-anything"), ("destination", "participant:p_other"),
])
def test_rehashed_review_cannot_change_expected_authority(binding, hub, field, value):
    review = copy.deepcopy(binding[3]["review"])
    review[field] = value
    raw = json.dumps(review).encode()
    digest = hashlib.sha256(raw).hexdigest()
    binding[2]["digest"] = binding[3]["digest"] = digest
    binding[3]["canonical_review"] = encode(raw)
    with pytest.raises(AdapterError):
        PreparedSkill.bind(*binding, now=hub.now[0])


def test_reused_short_id_with_changed_source_is_not_inherited(binding, hub):
    binding[1].body["skill_request"]["args"] = "different work with same short ID"
    with pytest.raises(AdapterError):
        PreparedSkill.bind(*binding, now=hub.now[0])


def test_renaming_does_not_change_authority_but_reusing_name_does(binding, hub):
    binding[1].sender = "new display label"
    assert PreparedSkill.bind(*binding, now=hub.now[0]).requester_name == "new display label"
    binding[1].sender_id = hub.people["carol"].id
    with pytest.raises(AdapterError):
        PreparedSkill.bind(*binding, now=hub.now[0])
