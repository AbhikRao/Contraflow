"""Agent tests (deterministic planner + tool layer). LLM-mode smoke test runs only when
CONTRAFLOW_TEST_LLM=1 and a local model is served."""

import os

import pytest

from contraflow.agent.runtime import run_agent
from contraflow.agent.tools import ToolBox
from contraflow.api import state
from contraflow.engine import Scenario
from contraflow.graphdb import BASE, SCENARIO_PREFIX

ST = state()
NET = ST["net"]
ALL = [c["id"] for x in NET.crossings for c in x["carriageways"]]
MAB = ["maraimalai_adigal_bridge:northbound", "maraimalai_adigal_bridge:southbound"]


def ask(msg, sc=None, confirm=None, history=None):
    return run_agent(ST, msg, sc or Scenario("B"), history or [], "deterministic", confirm)


def roads_fingerprint():
    return ST["db"].graph(BASE).q("t", "MATCH ()-[e:ROAD]->() RETURN count(e), sum(e.len)")[0]


def test_alias_resolves_and_records_evidence():
    before = roads_fingerprint()
    r = ask("Saidapet bridge is blocked.")
    assert r["mode"]["kind"] == "deterministic"
    assert len(r["proposals"]) == 1
    assert sorted(r["proposals"][0]["scenario"]["closed_carriageways"]) == MAB
    search = next(s for s in r["steps"] if s.get("tool") == "search_location")
    assert search["result"]["resolved"]["method"] == "zone_proximity"
    rid = r["proposals"][0]["report_id"]
    linked = ST["db"].graph(BASE).q("t", "MATCH (:Report {id: $id})-[:REFERS_TO]->(x:Crossing) RETURN x.id", {"id": rid})
    assert linked == [["maraimalai_adigal_bridge"]]
    assert roads_fingerprint() == before                       # base road graph never mutated
    assert r["grounding"]["ungrounded"] == []


def test_ambiguous_report_asks_and_does_not_mutate():
    r = ask("Guindy bridge is under water.")
    assert r["proposals"] == []
    assert r["clarification"] and {o["value"]["crossing_id"] for o in r["clarification"]["options"]} == {
        "abraham_bridge", "maraimalai_adigal_bridge"}
    r2 = ask("Guindy bridge is under water.", confirm={"crossing_id": "abraham_bridge",
                                                       "pending": r["clarification"]["pending"]})
    assert r2["proposals"][0]["scenario"]["closed_carriageways"] == ["abraham_bridge:two-way"]


def test_explicit_direction_only_closes_that_carriageway():
    r = ask("Mount Road bridge northbound closed")
    assert r["proposals"][0]["scenario"]["closed_carriageways"] == ["maraimalai_adigal_bridge:northbound"]


def test_unknown_location_is_reported_not_guessed():
    r = ask("Elphinstone bridge closed")
    assert r["proposals"] == [] and "could not match" in r["message"].lower()


def test_area_only_report_is_not_turned_into_a_closure():
    r = ask("Saidapet is flooded")
    assert r["proposals"] == []


def test_constraint_changes_eligible_interventions():
    sc = Scenario("B", ALL)
    plain = ask("What should we reopen first?", sc)
    assert "Maraimalai Adigal" in plain["message"].split("Next:")[0]
    r = ask("Keep Maraimalai Adigal available for inbound emergency access.", sc)
    cons = r["proposals"][0]["scenario"]["constraints"]
    assert {c["target"] for c in cons} == set(MAB) and {c["kind"] for c in cons} == {"reserve_emergency"}
    rank = next(s for s in r["steps"] if s.get("tool") == "rank_interventions")["result"]
    assert all("Maraimalai" not in x["action"] for x in rank["ranking"])
    assert rank["ranking"][0]["action"].startswith("Reopen Kotturpuram")
    assert rank["excluded_by_constraints"]


def test_why_zero_effect_is_explained_from_graph():
    r = ask("Why doesn't closing Thiru Vi Ka matter?")
    assert "does not make" in r["message"] and r["proposals"] == []


def test_road_section_closure_can_cut_off_a_zone():
    r = ask("Velachery Main Road closed near Guindy")
    assert r["proposals"] and r["proposals"][0]["scenario"]["closed_segments"]
    assert "CUT OFF: Guindy" in r["message"] and "restoring road access for Guindy" in r["message"]


@pytest.mark.parametrize("msg", ["Saidapet bridge is blocked.", "What should we reopen first?",
                                 "Why doesn't closing Thiru Vi Ka matter?", "Kotturpuram bridge flooded"])
def test_every_number_is_grounded(msg):
    r = ask(msg, Scenario("B", ALL) if "reopen" in msg else None)
    assert r["grounding"]["ungrounded"] == [], r["message"]


def test_tool_layer_rejects_unsafe_or_invalid_calls():
    tb = ToolBox(ST, Scenario("B"), user_msg="hello")
    assert "error" in tb.call("propose_closure", {"report_text": "x", "crossing_id": "maraimalai_adigal_bridge"})
    assert "error" in tb.call("inspect_crossing", {"crossing_id": "no_such_bridge"})
    assert "error" in tb.call("evaluate_closure", {"segment_ids": ["bogus"]})
    assert "error" in tb.call("inspect_crossing", {"wrong_arg": 1})
    with pytest.raises(Exception):
        tb.call("run_cypher", {"q": "MATCH (n) DETACH DELETE n"})
    assert tb.proposals == []


def test_direction_guard_ignores_unstated_direction():
    tb = ToolBox(ST, Scenario("B"), user_msg="Saidapet bridge is blocked")
    tb.call("search_location", {"text": "Saidapet bridge is blocked"})
    r = tb.call("propose_closure", {"report_text": "Saidapet bridge is blocked", "crossing_id": "maraimalai_adigal_bridge",
                                    "direction": "southbound"})
    assert "error" not in r
    assert sorted(tb.work.closed_carriageways) == MAB


def test_no_scenario_graphs_left_behind():
    ask("Saidapet bridge is blocked.")
    assert not [g for g in ST["db"].list_graphs() if g.startswith(SCENARIO_PREFIX)]


@pytest.mark.skipif(os.environ.get("CONTRAFLOW_TEST_LLM") != "1", reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
def test_llm_mode_smoke():
    r = run_agent(ST, "Saidapet bridge is blocked.", Scenario("B"), [], "llm", None)
    assert r["mode"]["kind"] == "llm"
    assert any(s.get("tool") in ("assess_field_report", "search_location") for s in r["steps"])
    assert r["grounding"]["ungrounded"] == []
