"""Codex final review blockers: investigation scope leakage and the repeated clarification after confirmation.
Every number is re-derived with an explicit engine evaluation of the intended scenario."""
import os

import pytest

from contraflow.agent.intent import parse_intent
from contraflow.agent.runtime import run_agent
from contraflow.agent.tools import ToolBox
from contraflow.api import state
from contraflow.engine import Scenario

ST = state()
ENG = ST["engine"]
LLM = os.environ.get("CONTRAFLOW_TEST_LLM") == "1"
KOT = ["kotturpuram_bridge:northbound", "kotturpuram_bridge:southbound"]
MAB = ["maraimalai_adigal_bridge:northbound", "maraimalai_adigal_bridge:southbound"]
MAB_SB = "maraimalai_adigal_bridge:southbound"
EXACT = ("A volunteer says Kotturpuram bridge may be blocked by fallen trees. Who would need a detour, keeping "
         "Maraimalai Adigal southbound reserved for ambulances?")
REVERSED = ("Keeping Maraimalai Adigal southbound reserved for ambulances, who would need a detour if Kotturpuram "
            "bridge may be blocked?")


def res(r, tool):
    return next((s["result"] for s in r["steps"] if s.get("tool") == tool and s.get("kind") == "tool"), None)


def total(sc):
    return ENG.evaluate(sc)["metrics"]["total_delta_m"]


def check_exact_scope(r):
    inv, asm, rank = res(r, "investigate_disruption"), res(r, "assume_constraint"), res(r, "rank_recovery")
    assert sorted(inv["closes"]["closed_carriageways"]) == KOT                     # whole bridge: no direction given
    assert inv["added_km"] == pytest.approx(total(Scenario("B", KOT)) / 1000, abs=0.01)   # 1.63 km
    assert asm["scope"]["direction"] == "southbound" and asm["assumed"].endswith("(southbound)")
    hyp = Scenario("B", KOT, constraints=[{"kind": "reserve_emergency", "target": MAB_SB}])
    assert rank["scenario_key"] == ENG.normalize(hyp).key()                          # ranking uses the declared scopes
    assert rank["current_added_km"] == pytest.approx(total(hyp) / 1000, abs=0.01)    # 2.08 km, not 11.92
    assert rank["ranking"][0]["action"] == ENG.interventions(hyp)["ranking"][0]["label"] == "Reopen Kotturpuram Bridge (northbound)"
    assert r["proposals"] == []
    assert ENG.normalize(Scenario.from_dict(r["working_scenario"])).key() == ENG.normalize(Scenario("B")).key()


# ------------------------------------------------------------------ intent: one typed scope per clause
@pytest.mark.parametrize("msg,primary_dir,aux_dir", [
    (EXACT, None, "southbound"), (REVERSED, None, "southbound"),
    ("While Maraimalai Adigal northbound stays reserved for ambulances, what if Kotturpuram southbound were closed?",
     "southbound", "northbound"),
    ("Kotturpuram northbound may be flooded, keeping Maraimalai Adigal reserved for emergency vehicles.", "northbound", None),
    ("Kotturpuram southbound may be blocked, keeping Kotturpuram northbound reserved for ambulances.", "southbound", "northbound"),
])
def test_directions_never_leak_between_clauses(msg, primary_dir, aux_dir):
    i = parse_intent(msg)
    assert i.action == "investigate" and not i.may_mutate
    assert (i.direction, i.aux_constraint, i.aux_direction) == (primary_dir, "reserve_emergency", aux_dir), i.to_dict()
    assert "reserved" not in i.primary_text.lower() and "ambulance" not in i.primary_text.lower()


# ------------------------------------------------------------------ the exact Codex reproduction (and reversed order)
@pytest.mark.parametrize("msg", [EXACT, REVERSED])
def test_exact_reproduction_uses_the_declared_scopes(msg):
    check_exact_scope(run_agent(ST, msg, Scenario("B"), [], "deterministic", None))


def test_suggested_follow_ups_carry_the_same_scopes():
    r = run_agent(ST, EXACT, Scenario("B"), [], "deterministic", None)
    parsed = [parse_intent(a["message"]) for a in r["suggested_actions"]]
    assert [(p.operation, p.direction) for p in parsed] == [("close", None), ("reserve_emergency", "southbound")]
    # and executing the reservation follow-up proposes exactly the southbound carriageway
    p = run_agent(ST, r["suggested_actions"][1]["message"], Scenario("B"), [], "deterministic", None)["proposals"]
    assert [(k["kind"], k["target"]) for k in p[0]["scenario"]["constraints"]] == [("reserve_emergency", MAB_SB)]


def test_multiple_locations_each_keep_their_own_direction():
    r = run_agent(ST, "What if Kotturpuram southbound and Abraham bridge were closed?", Scenario("B"), [], "deterministic", None)
    inv = res(r, "investigate_disruption")
    assert sorted(inv["closes"]["closed_carriageways"]) == ["abraham_bridge:two-way", "kotturpuram_bridge:southbound"]
    assert {(s["crossing_id"], s["direction"]) for s in inv["scopes"]} == {("kotturpuram_bridge", "southbound"), ("abraham_bridge", None)}
    r = run_agent(ST, "What if Kotturpuram and Abraham bridges were closed?", Scenario("B"), [], "deterministic", None)
    inv = res(r, "investigate_disruption")
    assert sorted(inv["closes"]["closed_carriageways"]) == sorted(KOT + ["abraham_bridge:two-way"])
    assert inv["added_km"] == pytest.approx((total(Scenario("B", KOT + ["abraham_bridge:two-way"]))) / 1000, abs=0.01)


def test_conflicting_scopes_are_refused_not_silently_assumed():
    r = run_agent(ST, "Kotturpuram bridge may be blocked, keeping Kotturpuram reserved for ambulances.",
                  Scenario("B"), [], "deterministic", None)
    asm = res(r, "assume_constraint")
    assert asm["code"] == "conflicting_scope" and "Constraint not assumed" in r["message"]
    assert res(r, "rank_recovery")["scenario_key"] == ENG.normalize(Scenario("B", KOT)).key()   # no reservation applied
    assert r["proposals"] == []


# ------------------------------------------------------------------ tool layer: the model cannot widen or move a scope
def test_tool_layer_ignores_model_supplied_locations_and_directions():
    tb = ToolBox(ST, Scenario("B"), user_msg=EXACT)
    inv = tb.call("investigate_disruption", {"location": "Kotturpuram bridge southbound"})
    assert sorted(inv["closes"]["closed_carriageways"]) == KOT                       # the user's clause, not the model's
    asm = tb.call("assume_constraint", {"kind": "reserve_emergency", "location": "Maraimalai Adigal"})
    assert asm["scope"]["direction"] == "southbound" and tb.hyp_delta["constraints"] == [{"kind": "reserve_emergency", "target": MAB_SB}]
    bad = tb.call("assume_constraint", {"kind": "no_reopen", "location": "Maraimalai Adigal"})
    assert bad["code"] == "intent_mismatch"
    assert tb.proposals == []


def test_stubbed_model_wrong_scope_cannot_change_the_analysis(monkeypatch):
    import contraflow.agent.runtime as rt
    monkeypatch.setattr(rt.llm, "llm_status", lambda: {"llm_available": True, "model": "stub", "base_url": "", "reason": None})
    calls = iter([{"content": "", "tool_calls": [
        {"id": "1", "function": {"name": "investigate_disruption", "arguments": '{"location": "Kotturpuram southbound"}'}},
        {"id": "2", "function": {"name": "assume_constraint", "arguments": '{"kind": "reserve_emergency", "location": "Maraimalai Adigal both directions"}'}}]}]
        + [{"content": "done", "tool_calls": []}] * 10)
    monkeypatch.setattr(rt.llm, "chat", lambda *a, **k: next(calls))
    check_exact_scope(run_agent(ST, EXACT, Scenario("B"), [], "llm", None))


# ------------------------------------------------------------------ confirmation is used, not re-asked
def test_confirmed_location_is_investigated_instead_of_asking_again():
    q = "Guindy bridge might be under water."
    r = run_agent(ST, q, Scenario("B"), [], "deterministic", None)
    c = r["clarification"]
    opt = next(o for o in c["options"] if "Maraimalai" in o["label"])
    r2 = run_agent(ST, q, Scenario("B"), [], "deterministic", {**opt["value"], "pending": c["pending"]})
    assert r2["clarification"] is None and r2["proposals"] == []
    inv = res(r2, "investigate_disruption")
    assert sorted(inv["closes"]["closed_carriageways"]) == MAB
    assert inv["added_km"] == pytest.approx(total(Scenario("B", MAB)) / 1000, abs=0.01)
    # a stale confirmation (scenario changed) is still refused; a choice outside the offer is still invalid
    r3 = run_agent(ST, q, Scenario("B", KOT), [], "deterministic", {**opt["value"], "pending": c["pending"]})
    assert r3.get("stale") and res(r3, "investigate_disruption") is None
    from contraflow.engine import ValidationError
    with pytest.raises(ValidationError):
        run_agent(ST, q, Scenario("B"), [], "deterministic", {"crossing_id": "thiru_vi_ka_bridge", "pending": c["pending"]})


def test_earlier_protections_still_hold():
    for msg in ["Kotturpuram bridge is not blocked.", "Do not close Saidapet bridge.",
                "Maraimalai Adigal must not be reserved for emergency vehicles."]:
        assert run_agent(ST, msg, Scenario("B"), [], "deterministic", None)["proposals"] == []
    r = run_agent(ST, "Saidapet bridge is blocked.", Scenario("B"), [], "deterministic", None)
    assert sorted(r["proposals"][0]["scenario"]["closed_carriageways"]) == MAB


# ------------------------------------------------------------------ live local model (opt-in)
@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
@pytest.mark.parametrize("msg", [EXACT, REVERSED])
def test_live_exact_reproduction(msg):
    r = run_agent(ST, msg, Scenario("B"), [], "llm", None)
    assert r["mode"]["kind"] == "llm"
    check_exact_scope(r)


@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
def test_live_guindy_confirmation():
    q = "Guindy bridge might be under water."
    r = run_agent(ST, q, Scenario("B"), [], "llm", None)
    c = r["clarification"]
    assert c is not None
    opt = next(o for o in c["options"] if "Maraimalai" in o["label"])
    r2 = run_agent(ST, q, Scenario("B"), [], "llm", {**opt["value"], "pending": c["pending"]})
    assert r2["clarification"] is None and sorted(res(r2, "investigate_disruption")["closes"]["closed_carriageways"]) == MAB
