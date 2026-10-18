"""Recommendation robustness ("Test assumptions"): every variation is derived from the scenario and re-ranked on graph
copies. Each variation's result is checked against an independent `interventions` run on the same scenario, so nothing
can come from a fixed table."""
import pytest
from fastapi.testclient import TestClient

from contraflow.agent.runtime import run_agent
from contraflow.api import app, state
from contraflow.engine import Scenario, _best
from contraflow.graphdb import SCENARIO_PREFIX

from test_case_file_mcp import MCP

ST = state()
ENG = ST["engine"]
client = TestClient(app)
KOT = ["kotturpuram_bridge:northbound", "kotturpuram_bridge:southbound"]
MAR = ["maraimalai_adigal_bridge:northbound", "maraimalai_adigal_bridge:southbound"]
ALL = [c["id"] for x in ST["net"].crossings for c in x["carriageways"]]


def by_kind(r, kind):
    return [v for v in r["variations"] if v["kind"] == kind]


def independent_best(sc_dict):
    b = _best(ENG.interventions(Scenario.from_dict(sc_dict))["ranking"])
    return b and b["id"]


@pytest.fixture(scope="module")
def kot():
    return ENG.robustness(Scenario("B", KOT))


@pytest.fixture(scope="module")
def all4():
    return ENG.robustness(Scenario("B", ALL))


def test_each_variation_changes_one_assumption_and_matches_an_independent_ranking(kot, all4):
    for r in (kot, all4):
        assert 1 <= len(r["variations"]) <= 5
        base = r["scenario"]
        for v in r["variations"]:
            s = v["scenario"]
            changed = [k for k in ("rule", "closed_carriageways", "closed_segments", "reversed_segments", "constraints")
                       if s[k] != base[k]]
            # option_unavailable for a contraflow winner reuses the same scenario; every other variation changes one field
            assert len(changed) == 1 or (v["kind"] == "option_unavailable" and not changed), (v["id"], changed)
            assert (v["best"] or {}).get("id") == independent_best(s), v["id"]
            assert v["scenario_key"] == Scenario.from_dict(s).key()


def test_kotturpuram_variations_are_interpretable(kot):
    assert kot["original"]["label"] == "Reopen Kotturpuram Bridge (northbound)"
    rule = by_kind(kot, "assistance_rule")[0]
    assert rule["scenario"]["rule"] == "A" and rule["status"] == "no repair needed"
    assert rule["current"]["total_delta_m"] == ENG.evaluate(Scenario("A", KOT))["metrics"]["total_delta_m"] == 0
    dirs = {v["scenario"]["closed_carriageways"][0]: v for v in by_kind(kot, "closure_direction")}
    assert set(dirs) == set(KOT)
    assert dirs[KOT[0]]["status"] == "remains best" and dirs[KOT[1]]["status"] == "no repair needed"
    un = by_kind(kot, "option_unavailable")[0]
    assert {"kind": "no_reopen", "target": KOT[0]} in un["scenario"]["constraints"]
    assert {"reopen:" + KOT[0], "reopen_bridge:kotturpuram_bridge"} <= set(un["excluded"])
    assert un["status"] == "original excluded" and un["best"] is None
    assert kot["summary"] == f"the original recommendation stays best in 1 of {len(kot['variations'])} tested variation(s)"


def test_reserved_carriageway_is_the_busiest_open_one_by_graph_routes(kot):
    v = by_kind(kot, "commander_constraint")[0]
    usage = ENG.carriageway_usage(ENG.raw_access(Scenario("B", KOT), __import__("contraflow.graphdb").graphdb.Ledger()))
    open_ = {c: z for c, z in usage.items() if c not in KOT}
    busiest = sorted(open_, key=lambda c: (-len(open_[c]), c))[0]
    assert v["scenario"]["constraints"] == [{"kind": "reserve_emergency", "target": busiest}]
    assert v["status"].startswith("changed") and v["zones_deciding"]
    assert v["best"]["recovered_km"] > kot["original"]["recovered_km"]


def test_all_crossings_closed(all4):
    assert all4["original"]["label"] == "Reopen all of Maraimalai Adigal Bridge"
    st = {v["kind"] + ":" + v["id"]: v["status"] for v in all4["variations"]}
    assert st["assistance_rule:rule:A"] == "remains best"
    assert st["closure_direction:direction:" + MAR[0]] == "same crossing, narrower action"
    assert st["closure_direction:direction:" + MAR[1]] == "original not applicable"
    assert st["option_unavailable:unavailable:reopen_bridge:maraimalai_adigal_bridge"] == "original excluded"
    assert not by_kind(all4, "commander_constraint")       # nothing is open to reserve: no variation invented


def test_existing_constraints_are_kept_and_enforced():
    sc = Scenario("B", KOT, constraints=[{"kind": "reserve_emergency", "target": MAR[1]}])
    r = ENG.robustness(sc)
    reserved = set(ST["net"].carriageway(MAR[1])["eids"])
    for v in r["variations"]:
        assert {"kind": "reserve_emergency", "target": MAR[1]} in v["scenario"]["constraints"]
        if v.get("best"):
            item = next(x for x in ENG.interventions(Scenario.from_dict(v["scenario"]))["ranking"] if x["id"] == v["best"]["id"])
            assert not set(item["segments"]) & reserved


def test_a_variation_that_breaks_a_constraint_is_rejected_not_rewritten():
    # no_reopen on Kotturpuram southbound: the "only northbound closed" variation would reopen it -> must be rejected
    sc = Scenario("B", KOT, constraints=[{"kind": "no_reopen", "target": KOT[1]}])
    r = ENG.robustness(sc)
    v = next(v for v in r["variations"] if v["id"] == "direction:" + KOT[0])
    assert v["rejected"]["code"] == "constraint_violation" and "best" not in v
    assert {"kind": "no_reopen", "target": KOT[1]} in v["scenario"]["constraints"]
    assert "of 4 tested" in r["summary"]                     # the rejected one is not counted as tested


def test_no_recommendation_and_read_only():
    before = ST["memory"].stats()
    r = ENG.robustness(Scenario("B"))
    assert r["original"] is None and r["summary"] == "no repair improves access in the scenario itself"
    assert ST["memory"].stats() == before
    assert not [g for g in ST["db"].list_graphs() if g.startswith(SCENARIO_PREFIX)]


# ------------------------------------------------------------------ case-file integration ("Test assumptions")
def test_case_file_test_assumptions_endpoint():
    r = run_agent(ST, "What if Kotturpuram bridge were closed?", Scenario("B"), [], "deterministic", None)
    iid = r["investigation_id"]
    case = client.post(f"/api/case/{iid}", json={"scenario": {"rule": "B"}}).json()
    out = client.post(f"/api/case/{iid}/robustness", json={"scenario": {"rule": "B"}}).json()
    assert out["available"] and out["original"]["label"] == case["recommendation"] and out["stored_matches_fresh"]
    assert out["scenario_key"] == Scenario("B", KOT).key() == ENG.normalize(Scenario("B", KOT)).key()
    assert out["evaluated_against_key"] == Scenario("B").key()
    assert out["graph_operations"]["by_op"]["graph_copy"] >= len(out["variations"])
    assert "ledger" not in out


def test_case_file_test_assumptions_goes_stale_with_the_scenario():
    r = run_agent(ST, "What if Kotturpuram bridge were closed?", Scenario("B"), [], "deterministic", None)
    iid = r["investigation_id"]
    # on screen: Maraimalai Adigal northbound is already closed -> the hypothesis is tested on THAT scenario, freshly
    now = {"rule": "B", "closed_carriageways": [MAR[0]]}
    out = client.post(f"/api/case/{iid}/robustness", json={"scenario": now}).json()
    assert set(out["scenario"]["closed_carriageways"]) == set(KOT) | {MAR[0]}
    assert out["evaluated_against_key"] == Scenario.from_dict(now).key()
    fresh = _best(ENG.interventions(Scenario("B", KOT + [MAR[0]]))["ranking"])
    assert out["original"]["id"] == fresh["id"]
    assert out["stored_matches_fresh"] == (fresh["label"] == "Reopen Kotturpuram Bridge (northbound)")


def test_case_file_test_assumptions_errors():
    assert client.post("/api/case/inv_000000000000/robustness", json={"scenario": {"rule": "B"}}).status_code == 404
    assert client.post("/api/case/bad/robustness", json={"scenario": {"rule": "B"}}).status_code == 422
    assert client.post("/api/case/inv_000000000000/robustness", json={"scenario": {"rule": "Z"}}).status_code == 422


def test_mcp_test_recommendation_tool():
    m = MCP()
    try:
        m.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}})
        res = m.tool("test_recommendation", {"closed_crossings": ["Kotturpuram"]})
        assert not res["isError"]
        d = res["structuredContent"]
        assert d["original"]["label"] == "Reopen Kotturpuram Bridge (northbound)"
        assert [v["status"] for v in d["variations"]] == [v["status"] for v in ENG.robustness(Scenario("B", KOT))["variations"]]
        assert "not a probability" in d["method"]
    finally:
        m.close()
