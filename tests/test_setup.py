"""F8 regressions: content-based graph fingerprint and startup readiness."""
import copy

import pytest

import contraflow.graphdb as gdb
from contraflow.graphdb import DB, connect_db, model_fingerprint
from contraflow.model import build

NET = build()


def test_fingerprint_changes_with_content_not_just_counts():
    other = copy.deepcopy(NET)
    seg = next(s for s in other.segments if abs(s["len"] - 465.21) < 0.01)
    seg["len"] = 1465.21
    assert model_fingerprint(other) != model_fingerprint(NET)
    other2 = copy.deepcopy(NET)
    other2.segments[0]["coords"][0][0] += 0.0001            # geometry change
    assert model_fingerprint(other2) != model_fingerprint(NET)


def test_changed_length_forces_reload(monkeypatch):
    monkeypatch.setattr(gdb, "BASE", "test_fp_probe")
    db = DB()
    try:
        assert db.load_base(NET, force=True)["loaded"]
        assert not db.load_base(NET)["loaded"]
        other = copy.deepcopy(NET)
        seg = next(s for s in other.segments if abs(s["len"] - 465.21) < 0.01)
        seg["len"] = 1465.21
        other.index()
        assert db.load_base(other)["loaded"]
        stored = db.graph("test_fp_probe").q("t", "MATCH ()-[e:ROAD {eid: $e}]->() RETURN e.len", {"e": seg["eid"]})
        assert stored[0][0] == pytest.approx(1465.21)
    finally:
        db.delete("test_fp_probe")


def test_connect_db_fails_clearly_when_unreachable():
    with pytest.raises(RuntimeError, match="unreachable"):
        connect_db(attempts=2, delay=0, port=6399)


def test_connect_db_succeeds_when_ready():
    assert connect_db(attempts=3, delay=0).client.connection.ping()
