"""Historical project IDs do not identify a later project's creator."""

import pytest

from collab.protocol import Envelope
from collab.server.app import _may_change
from collab.server.store import Store


@pytest.mark.parametrize("new_announcement", [False, True])
def test_a_reused_project_id_does_not_recover_the_deleted_projects_creator(
        tmp_path, monkeypatch, new_announcement):
    """Missing publication is unknown provenance, not permission for an old creator."""
    path = tmp_path / "projects.db"
    store = Store(path)
    old = store.add_participant("old", "synthetic-old")
    new = store.add_participant("new", "synthetic-new")
    try:
        monkeypatch.setattr("collab.server.store.time.time", lambda: 1577836800.0)
        store.upsert_project("P_reused", title="Old", owner=None,
                             created_by=old.name, created_by_id=old.id)
        store.append(Envelope(kind="project", sender=old.name, sender_id=old.id,
                              ts="2020-01-01T00:00:01+00:00",
                              body={"action": "propose", "id": "P_reused"}))
        store.delete_project("P_reused")
        monkeypatch.setattr("collab.server.store.time.time", lambda: 1577836810.0)
        store.upsert_project("P_reused", title="New", owner=None,
                             created_by=new.name, created_by_id=new.id)
        if new_announcement:
            store.append(Envelope(kind="project", sender=new.name, sender_id=new.id,
                                  ts="2020-01-01T00:00:11+00:00",
                                  body={"action": "propose", "id": "P_reused"}))
        # Reproduce the owner-base schema, before creator identity was stored.
        store._db.execute("ALTER TABLE projects DROP COLUMN created_by_id")
        store._db.commit()
    finally:
        store.close()
    reopened = Store(path)
    try:
        record = reopened.get_project("P_reused")
        # Even a chronological event is not durable incarnation provenance:
        # an old request can publish late. Legacy creators stay host-managed.
        assert record["created_by_id"] is None
        assert not _may_change(old, record)
        assert not _may_change(new, record)
    finally:
        reopened.close()


def test_conflicting_project_creator_evidence_remains_unresolved(tmp_path, monkeypatch):
    """Recovery cannot choose an arbitrary principal from conflicting proposals."""
    path = tmp_path / "ambiguous.db"
    store = Store(path)
    old = store.add_participant("old", "synthetic-old")
    new = store.add_participant("new", "synthetic-new")
    try:
        monkeypatch.setattr("collab.server.store.time.time", lambda: 1577836800.0)
        store.upsert_project("P_ambiguous", title="Synthetic", owner=None,
                             created_by=new.name, created_by_id=new.id)
        for person in (old, new):
            store.append(Envelope(kind="project", sender=person.name, sender_id=person.id,
                                  ts="2020-01-01T00:00:01+00:00",
                                  body={"action": "propose", "id": "P_ambiguous"}))
        store._db.execute("ALTER TABLE projects DROP COLUMN created_by_id")
        store._db.commit()
    finally:
        store.close()
    reopened = Store(path)
    try:
        record = reopened.get_project("P_ambiguous")
        assert record["created_by_id"] is None
        assert not _may_change(old, record)
        assert not _may_change(new, record)
    finally:
        reopened.close()


def test_a_late_old_proposal_does_not_authorize_the_replacement_project(tmp_path, monkeypatch):
    """An awaited old publication can land after deletion and same-ID recreation."""
    path = tmp_path / "late.db"
    store = Store(path)
    old = store.add_participant("old", "synthetic-old")
    new = store.add_participant("new", "synthetic-new")
    try:
        monkeypatch.setattr("collab.server.store.time.time", lambda: 1577836800.0)
        store.upsert_project("P_reused", title="Old", owner=None,
                             created_by=old.name, created_by_id=old.id)
        store.delete_project("P_reused")
        monkeypatch.setattr("collab.server.store.time.time", lambda: 1577836810.0)
        store.upsert_project("P_reused", title="New", owner=None,
                             created_by=new.name, created_by_id=new.id)
        # The old HTTP action resumes after its to_thread write; its envelope
        # timestamp is created now. The replacement's publication never lands.
        store.append(Envelope(kind="project", sender=old.name, sender_id=old.id,
                              ts="2020-01-01T00:00:11+00:00",
                              body={"action": "propose", "id": "P_reused"}))
        store._db.execute("ALTER TABLE projects DROP COLUMN created_by_id")
        store._db.commit()
    finally:
        store.close()
    reopened = Store(path)
    try:
        record = reopened.get_project("P_reused")
        assert record["created_by_id"] is None
        assert not _may_change(old, record)
    finally:
        reopened.close()
