"""Regression tests for final blockers B1 (passive/descriptive negation) and B2 (candidate trust boundary).
B3/B4 are covered in tests/test_ui_regressions.py (real browser)."""
import os

import pytest
from fastapi.testclient import TestClient

from contraflow.agent.intent import parse_intent
from contraflow.agent.runtime import run_agent
from contraflow.agent.tools import ToolBox
from contraflow.api import app, state
from contraflow.engine import Scenario

ST = state()
NET, ENG = ST["net"], ST["engine"]
ALL = [c["id"] for x in NET.crossings for c in x["carriageways"]]
NB = "maraimalai_adigal_bridge:northbound"
LLM = os.environ.get("CONTRAFLOW_TEST_LLM") == "1"
client = TestClient(app)
B1_EXACT = ["Maraimalai Adigal must not be reserved for emergency vehicles.",
            "Maraimalai Adigal is not reserved for emergency vehicles."]


# ================================================================== B1
NEGATIVE = [  # (message, operation) — none of these may ever authorize a mutation
    *[(m, "reserve_emergency") for m in B1_EXACT],
    ("Maraimalai Adigal should not be kept for ambulances.", "reserve_emergency"),
    ("Maraimalai Adigal cannot be reserved for emergency vehicles", "reserve_emergency"),
    ("Maraimalai Adigal must never be reserved for emergency vehicles", "reserve_emergency"),
    ("Maraimalai Adigal isn't reserved for emergency vehicles", "reserve_emergency"),
    ("Maraimalai Adigal has not been reserved for emergency vehicles", "reserve_emergency"),
    ("Saidapet bridge must not be closed.", "close"), ("Saidapet bridge should not be blocked", "close"),
    ("Saidapet bridge cannot be shut", "close"), ("Saidapet bridge has not been closed", "close"),
    ("Saidapet bridge isn't blocked", "close"), ("Saidapet bridge won't be closed", "close"),
    ("Kotturpuram bridge has not been reopened.", "reopen"), ("Kotturpuram bridge is not reopened", "reopen"),
]


@pytest.mark.parametrize("msg,op", NEGATIVE)
def test_b1_negative_statements_never_authorize(msg, op):
    i = parse_intent(msg)
    assert i.operation == op and i.polarity == "negative" and not i.may_mutate, i.to_dict()
    for sc in (Scenario("B"), Scenario("B", ALL)):
        r = run_agent(ST, msg, sc, [], "deterministic", None)
        assert r["proposals"] == [], (msg, r["message"])


@pytest.mark.parametrize("msg,op", [
    ("Kotturpuram bridge must not be reopened.", "no_reopen"), ("Kotturpuram bridge cannot be reopened yet", "no_reopen"),
    ("Kotturpuram bridge must not be reversed.", "keep_direction"), ("Do not reverse the Saidapet bridge", "keep_direction"),
])
def test_b1_prohibitions_become_their_own_constraints(msg, op):
    """'must not be reopened/reversed' are commander prohibitions (affirmative constraint operations), not their opposite."""
    i = parse_intent(msg)
    assert (i.operation, i.polarity, i.mood, i.may_mutate) == (op, "affirmative", "imperative", True)


@pytest.mark.parametrize("msg", ["Maraimalai Adigal is reserved for emergency vehicles.",
                                 "Kotturpuram bridge is reversed.", "Kotturpuram bridge is not reversed."])
def test_b1_descriptions_do_not_become_constraint_commands(msg):
    i = parse_intent(msg)
    assert not i.may_mutate
    r = run_agent(ST, msg, Scenario("B", ALL), [], "deterministic", None)
    assert r["proposals"] == []


@pytest.mark.parametrize("msg", ["Reserve Maraimalai Adigal for emergency vehicles.",
                                 "Maraimalai Adigal must be reserved for emergency vehicles.",
                                 "Keep Maraimalai Adigal available for inbound emergency access."])
def test_b1_intentional_affirmative_reservations_still_work(msg):
    r = run_agent(ST, msg, Scenario("B", ALL), [], "deterministic", None)
    assert [p["kind"] for p in r["proposals"]] == ["constraint"]


@pytest.mark.parametrize("msg", B1_EXACT + ["Maraimalai Adigal is reserved for emergency vehicles."])
def test_b1_model_tool_calls_get_the_same_checks(msg):
    tb = ToolBox(ST, Scenario("B", ALL), user_msg=msg)
    r = tb.call("set_planning_constraint", {"kind": "reserve_emergency", "crossing_id": "maraimalai_adigal_bridge"})
    assert r["code"] in ("negated_instruction", "intent_mismatch") and tb.proposals == []


@pytest.mark.parametrize("msg", B1_EXACT)
def test_b1_runtime_completion_never_overrides(monkeypatch, msg):
    import contraflow.agent.runtime as rt
    monkeypatch.setattr(rt.llm, "llm_status", lambda: {"llm_available": True, "model": "stub", "base_url": "", "reason": None})
    calls = iter([{"content": "", "tool_calls": [{"id": "1", "function": {"name": "set_planning_constraint",
                   "arguments": '{"kind": "reserve_emergency", "crossing_id": "maraimalai_adigal_bridge"}'}}]}]
                 + [{"content": "done", "tool_calls": []}] * 10)
    monkeypatch.setattr(rt.llm, "chat", lambda *a, **k: next(calls))
    r = run_agent(ST, msg, Scenario("B"), [], "llm", None)
    assert r["proposals"] == []
    assert not any("completed by deterministic" in (s.get("text") or "") for s in r["steps"])


@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
@pytest.mark.parametrize("msg", B1_EXACT)
def test_live_b1_exact_examples(msg):
    r = run_agent(ST, msg, Scenario("B"), [], "llm", None)
    assert r["mode"]["kind"] == "llm" and r["proposals"] == []


# ================================================================== B2
def _rank(sc):
    return ENG.interventions(sc)


def _post(current, candidate_id, base_key, **extra):
    return client.post("/api/apply_candidate", json={"current": current.to_dict(), "candidate_id": candidate_id,
                                                     "base_key": base_key, **extra})


def test_b2_paired_substitution_cannot_change_the_operation():
    sc = Scenario("B", ALL)
    rank = {c["id"]: c for c in _rank(sc)["ranking"]}
    mab, kot = rank["reopen_bridge:maraimalai_adigal_bridge"], rank["reopen:kotturpuram_bridge:northbound"]
    # The old exploit: claim MAB, supply Kotturpuram's result + key. Those fields are ignored now.
    r = _post(sc, mab["id"], mab["base_key"], result_scenario=kot["result_scenario"], result_key=kot["result_key"],
              candidate={**mab, "result_scenario": kot["result_scenario"], "result_key": kot["result_key"]})
    assert r.status_code == 200 and r.json()["computed_by"] == "server"
    applied = Scenario.from_dict(r.json()["scenario"])
    assert applied.key() == mab["result_key"] != kot["result_key"]
    assert not {NB, "maraimalai_adigal_bridge:southbound"} & set(applied.closed_carriageways)
    assert "kotturpuram_bridge:northbound" in applied.closed_carriageways
    ev = ENG.evaluate(applied)["metrics"]["total_delta_m"]
    assert ev == pytest.approx(mab["after_total_delta_m"], abs=0.2)            # server-computed outcome
    assert r.json()["candidate"]["after_total_delta_m"] == pytest.approx(ev, abs=0.2)


def test_b2_old_self_authorizing_body_is_rejected():
    sc = Scenario("B", ALL)
    c = _rank(sc)["ranking"][0]
    r = client.post("/api/apply_candidate", json={"current": sc.to_dict(), "candidate": c})
    assert r.status_code == 422                                                # no id + key: not an operation request


def test_b2_no_reopen_constraint_cannot_be_bypassed():
    cons = Scenario("B", ALL, constraints=[{"kind": "no_reopen", "target": NB}])
    free = {c["id"]: c for c in _rank(Scenario("B", ALL))["ranking"]}
    ranked = {c["id"]: c for c in _rank(cons)["ranking"]}
    kot = ranked["reopen:kotturpuram_bridge:northbound"]
    # substitute the unconstrained Maraimalai result into an eligible Kotturpuram request
    r = _post(cons, kot["id"], kot["base_key"], result_scenario=free["reopen_bridge:maraimalai_adigal_bridge"]["result_scenario"],
              result_key=free["reopen_bridge:maraimalai_adigal_bridge"]["result_key"])
    assert r.status_code == 200
    applied = Scenario.from_dict(r.json()["scenario"])
    assert NB in applied.closed_carriageways                                   # Maraimalai stays closed
    assert {(k["kind"], k["target"]) for k in applied.constraints} == {("no_reopen", NB)}   # constraint preserved
    # asking for the prohibited operation directly is refused
    for cid in ("reopen_bridge:maraimalai_adigal_bridge", f"reopen:{NB}"):
        r = _post(cons, cid, kot["base_key"])
        assert r.status_code == 422 and r.json()["detail"]["code"] in ("constraint_violation", "ineligible_candidate")


@pytest.mark.parametrize("cid", [
    "reopen:nonexistent", "reopen_bridge:kotturpuram_bridge",                 # bridge with only one closed direction
    "reopen_bridge:maraimalai_adigal_bridge",                                 # broadening beyond the closed direction
    "reopen_seg:308198209_256035491_0", "reverse:308198209_256035491_0", "drop_constraint:x", "",
])
def test_b2_forged_or_broadened_ids_refused(cid):
    sc = Scenario("B", [NB, "kotturpuram_bridge:northbound"])
    key = ENG.normalize(sc).key()
    r = _post(sc, cid or "x", key)
    assert r.status_code == 422 and r.json()["detail"]["code"] in ("ineligible_candidate",)


def test_b2_reserved_carriageway_cannot_be_reopened_by_id():
    sc = Scenario("B", ALL, constraints=[{"kind": "reserve_emergency", "target": NB}])
    r = _post(sc, f"reopen:{NB}", ENG.normalize(sc).key())
    assert r.status_code == 422 and r.json()["detail"]["code"] == "constraint_violation"


def test_b2_server_computes_prediction_not_client():
    sc = Scenario("B", ALL)
    c = next(x for x in _rank(sc)["ranking"] if x["id"] == "reopen_bridge:maraimalai_adigal_bridge")
    r = _post(sc, c["id"], c["base_key"], after_total_delta_m=0, recovered_m=999999, effect="restores_access")
    body = r.json()["candidate"]
    assert body["recovered_m"] == pytest.approx(11920.5, abs=0.2) and body["effect"] == "shorter"
