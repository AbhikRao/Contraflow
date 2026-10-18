"""Regression tests for audit findings F2 (harmful interventions) and F3 (constraint bypasses)."""
import pytest
from fastapi.testclient import TestClient

from contraflow.api import app, state
from contraflow.engine import Scenario, ValidationError, rank_key

ST = state()
ENG, NET = ST["engine"], ST["net"]
ALL = [c["id"] for x in NET.crossings for c in x["carriageways"]]
client = TestClient(app)


# ------------------------------------------------------------------ F2
def _cands(sc):
    return {c["id"]: c for c in ENG.interventions(sc)["ranking"]}


def test_f2_reversal_that_disconnects_little_mount_is_flagged_harmful():
    sc = Scenario("B", ["maraimalai_adigal_bridge:northbound"])
    c = _cands(sc)["reverse:9189140910_6496965128_0"]
    assert c["zones_lost"] == ["little_mount"] and c["effect"] == "loses_access"
    assert "HARMFUL" in c["why"] and c["share_of_disruption"] is None
    applied = ENG.evaluate(sc.with_changes(reverse=["9189140910_6496965128_0"]))["metrics"]
    assert "little_mount" in applied["cut_off"]          # the evaluation itself also reports the loss


def test_f2_reversal_that_lengthens_little_mount_is_longer_not_no_change():
    c = _cands(Scenario("B", ["maraimalai_adigal_bridge:northbound"]))["reverse:6496965128_269766793_0"]
    assert c["effect"] == "longer" and c["zones_worsened"][0]["zone"] == "little_mount"
    assert c["zones_worsened"][0]["added_m"] == pytest.approx(156.9, abs=0.2)


@pytest.mark.parametrize("closed", [["maraimalai_adigal_bridge:northbound"], ALL,
                                    ["kotturpuram_bridge:northbound"], ["maraimalai_adigal_bridge:southbound"]])
def test_f2_no_candidate_hides_loss_or_harm_and_losses_rank_last(closed):
    ranking = ENG.interventions(Scenario("B", closed))["ranking"]
    for c in ranking:
        if c["effect"] == "no_change":
            assert not (c["zones_lost"] or c["zones_worsened"] or c["zones_improved"] or c["zones_restored"])
        if c["zones_lost"]:
            assert c["effect"] == "loses_access"
    assert ranking == sorted(ranking, key=rank_key)
    seen_loss = False
    for c in ranking:
        seen_loss |= bool(c["zones_lost"])
        assert not (seen_loss and not c["zones_lost"]), "a harmless candidate ranked below a harmful one"


def test_f2_ui_categories_cover_every_effect():
    effects = {c["effect"] for c in ENG.interventions(Scenario("B", ["maraimalai_adigal_bridge:northbound"]))["ranking"]}
    assert effects <= {"loses_access", "restores_access", "shorter", "longer", "mixed", "no_change"}


# ------------------------------------------------------------------ F3
SB_REV = "11052199789_269767231_0"           # Maraimalai Adigal southbound carriageway segment


@pytest.mark.parametrize("kind", ["reserve_emergency", "keep_direction"])
def test_f3_evaluate_rejects_reversing_constrained_carriageway(kind):
    body = {"rule": "B", "closed_carriageways": ["maraimalai_adigal_bridge:northbound"], "reversed_segments": [SB_REV],
            "constraints": [{"kind": kind, "target": "maraimalai_adigal_bridge:southbound"}]}
    r = client.post("/api/evaluate", json=body)
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["code"] == "constraint_violation" and d["kind"] == kind and d["segment"] == SB_REV


@pytest.mark.parametrize("kind", ["reserve_emergency", "keep_direction"])
def test_f3_reverse_forbidden_on_every_constrained_oneway_carriageway(kind):
    for x in NET.crossings:
        for cw in x["carriageways"]:
            if not cw["oneway"]:
                continue
            sc = Scenario("B", [], [], [cw["eids"][0]], [{"kind": kind, "target": cw["id"]}])
            with pytest.raises(ValidationError) as e:
                ENG.evaluate(sc)
            assert e.value.code == "constraint_violation"


def test_f3_no_reopen_segment_bypass_is_closed():
    body = {"scenario": {"rule": "B", "closed_segments": ["308198209_256035491_0"],
                         "constraints": [{"kind": "no_reopen", "target": "maraimalai_adigal_bridge:northbound"}]}}
    r = client.post("/api/interventions", json=body)
    assert r.status_code == 200
    j = r.json()
    # The segment is the whole northbound carriageway, so the canonical form is a carriageway closure (R5);
    # no form of reopening it may be offered.
    seg = "308198209_256035491_0"
    assert not [c for c in j["ranking"] if seg in c["segments"] and c["type"] != "contraflow"]
    ex = [c for c in j["excluded"] if seg in c["segments"]]
    assert ex and all(c["excluded_by"]["kind"] == "no_reopen" for c in ex)


def test_f3_no_reopen_rejects_scenarios_that_reopen_by_any_path():
    cons = [{"kind": "no_reopen", "target": "kotturpuram_bridge:northbound"}]
    eid = NET.carriageway("kotturpuram_bridge:northbound")["eids"][0]
    assert ENG.evaluate(Scenario("B", ["kotturpuram_bridge:northbound"], constraints=cons))   # closed: valid
    assert ENG.evaluate(Scenario("B", [], [eid], constraints=cons))                           # closed via segment: valid
    for bad in (Scenario("B", [], constraints=cons), Scenario("B", [], [], [], cons)):
        with pytest.raises(ValidationError) as e:
            ENG.evaluate(bad)
        assert e.value.detail["action"] == "reopen"


def test_f3_reserved_carriageway_never_offered_as_reopening():
    cw = "maraimalai_adigal_bridge:northbound"
    for sc in (Scenario("B", ALL, constraints=[{"kind": "reserve_emergency", "target": cw}]),
               Scenario("B", [], NET.carriageway(cw)["eids"], constraints=[{"kind": "reserve_emergency", "target": cw}])):
        iv = ENG.interventions(sc)
        assert all(cw not in c["carriageways"] and not set(c["segments"]) & set(NET.carriageway(cw)["eids"])
                   or c["type"] == "contraflow" for c in iv["ranking"])


def test_f3_closed_and_reversed_conflict_rejected():
    e = next(s["eid"] for s in NET.segments if s["oneway"] and not s["carriageway"])
    with pytest.raises(ValidationError) as err:
        ENG.evaluate(Scenario("B", [], [e], [e]))
    assert err.value.code == "closed_and_reversed"
