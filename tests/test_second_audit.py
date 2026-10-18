"""Regression tests for second-audit findings R1-R5 and provenance."""
import os

import pytest
from fastapi.testclient import TestClient

from contraflow.agent import briefing
from contraflow.agent.intent import parse_intent
from contraflow.agent.runtime import run_agent
from contraflow.agent.tools import ToolBox
from contraflow.api import app, state
from contraflow.engine import Scenario, ValidationError

ST = state()
NET, ENG = ST["net"], ST["engine"]
ALL = [c["id"] for x in NET.crossings for c in x["carriageways"]]
NB, SB = "maraimalai_adigal_bridge:northbound", "maraimalai_adigal_bridge:southbound"
LLM = os.environ.get("CONTRAFLOW_TEST_LLM") == "1"
client = TestClient(app)


def ask(msg, sc, mode="deterministic", history=None, confirm=None):
    return run_agent(ST, msg, sc, history or [], mode, confirm)


# ================================================================== R1 negation
@pytest.mark.parametrize("msg,op,pol,mood,mut", [
    ("Close the Saidapet bridge.", "close", "affirmative", "imperative", True),
    ("Do not close the Saidapet bridge.", "close", "negative", "imperative", False),
    ("The Saidapet bridge is closed.", "close", "affirmative", "descriptive", True),
    ("The Saidapet bridge is not closed.", "close", "negative", "descriptive", False),
    ("The Saidapet bridge has reopened.", "reopen", "affirmative", "descriptive", True),
    ("Do not reopen the Saidapet bridge.", "no_reopen", "affirmative", "imperative", True),
    ("Reserve the Saidapet bridge for emergency vehicles.", "reserve_emergency", "affirmative", "imperative", True),
    ("Do not reserve the Saidapet bridge for emergency vehicles.", "reserve_emergency", "negative", "imperative", False),
    ("Never close Kotturpuram bridge", "close", "negative", "imperative", False),
    ("You must not close the Adyar bridge", "close", "negative", "imperative", False),
    ("Don't keep Maraimalai Adigal for ambulances", "reserve_emergency", "negative", "imperative", False),
    ("Is Kotturpuram bridge closed?", "close", "affirmative", "question", False),
])
def test_r1_typed_intent(msg, op, pol, mood, mut):
    i = parse_intent(msg)
    assert (i.operation, i.polarity, i.mood, i.may_mutate) == (op, pol, mood, mut)


@pytest.mark.parametrize("msg,sc", [
    ("Do not close Saidapet bridge.", []), ("Do not close Saidapet bridge northbound.", []),
    ("Don't close the Adyar bridge", []), ("Never close Kotturpuram bridge", []),
    ("Do not reserve Maraimalai Adigal for emergency vehicles.", ALL),
    ("Don't keep Kotturpuram bridge for ambulances", ALL),
    ("The Saidapet bridge is not closed.", []), ("Kotturpuram bridge has not reopened yet", ALL),
])
def test_r1_negated_messages_never_propose(msg, sc):
    r = ask(msg, Scenario("B", sc))
    assert r["proposals"] == [], r["message"]
    assert "not" in r["message"].lower()


def test_r1_tool_layer_rejects_mutation_for_negative_instruction():
    tb = ToolBox(ST, Scenario("B"), user_msg="Do not close Saidapet bridge.")
    r = tb.call("propose_closure", {"report_text": "x", "crossing_id": "maraimalai_adigal_bridge"})
    assert r["code"] == "negated_instruction" and tb.proposals == []
    tb = ToolBox(ST, Scenario("B", ALL), user_msg="Do not reserve Maraimalai Adigal for emergency vehicles.")
    r = tb.call("set_planning_constraint", {"kind": "reserve_emergency", "crossing_id": "maraimalai_adigal_bridge"})
    assert r["code"] == "negated_instruction" and tb.proposals == []
    assert tb.call("assess_field_report", {"report_text": "Saidapet bridge is blocked"})["code"] == "intent_mismatch"


def test_r1_deterministic_completion_never_overrides_negation(monkeypatch):
    import contraflow.agent.runtime as rt
    monkeypatch.setattr(rt.llm, "llm_status", lambda: {"llm_available": True, "model": "stub", "base_url": "", "reason": None})
    monkeypatch.setattr(rt.llm, "chat", lambda *a, **k: {"content": "ok", "tool_calls": []})
    r = run_agent(ST, "Do not close Saidapet bridge.", Scenario("B"), [], "llm", None)
    assert r["proposals"] == [] and not any("completed by deterministic" in (s.get("text") or "") for s in r["steps"])
    # the reply is the runtime's negation template: never labelled as a tool-composed briefing
    assert r["briefing_source"] == "deterministic" and "not proposed" in r["message"]


# ================================================================== R2 exact direction
@pytest.mark.parametrize("tool,msg,sc", [
    ("propose_closure", "Close Maraimalai Adigal bridge northbound only.", []),
    ("propose_reopen", "Maraimalai Adigal bridge northbound has reopened.", ALL),
    ("set_planning_constraint", "Reserve Maraimalai Adigal northbound for inbound emergency access.", ALL),
])
@pytest.mark.parametrize("arg", [None, "all", "southbound"])
def test_r2_direction_never_expands(tool, msg, sc, arg):
    tb = ToolBox(ST, Scenario("B", sc), user_msg=msg)
    args = {"crossing_id": "maraimalai_adigal_bridge"}
    if tool == "propose_closure":
        args["report_text"] = msg
    if tool == "set_planning_constraint":
        args["kind"] = "reserve_emergency"
    if arg:
        args["direction"] = arg
    r = tb.call(tool, args)
    if arg is None:                                  # filled with the user's exact scope
        assert "error" not in r and tb.proposals[0]["scope"]["carriageways"] == [NB]
        p = Scenario.from_dict(tb.proposals[0]["scenario"])
        before = ENG.effective_closed(Scenario("B", sc))
        after = ENG.effective_closed(p)
        nb_eids = set(NET.carriageway(NB)["eids"])
        if tool == "propose_closure":
            assert after - before == nb_eids          # predicted graph mutation is exactly the northbound arcs
        elif tool == "propose_reopen":
            assert before - after == nb_eids and set(NET.carriageway(SB)["eids"]) <= after
        else:
            assert {(k["kind"], k["target"]) for k in p.constraints} == {("reserve_emergency", NB)}
    else:
        assert r["code"] == "direction_conflict" and tb.proposals == []


def test_r2_agent_level_northbound_only():
    r = ask("Close Maraimalai Adigal bridge northbound only.", Scenario("B"))
    assert [p["scenario"]["closed_carriageways"] for p in r["proposals"]] == [[NB]]


def test_r2_direction_preserved_through_confirmation():
    r1 = ask("Guindy bridge northbound is blocked.", Scenario("B"))
    pend = r1["clarification"]["pending"]
    r2 = ask("Guindy bridge northbound is blocked.", Scenario("B"),
             confirm={"crossing_id": "maraimalai_adigal_bridge", "pending": pend})
    assert r2["proposals"][0]["scenario"]["closed_carriageways"] == [NB]


# ================================================================== R3 focus vs new location
HIST = [{"role": "user", "content": "Why does Kotturpuram bridge matter?"},
        {"role": "assistant", "content": "...", "focus": "kotturpuram_bridge"}]


def test_r3_new_ambiguous_location_outranks_focus():
    r = ask("Guindy bridge is blocked; close it.", Scenario("B"), history=HIST)
    assert r["proposals"] == [] and r["clarification"]
    assert "kotturpuram_bridge" not in r["clarification"]["pending"]["candidates"]


def test_r3_confirmation_keeps_new_target_operation_and_direction():
    r1 = ask("Guindy bridge northbound is blocked; close it.", Scenario("B"), history=HIST)
    r2 = ask("Guindy bridge northbound is blocked; close it.", Scenario("B"), history=HIST,
             confirm={"crossing_id": "maraimalai_adigal_bridge", "pending": r1["clarification"]["pending"]})
    assert [(p["kind"], p["scenario"]["closed_carriageways"]) for p in r2["proposals"]] == [("close", [NB])]


def test_r3_pronoun_uses_focus_only_without_a_new_location():
    r = ask("Close it.", Scenario("B"), history=HIST)
    assert r["proposals"][0]["scenario"]["closed_carriageways"] == ["kotturpuram_bridge:northbound",
                                                                    "kotturpuram_bridge:southbound"]
    assert r["proposals"][0]["authorized_by"] == "conversation context"
    r = ask("Saidapet bridge is blocked, close it.", Scenario("B"), history=HIST)   # named location wins
    assert sorted(r["proposals"][0]["scenario"]["closed_carriageways"]) == [NB, SB]


def test_r3_tool_layer_refuses_context_when_message_names_a_location():
    tb = ToolBox(ST, Scenario("B"), user_msg="Guindy bridge is blocked; close it.")
    assert tb.authorize_context("kotturpuram_bridge") is False
    assert tb.call("propose_closure", {"report_text": "x", "crossing_id": "kotturpuram_bridge"})["code"] in (
        "unauthorized_target", "ambiguous_location")


# ================================================================== R4 latest ranking
MSG4 = "First rank recovery options, then reserve Maraimalai Adigal bridge for emergency vehicles and rank again."


def test_r4_briefing_uses_latest_ranking_for_final_scenario():
    tb = ToolBox(ST, Scenario("B", ALL), user_msg=MSG4)
    tb.call("rank_interventions", {"top_n": 3})
    tb.call("set_planning_constraint", {"kind": "reserve_emergency", "crossing_id": "maraimalai_adigal_bridge"})
    tb.call("rank_interventions", {"top_n": 3})
    text, _ = briefing.compose(tb.steps, tb.work.key())
    current = text.split("If the proposal is applied")[1]
    assert "Kotturpuram" in current.split(".")[0] + current and "Reopen all of Maraimalai" not in current.split("Excluded")[0]
    assert "historical" in text.split("If the proposal is applied")[0]
    assert "Current recommendation" not in text                     # nothing presents the old ranking as current


def test_r4_follow_up_ranking_is_for_final_scenario(monkeypatch):
    import contraflow.agent.runtime as rt
    monkeypatch.setattr(rt.llm, "llm_status", lambda: {"llm_available": True, "model": "stub", "base_url": "", "reason": None})
    calls = iter([
        {"content": "", "tool_calls": [{"id": "1", "function": {"name": "rank_interventions", "arguments": "{}"}}]},
        {"content": "", "tool_calls": [{"id": "2", "function": {"name": "set_planning_constraint", "arguments":
            '{"kind": "reserve_emergency", "crossing_id": "maraimalai_adigal_bridge"}'}}]},
        {"content": "done", "tool_calls": []}])
    monkeypatch.setattr(rt.llm, "chat", lambda *a, **k: next(calls))
    r = run_agent(ST, MSG4, Scenario("B", ALL), [], "llm", None)
    ranks = [s for s in r["steps"] if s.get("tool") == "rank_interventions"]
    assert ranks[-1]["result"]["scenario_key"] == Scenario.from_dict(r["working_scenario"]).key()
    assert "Kotturpuram" in r["message"].split("If the proposal is applied")[1]
    assert r["proposals"][0]["state"] == "proposed"


# ================================================================== R5 prediction == application
SCENARIOS = [
    Scenario("B", [NB], list(NET.carriageway(NB)["eids"])),            # overlapping representations
    Scenario("B", [NB]), Scenario("B", ALL), Scenario("A", ALL),
    Scenario("B", ALL, constraints=[{"kind": "reserve_emergency", "target": NB}]),
    Scenario("B", ALL, constraints=[{"kind": "no_reopen", "target": "kotturpuram_bridge:northbound"}]),
    Scenario("B", [], list(NET.carriageway(SB)["eids"])),               # carriageway closed only via segments
]


def _independent_effect(cur: dict, after: dict, zones) -> tuple[list, list, float]:
    """Test-side recomputation (does NOT reuse production candidate_effect)."""
    lost = sorted(z for z in zones if z in cur and z not in after)
    restored = sorted(z for z in zones if z not in cur and z in after)
    net = sum(cur[z]["dist"] - after[z]["dist"] for z in zones if z in cur and z in after
              and abs(cur[z]["dist"] - after[z]["dist"]) > 0.5)
    return lost, restored, round(net, 1)


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.key())
def test_r5_every_candidate_prediction_matches_application(sc):
    from contraflow.graphdb import Ledger
    iv = ENG.interventions(sc)
    cur = ENG.raw_access(ENG.normalize(sc), Ledger())
    zones = list(ENG.baseline(sc.rule))
    for c in iv["ranking"]:
        out = ENG.apply_result(sc, c["id"], c["base_key"])               # server rebuilds the operation by id
        applied = out["scenario"]
        assert applied.key() == c["result_key"], c["id"]                   # same canonical transformation
        ev = ENG.evaluate(applied)                                        # independent evaluation of the result
        assert abs(ev["metrics"]["total_delta_m"] - c["after_total_delta_m"]) < 0.2, c["id"]
        assert ev["metrics"]["cut_off"] == c["after_cut_off"], c["id"]
        after = ENG.raw_access(applied, Ledger())
        lost, restored, net = _independent_effect(cur, after, zones)
        assert (lost, restored) == (sorted(c["zones_lost"]), sorted(c["zones_restored"])), c["id"]
        assert abs(net - c["recovered_m"]) < 0.2, c["id"]
        assert (out["candidate"]["effect"], out["candidate"]["recovered_m"]) == (c["effect"], c["recovered_m"])
    for c in iv["excluded"]:
        assert c["id"] not in {x["id"] for x in iv["ranking"]}
        with pytest.raises(ValidationError):                               # prohibited candidates cannot be applied
            ENG.apply_result(sc, c["id"], iv["scenario_key"])


def test_r5_overlap_reopening_predicted_and_applied_identically():
    sc = SCENARIOS[0]
    c = next(x for x in ENG.interventions(sc)["ranking"] if x["id"] == f"reopen:{NB}")
    assert c["recovered_m"] == pytest.approx(11920.5, abs=0.2)
    out = ENG.apply_result(sc, c["id"], c["base_key"])
    assert ENG.evaluate(out["scenario"])["metrics"]["total_delta_m"] == pytest.approx(0.0, abs=0.2)


def test_r5_stale_candidates_refused():
    sc = Scenario("B", ALL)
    c = next(x for x in ENG.interventions(sc)["ranking"] if x["id"].startswith("reopen:kotturpuram"))
    newer = Scenario("B", ALL, constraints=[{"kind": "no_reopen", "target": "kotturpuram_bridge:northbound"}])
    with pytest.raises(ValidationError) as e:
        ENG.apply_result(newer, c["id"], c["base_key"])
    assert e.value.code == "stale_candidate"
    r = client.post("/api/apply_candidate", json={"current": newer.to_dict(), "candidate_id": c["id"],
                                                  "base_key": c["base_key"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_candidate"
    ok = client.post("/api/apply_candidate", json={"current": sc.to_dict(), "candidate_id": c["id"],
                                                   "base_key": c["base_key"]})
    assert ok.status_code == 200 and Scenario.from_dict(ok.json()["scenario"]).key() == c["result_key"]


def test_r5_normalization_is_canonical():
    a = ENG.normalize(Scenario("B", [NB], list(NET.carriageway(NB)["eids"])))
    b = ENG.normalize(Scenario("B", [], list(NET.carriageway(NB)["eids"])))
    assert a.key() == b.key() == ENG.normalize(Scenario("B", [NB])).key()


# ================================================================== provenance
def test_model_only_reply_is_labelled_model_written(monkeypatch):
    import contraflow.agent.runtime as rt
    monkeypatch.setattr(rt.llm, "llm_status", lambda: {"llm_available": True, "model": "stub", "base_url": "", "reason": None})
    monkeypatch.setattr(rt.llm, "chat", lambda *a, **k: {"content": "Everything is fine; 3 bridges open.", "tool_calls": []})
    r = run_agent(ST, "hello there", Scenario("B"), [], "llm", None)
    assert r["briefing_source"] == "model" and r["proposals"] == []


# ================================================================== live Qwen (opt-in)
@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
@pytest.mark.parametrize("msg,sc", [("Do not close Saidapet bridge.", []),
                                    ("Do not reserve Maraimalai Adigal for emergency vehicles.", ALL)])
def test_live_r1_negation(msg, sc):
    r = ask(msg, Scenario("B", sc), mode="llm")
    assert r["mode"]["kind"] == "llm" and r["proposals"] == []


@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
def test_live_r2_northbound_only():
    r = ask("Close Maraimalai Adigal bridge northbound only.", Scenario("B"), mode="llm")
    assert r["proposals"] and all(p["scenario"]["closed_carriageways"] == [NB] for p in r["proposals"])


@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
def test_live_r3_focus_does_not_override_new_location():
    r = ask("Guindy bridge is blocked; close it.", Scenario("B"), mode="llm", history=HIST)
    assert not any("kotturpuram_bridge" in c for p in r["proposals"] for c in p["scenario"]["closed_carriageways"])
    assert r["proposals"] == [] and r["clarification"]


@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
def test_live_r4_latest_ranking():
    r = ask(MSG4, Scenario("B", ALL), mode="llm")
    assert [p["kind"] for p in r["proposals"]] == ["constraint"]
    assert "If the proposal is applied" in r["message"]
    assert "Kotturpuram" in r["message"].split("If the proposal is applied")[1]
    assert "Current recommendation" not in r["message"]
