"""Competitive upgrade: model-driven investigation workflow and graph-backed investigation memory.
Every number asserted here is recomputed independently (engine evaluation on the explicit scenario, or NetworkX)."""
import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from contraflow.agent.intent import parse_intent
from contraflow.agent.runtime import run_agent
from contraflow.agent.tools import ToolBox, specs_for
from contraflow.api import app, state
from contraflow.engine import Scenario
from contraflow.memory import Memory

ST = state()
NET, ENG, DB, MEM = ST["net"], ST["engine"], ST["db"], ST["memory"]
MAB = ["maraimalai_adigal_bridge:northbound", "maraimalai_adigal_bridge:southbound"]
KOT = ["kotturpuram_bridge:northbound", "kotturpuram_bridge:southbound"]
ALL = [c["id"] for x in NET.crossings for c in x["carriageways"]]
SAIDAPET_Q = "Saidapet bridge may be flooded. Which neighbourhoods are affected, keeping Kotturpuram reserved for ambulances?"
client = TestClient(app)


def tools_of(r):
    return [s["tool"] for s in r["steps"] if s.get("kind") == "tool"]


def result_of(r, tool):
    return next(s["result"] for s in r["steps"] if s.get("tool") == tool and s.get("kind") == "tool")


def total(sc):
    return ENG.evaluate(sc)["metrics"]["total_delta_m"]


# ------------------------------------------------------------------ intent
@pytest.mark.parametrize("msg,action", [
    ("Saidapet bridge may be flooded.", "investigate"), ("What if Abraham bridge were closed?", "investigate"),
    ("Reportedly Kotturpuram southbound is blocked", "investigate"), ("Investigate a closure of Thiru Vi Ka", "investigate"),
    ("Does our earlier finding about Saidapet still hold?", "recall"),
    ("Which roads matter most for hospital access?", "vulnerability"), ("Where are the weak points?", "vulnerability"),
    ("Close Saidapet bridge", "close"), ("Saidapet bridge is blocked.", "close"),
])
def test_intent_routes_investigation_recall_and_vulnerability(msg, action):
    i = parse_intent(msg)
    assert i.action == action, i.to_dict()
    if action in ("investigate", "recall", "vulnerability"):
        assert not i.may_mutate


def test_investigation_splits_disruption_from_constraint_clause():
    i = parse_intent(SAIDAPET_Q)
    assert i.aux_constraint == "reserve_emergency" and "Kotturpuram" in i.aux_text and "Kotturpuram" not in i.primary_text


def test_toolsets_are_restricted_per_intent():
    names = lambda m: {s["name"] for s in specs_for(parse_intent(m))}  # noqa: E731
    inv = names("Saidapet bridge may be flooded")
    assert "investigate_disruption" in inv and not inv & {"propose_closure", "propose_reopen", "set_planning_constraint"}
    assert "find_critical_roads" in names("Which roads matter most?")


# ------------------------------------------------------------------ investigation (deterministic planner)
def test_saidapet_investigation_is_read_only_and_numerically_exact():
    sc = Scenario("B")
    r = run_agent(ST, SAIDAPET_Q, sc, [], "deterministic", None)
    assert r["proposals"] == [] and r["clarification"] is None
    assert Scenario.from_dict(r["working_scenario"]).key() == ENG.normalize(sc).key()      # nothing applied
    assert tools_of(r)[:4] == ["investigate_disruption", "assume_constraint", "rank_recovery", "compare_options"]
    inv = result_of(r, "investigate_disruption")
    # independent check: the hypothesis impact equals an explicit evaluation of the closed bridge
    assert inv["added_km"] == pytest.approx(total(Scenario("B", MAB)) / 1000, abs=0.01)
    assert inv["added_km"] == pytest.approx(11.92, abs=0.01)
    guindy = next(a for a in inv["affected"] if a["id"] == "guindy")
    ev = {z["zone"]: z for z in ENG.evaluate(Scenario("B", MAB))["zones"]}
    assert guindy["delta_m"] == pytest.approx(ev["guindy"]["delta"], abs=0.2)
    assert {a["id"] for a in inv["affected"]} == {z for z, row in ev.items() if row.get("delta") and abs(row["delta"]) > 0.5}
    # assumed constraint is analysis-only and the ranking honours it
    rank = result_of(r, "rank_recovery")
    hyp = Scenario("B", MAB, constraints=[{"kind": "reserve_emergency", "target": c} for c in KOT])
    assert rank["scenario_key"] == ENG.normalize(hyp).key()
    ref = ENG.interventions(hyp)["ranking"]
    assert rank["ranking"][0]["action"] == ref[0]["label"]
    assert not any("kotturpuram" in c["action"].lower() and "reopen" in c["action"].lower() for c in rank["ranking"])
    # suggested actions are explicit commands, not proposals
    assert {a["message"] for a in r["suggested_actions"]} >= {"Close Maraimalai Adigal Bridge.",
                                                              "Reserve Kotturpuram Bridge for emergency vehicles."}
    assert "Nothing has been changed" in r["message"]


def test_comparison_winner_matches_independent_application():
    r = run_agent(ST, "What if Abraham bridge and Maraimalai Adigal bridge were closed?", Scenario("B", KOT), [],
                  "deterministic", None)
    if "compare_options" not in tools_of(r):
        pytest.skip("fewer than two beneficial options for this hypothesis")
    cmp = result_of(r, "compare_options")
    valid = [o for o in cmp["options"] if "error" not in o]
    for o in valid:
        assert o["recovered_km"] >= 0
    assert cmp["winner"] == max(valid, key=lambda o: (-len(o["zones_cut_off_by_it"]), o["recovered_km"]))["action"]


def test_ambiguous_location_asks_and_records_unverified_report():
    before = MEM.stats()
    r = run_agent(ST, "Guindy bridge might be under water", Scenario("B"), [], "deterministic", None)
    assert r["proposals"] == [] and r["clarification"] is not None
    assert len(r["clarification"]["options"]) >= 2
    after = MEM.stats()
    assert after["reports"] == before["reports"] + 1 and after["findings"] == before["findings"]   # nothing computed
    # confirming one candidate continues the investigation for that crossing only
    opt = r["clarification"]["options"][0]
    r2 = run_agent(ST, "Guindy bridge might be under water", Scenario("B"), [], "deterministic",
                   {**opt["value"], "pending": r["clarification"].get("pending")})
    assert r2["proposals"] == []
    assert "investigate_disruption" in tools_of(r2)


def test_unknown_place_is_not_invented():
    r = run_agent(ST, "What if the Marina lighthouse bridge were closed?", Scenario("B"), [], "deterministic", None)
    assert r["proposals"] == []
    inv = [s["result"] for s in r["steps"] if s.get("tool") == "investigate_disruption"]
    assert not inv or not inv[0].get("added_km")


# ------------------------------------------------------------------ model-tool authorization during investigation
def test_model_cannot_propose_during_investigation():
    tb = ToolBox(ST, Scenario("B"), user_msg="Saidapet bridge may be flooded")
    r = tb.call("propose_closure", {"crossing_id": "maraimalai_adigal_bridge", "report_text": "Saidapet bridge may be flooded"})
    assert r.get("code") in ("intent_mismatch", "not_authorized", "negated_instruction") and tb.proposals == [], r
    r = tb.call("assume_constraint", {"kind": "reserve_emergency", "location": "Kotturpuram"})
    assert r["code"] == "intent_mismatch"                       # user asked for no constraint


def test_stubbed_model_tool_outside_toolset_is_refused(monkeypatch):
    import contraflow.agent.runtime as rt
    monkeypatch.setattr(rt.llm, "llm_status", lambda: {"llm_available": True, "model": "stub", "base_url": "", "reason": None})
    calls = iter([{"content": "", "tool_calls": [{"id": "1", "function": {"name": "propose_closure",
                   "arguments": '{"crossing_id": "maraimalai_adigal_bridge"}'}}]}] + [{"content": "done", "tool_calls": []}] * 10)
    monkeypatch.setattr(rt.llm, "chat", lambda *a, **k: next(calls))
    r = run_agent(ST, "Saidapet bridge may be flooded", Scenario("B"), [], "llm", None)
    assert r["proposals"] == []
    assert any(s.get("kind") == "error" and s.get("tool") == "propose_closure" for s in r["steps"])
    assert "investigate_disruption" in tools_of(r)               # runtime completed the read-only investigation


# ------------------------------------------------------------------ memory
def test_memory_provenance_and_no_fact_promotion():
    r = run_agent(ST, "Reportedly Kotturpuram southbound is blocked", Scenario("B"), [], "deterministic", None)
    iid = r["investigation_id"]
    g = DB.graph(MEM.name)
    rows = g.q("t", "MATCH (r:Report)-[:IN]->(i:Investigation {id: $i}) RETURN r.provenance", {"i": iid})
    assert rows and {x[0] for x in rows} == {"reported_unverified"}
    rows = g.q("t", "MATCH (f:Finding)-[:IN]->(i:Investigation {id: $i}) RETURN f.provenance, f.scenario_key", {"i": iid})
    assert rows and {x[0] for x in rows} == {"computed"}
    assert g.q("t", "MATCH (d:Decision)-[:ON]->(:Proposal)-[:IN]->(:Investigation {id: $i}) RETURN count(d)", {"i": iid})[0][0] == 0
    assert g.q("t", "MATCH (f:Finding)-[:ASSUMES]->(a:Assumption) WHERE (f)-[:IN]->(:Investigation {id: $i}) "
                    "RETURN collect(a.provenance)", {"i": iid})[0][0][0] == "modeled_assumption"
    # base road graph is untouched by memory writes and no clones are left behind
    assert not [x for x in DB.list_graphs() if x.startswith("cf_sc_")]


def test_decisions_only_from_explicit_api_and_validated():
    r = run_agent(ST, "Close Abraham bridge", Scenario("B"), [], "deterministic", None)
    p = r["proposals"][0]
    assert p.get("memory_id")
    assert client.post("/api/memory/decision", json={"proposal_id": p["memory_id"], "state": "maybe"}).status_code == 422
    assert client.post("/api/memory/decision", json={"proposal_id": "prop_nope", "state": "applied"}).status_code == 404
    assert client.post("/api/memory/decision", json={"proposal_id": p["memory_id"], "state": "rejected"}).json()["ok"]
    st = DB.graph(MEM.name).q("t", "MATCH (d:Decision)-[:ON]->(p:Proposal {id: $p}) RETURN d.provenance, p.state",
                              {"p": p["memory_id"]})
    assert st == [["user_rejected", "rejected"]]


def test_recall_after_scenario_change_marks_superseded_changed_and_stale():
    q = "What if Maraimalai Adigal bridge were closed?"
    run_agent(ST, q, Scenario("B"), [], "deterministic", None)
    # 1) the hypothesised closure is now applied -> superseded
    r = run_agent(ST, "Does our earlier finding about Maraimalai Adigal still hold?", Scenario("B", MAB), [],
                  "deterministic", None)
    f = result_of(r, "recall_memory")["findings"]
    imp = next(x for x in f if x["kind"] == "disruption_impact")
    assert imp["still_applies"].startswith("superseded")
    # the ranking was computed for "Maraimalai Adigal closed", which is exactly the scenario now
    rk = next(x for x in f if x["kind"] == "recovery_ranking" and x["question"] == q)
    assert rk["still_applies"].startswith("yes")
    # 2) another closure changes the impact -> re-evaluated with a different number
    r = run_agent(ST, "Does our earlier finding about Maraimalai Adigal still hold?", Scenario("B", KOT), [],
                  "deterministic", None)
    imp = next(x for x in result_of(r, "recall_memory")["findings"] if x["kind"] == "disruption_impact")
    now_ref = (total(Scenario("B", KOT + MAB)) - total(Scenario("B", KOT))) / 1000
    assert imp["now_added_km"] == pytest.approx(now_ref, abs=0.01)
    assert imp["then_added_km"] == pytest.approx(11.92, abs=0.01)
    assert imp["still_applies"] == ("yes" if abs(now_ref - 11.92) < 0.05 else "changed")
    rk = next(x for x in result_of(r, "recall_memory")["findings"] if x["kind"] == "recovery_ranking")
    assert rk["still_applies"].startswith("stale")              # computed for a scenario that no longer holds
    assert r["proposals"] == []


def test_memory_survives_graph_loss_via_log_replay():
    run_agent(ST, "What if Thiru Vi Ka bridge were closed?", Scenario("B"), [], "deterministic", None)
    before = MEM.stats()
    DB.delete(MEM.name)
    fresh = Memory(DB, MEM.log, MEM.name)
    status = fresh.ensure()
    assert status["replayed_ops"] > 0
    assert fresh.stats() == before


def test_memory_survives_process_restart():
    run_agent(ST, "What if Abraham bridge were closed?", Scenario("B"), [], "deterministic", None)
    code = ("import json; from contraflow.api import state; m = state()['memory']; "
            "print(json.dumps({'s': m.stats(), 'r': m.recall(['abraham_bridge'], 3)}, default=str))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180,
                         env={**os.environ}, cwd=os.path.dirname(os.path.dirname(__file__)))
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["s"] == MEM.stats()
    assert any(f["kind"] == "disruption_impact" and "Abraham" in f["summary"] for f in data["r"])


def test_memory_survives_base_graph_rebuild():
    """Rebuilding the road graph (force) leaves memory intact; place ids are stable OSM-derived ids."""
    run_agent(ST, "What if Maraimalai Adigal bridge were closed?", Scenario("B"), [], "deterministic", None)
    before = MEM.stats()
    DB.load_base(NET, force=True)
    assert MEM.stats() == before
    places = DB.graph(MEM.name).q("t", "MATCH (p:Place {kind: 'crossing'}) RETURN p.id")
    assert {p[0] for p in places} <= {x["id"] for x in NET.crossings}
    r = run_agent(ST, "Does our earlier finding about Maraimalai Adigal still hold?", Scenario("B"), [], "deterministic", None)
    imp = next(x for x in result_of(r, "recall_memory")["findings"] if x["kind"] == "disruption_impact")
    assert imp["still_applies"] == "yes" and imp["now_added_km"] == pytest.approx(11.92, abs=0.01)


def test_contradictory_reports_are_surfaced():
    run_agent(ST, "Abraham bridge is not blocked.", Scenario("B"), [], "deterministic", None)
    r = run_agent(ST, "Abraham bridge may be flooded", Scenario("B"), [], "deterministic", None)
    inv = result_of(r, "investigate_disruption")
    assert inv["conflicting_reports"], inv
    assert r["proposals"] == []


def test_investigating_an_already_closed_crossing_says_so():
    sc = Scenario("B", MAB)
    r = run_agent(ST, "What if Maraimalai Adigal bridge were closed?", sc, [], "deterministic", None)
    inv = result_of(r, "investigate_disruption")
    assert inv["fully_already_closed"] and inv["added_km"] == 0
    assert "already closed in the current scenario" in r["message"] and r["proposals"] == []


def test_recall_shows_identical_findings_once():
    for _ in range(2):
        run_agent(ST, "What if Kotturpuram bridge were closed?", Scenario("B"), [], "deterministic", None)
    r = run_agent(ST, "Does our earlier finding about Kotturpuram still hold?", Scenario("B"), [], "deterministic", None)
    f = result_of(r, "recall_memory")["findings"]
    sigs = [(x["kind"], x["then"]) for x in f]
    assert len(sigs) == len(set(sigs))
