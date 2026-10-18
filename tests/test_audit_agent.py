"""Regression tests for audit findings F4 (intent through confirmation / negation), F5 (tool guards),
F6 (workflow completion) and grounding honesty."""
import os

import pytest

from contraflow.agent.intent import parse_intent
from contraflow.agent.runtime import run_agent
from contraflow.agent.tools import ToolBox
from contraflow.api import state
from contraflow.engine import Scenario, ValidationError

ST = state()
NET = ST["net"]
ALL = [c["id"] for x in NET.crossings for c in x["carriageways"]]
MAB = ["maraimalai_adigal_bridge:northbound", "maraimalai_adigal_bridge:southbound"]


def ask(msg, sc, confirm=None, mode="deterministic"):
    return run_agent(ST, msg, sc, [], mode, confirm)


def confirm_flow(msg, sc, choice):
    r1 = ask(msg, sc)
    assert r1["proposals"] == [] and r1["clarification"], r1["message"]
    pend = r1["clarification"]["pending"]
    assert choice in pend["candidates"]
    return r1, ask(msg, sc, {"crossing_id": choice, "pending": pend})


# ------------------------------------------------------------------ F4 intent preserved through clarification
def test_f4_ambiguous_reopening_stays_a_reopening():
    _, r = confirm_flow("Guindy bridge has reopened.", Scenario("B", ALL), "maraimalai_adigal_bridge")
    assert [p["kind"] for p in r["proposals"]] == ["reopen"]
    assert not set(MAB) & set(r["proposals"][0]["scenario"]["closed_carriageways"])


def test_f4_ambiguous_reservation_stays_a_reservation():
    _, r = confirm_flow("Keep Guindy bridge available for inbound emergency access.", Scenario("B", ALL),
                        "maraimalai_adigal_bridge")
    assert [p["kind"] for p in r["proposals"]] == ["constraint"]
    sc = r["proposals"][0]["scenario"]
    assert {(c["kind"], c["target"]) for c in sc["constraints"]} == {("reserve_emergency", c) for c in MAB}
    assert sorted(sc["closed_carriageways"]) == sorted(ALL)          # no closure was added or removed


def test_f4_ambiguous_closure_with_direction_keeps_direction():
    _, r = confirm_flow("Guindy bridge northbound is under water.", Scenario("B"), "maraimalai_adigal_bridge")
    assert r["proposals"][0]["scenario"]["closed_carriageways"] == ["maraimalai_adigal_bridge:northbound"]


@pytest.mark.parametrize("msg", ["Saidapet bridge is not blocked.", "Saidapet bridge isn't blocked anymore",
                                 "The Adyar bridge is no longer flooded", "Kotturpuram bridge has not reopened yet"])
def test_f4_negated_reports_never_propose_changes(msg):
    for mode in ("deterministic", "auto"):
        r = ask(msg, Scenario("B", ALL if "reopened" in msg else []), mode=mode)
        assert r["proposals"] == [], (mode, r["message"])
        assert "not" in r["message"].lower() or "NOT" in r["message"]


def test_f4_confirmation_rejected_if_scenario_changed():
    r1 = ask("Guindy bridge has reopened.", Scenario("B", ALL))
    pend = r1["clarification"]["pending"]
    changed = Scenario("A", ALL)                                # user switched the hospital rule meanwhile
    r2 = ask("Guindy bridge has reopened.", changed, {"crossing_id": "maraimalai_adigal_bridge", "pending": pend})
    assert r2["proposals"] == [] and r2.get("stale")


def test_f4_confirmation_must_be_an_offered_choice():
    r1 = ask("Guindy bridge has reopened.", Scenario("B", ALL))
    with pytest.raises(ValidationError):
        ask("Guindy bridge has reopened.", Scenario("B", ALL),
            {"crossing_id": "kotturpuram_bridge", "pending": r1["clarification"]["pending"]})
    with pytest.raises(ValidationError):
        ask("Guindy bridge has reopened.", Scenario("B", ALL), {"crossing_id": "maraimalai_adigal_bridge"})


@pytest.mark.parametrize("msg,action", [("Saidapet bridge is blocked.", "close"), ("Guindy bridge has reopened.", "reopen"),
                                        ("Keep Guindy bridge available for inbound emergency access.", "constraint"),
                                        ("Saidapet bridge is not blocked.", "negated"), ("What should we reopen first?", "rank"),
                                        ("Do not reverse the Saidapet bridge", "constraint"),
                                        ("Kotturpuram bridge is unsafe, do not reopen it", "constraint")])
def test_f4_intent_parsing(msg, action):
    assert parse_intent(msg).action == action


# ------------------------------------------------------------------ F5 tool guards
def test_f5_reopen_requires_user_named_target():
    tb = ToolBox(ST, Scenario("B", ALL), user_msg="Guindy bridge has reopened")
    r = tb.call("propose_reopen", {"crossing_id": "maraimalai_adigal_bridge", "direction": "northbound"})
    assert r.get("code") == "ambiguous_location" and tb.proposals == []


def test_f5_model_search_does_not_authorize_other_targets():
    tb = ToolBox(ST, Scenario("B"), user_msg="Guindy bridge is under water")
    tb.call("search_location", {"text": "Kotturpuram bridge"})
    r = tb.call("propose_closure", {"report_text": "x", "crossing_id": "kotturpuram_bridge"})
    assert r.get("code") == "unauthorized_target" and tb.proposals == []


def test_f5_action_must_match_user_intent():
    tb = ToolBox(ST, Scenario("B", ALL), user_msg="Maraimalai Adigal bridge has reopened")
    r = tb.call("propose_closure", {"report_text": "x", "crossing_id": "maraimalai_adigal_bridge"})
    assert r.get("code") == "intent_mismatch"
    tb = ToolBox(ST, Scenario("B"), user_msg="Saidapet bridge is blocked")
    assert tb.call("set_planning_constraint", {"kind": "reserve_emergency", "crossing_id": "maraimalai_adigal_bridge"}
                   ).get("code") == "intent_mismatch"
    tb = ToolBox(ST, Scenario("B"), user_msg="Keep Saidapet bridge for inbound emergency access")
    assert tb.call("set_planning_constraint", {"kind": "no_reopen", "crossing_id": "maraimalai_adigal_bridge"}
                   ).get("code") == "intent_mismatch"
    assert tb.proposals == []


def test_f5_unstated_direction_not_applied():
    tb = ToolBox(ST, Scenario("B", ALL), user_msg="Maraimalai Adigal bridge has reopened")
    r = tb.call("propose_reopen", {"crossing_id": "maraimalai_adigal_bridge", "direction": "northbound"})
    assert "error" not in r
    assert not set(MAB) & set(tb.work.closed_carriageways)          # both directions, as the user said


def test_f5_agent_proposals_obey_constraints():
    cons = [{"kind": "no_reopen", "target": c} for c in MAB]
    tb = ToolBox(ST, Scenario("B", ALL, constraints=cons), user_msg="Maraimalai Adigal bridge has reopened")
    r = tb.call("propose_reopen", {"crossing_id": "maraimalai_adigal_bridge"})
    assert r.get("code") == "constraint_violation" and tb.proposals == []


def test_f5_proposals_carry_scenario_identity():
    r = ask("Saidapet bridge is blocked.", Scenario("B"))
    p = r["proposals"][0]
    assert p["base_key"] == Scenario("B").key() == r["base_key"] and p["state"] == "proposed"
    assert p["scenario_key"] == Scenario.from_dict(p["scenario"]).key()


# ------------------------------------------------------------------ grounding honesty
def test_grounding_is_labelled_numeric_only():
    from contraflow.agent.runtime import grounding
    tb = ToolBox(ST, Scenario("B"))
    g = grounding("anything 5", tb, "")
    assert "semantic" in g["method"]


# ------------------------------------------------------------------ F6 live workflow (needs Ollama)
LLM = os.environ.get("CONTRAFLOW_TEST_LLM") == "1"


@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
@pytest.mark.parametrize("msg,sc,kind", [
    ("Keep Maraimalai Adigal available for inbound emergency access.", "all", "constraint"),
    ("Saidapet bridge is blocked.", "none", "close"),
    ("Kotturpuram bridge has reopened.", "all", "reopen"),
])
def test_f6_live_llm_completes_workflow(msg, sc, kind):
    scen = Scenario("B", ALL if sc == "all" else [])
    r = run_agent(ST, msg, scen, [], "llm", None)
    assert r["mode"]["kind"] == "llm"
    assert [p["kind"] for p in r["proposals"]][:1] == [kind]
    last_prop = max(i for i, s in enumerate(r["steps"]) if s.get("tool", "").startswith(("propose", "assess", "set_"))
                    and s.get("kind") == "tool")
    assert any(s.get("tool") == "rank_interventions" for s in r["steps"][last_prop + 1:])
    assert "not applied" in r["message"].lower()
    if r.get("model_note"):
        assert "claims_applied" in r["model_note"]


@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
def test_f6_live_llm_ambiguity_then_confirmation_preserves_reopen():
    r1 = run_agent(ST, "Guindy bridge has reopened.", Scenario("B", ALL), [], "llm", None)
    assert r1["proposals"] == [] and r1["clarification"]
    r2 = run_agent(ST, "Guindy bridge has reopened.", Scenario("B", ALL), [], "llm",
                   {"crossing_id": "abraham_bridge", "pending": r1["clarification"]["pending"]})
    assert [p["kind"] for p in r2["proposals"]] == ["reopen"]
