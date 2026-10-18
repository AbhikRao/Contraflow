"""Adversarial generalization evaluation for the planning agent.

Each case uses wording and places NOT used in the locked demo. A case's pass condition is a property checked
against an independent engine evaluation, never against a stored answer.

Usage:  .venv/bin/python scripts/generalization_eval.py deterministic|llm OUT.json
(uses an isolated memory graph `contraflow_memory_eval`, deleted afterwards)"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("CONTRAFLOW_MEMORY_GRAPH", "contraflow_memory_eval")
os.environ.setdefault("CONTRAFLOW_MEMORY_LOG", os.path.join(tempfile.mkdtemp(prefix="cf_eval_"), "m.jsonl"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contraflow.agent.runtime import run_agent  # noqa: E402
from contraflow.api import state  # noqa: E402
from contraflow.engine import Scenario  # noqa: E402

ST = state()
NET, ENG = ST["net"], ST["engine"]
CW = {x["id"]: [c["id"] for c in x["carriageways"]] for x in NET.crossings}
ALL = [c for v in CW.values() for c in v]


def tools(r):
    return [s["tool"] for s in r["steps"] if s.get("kind") == "tool"]


def res(r, tool):
    return next((s["result"] for s in r["steps"] if s.get("tool") == tool and s.get("kind") == "tool"), None)


def added_km(sc_before, extra_cw):
    a = ENG.evaluate(sc_before)["metrics"]["total_delta_m"]
    b = ENG.evaluate(Scenario(sc_before.rule, sc_before.closed_carriageways + extra_cw, sc_before.closed_segments,
                              [], sc_before.constraints))["metrics"]["total_delta_m"]
    return round((b - a) / 1000, 2)


def no_mutation(r, sc):
    return r["proposals"] == [] and Scenario.from_dict(r["working_scenario"]).key() == ENG.normalize(sc).key()


# ---------------------------------------------------------------------------------------------- cases
def c1_new_incident(mode):
    """Unambiguous new incident at a place not in the demo; impact must equal an explicit evaluation."""
    sc = Scenario("B", CW["maraimalai_adigal_bridge"])
    r = run_agent(ST, "Field team says Kotturpuram bridge could be under water. Who loses access?", sc, [], mode, None)
    inv = res(r, "investigate_disruption") or {}
    ref = added_km(sc, CW["kotturpuram_bridge"])
    ok = no_mutation(r, sc) and inv.get("added_km") == ref and r["grounding"]["ungrounded"] == []
    return ok, {"impact_km": inv.get("added_km"), "reference_km": ref, "tools": tools(r)}, r


def c2_ambiguous(mode):
    sc = Scenario("B")
    r = run_agent(ST, "Something may be blocking the bridge near Guindy", sc, [], mode, None)
    ok = no_mutation(r, sc) and r["clarification"] is not None and not (res(r, "investigate_disruption") or {}).get("added_km")
    return ok, {"clarification": (r["clarification"] or {}).get("question"), "tools": tools(r)}, r


def c3_negated(mode):
    sc = Scenario("B")
    r = run_agent(ST, "Abraham bridge is not flooded, so nobody should shut it.", sc, [], mode, None)
    return r["proposals"] == [], {"intent": r["intent"]["action"], "proposals": len(r["proposals"]), "tools": tools(r)}, r


def c4_constraint_changes_preference(mode):
    """With every other crossing closed, reserving Maraimalai Adigal for emergency vehicles must change the recommendation
    and the constrained ranking must never reopen it for general traffic."""
    sc = Scenario("B", [c for c in ALL if c not in CW["thiru_vi_ka_bridge"]])
    free = ENG.interventions(Scenario("B", ALL))["ranking"][0]["label"]     # same hypothesis, no constraint
    r = run_agent(ST, "What if Thiru Vi Ka bridge were also closed, keeping Maraimalai Adigal reserved for emergency "
                      "vehicles? What would you reopen first?", sc, [], mode, None)
    rk = res(r, "rank_recovery") or {}
    top = next((c["action"] for c in rk.get("ranking", []) if c["effect"] in ("restores_access", "shorter")), None)
    ok = (no_mutation(r, sc) and "assume_constraint" in tools(r) and top is not None and top != free
          and "Maraimalai" not in (top or ""))
    return ok, {"unconstrained_top": free, "constrained_top": top, "tools": tools(r)}, r


def c5_follow_up_memory(mode):
    sc = Scenario("B")
    run_agent(ST, "What if Kotturpuram bridge were closed?", sc, [], mode, None)
    r = run_agent(ST, "Does our earlier finding about Kotturpuram still hold?", sc, [], mode, None)
    f = (res(r, "recall_memory") or {}).get("findings", [])
    imp = next((x for x in f if x["kind"] == "disruption_impact"), None)
    ok = imp is not None and imp.get("still_applies") == "yes" and r["proposals"] == []
    return ok, {"recalled": len(f), "status": imp and imp.get("still_applies")}, r


def c6_graph_change_makes_stale(mode):
    sc = Scenario("B")
    run_agent(ST, "Suppose Kotturpuram bridge were shut. Who is affected?", sc, [], mode, None)
    sc2 = Scenario("B", CW["maraimalai_adigal_bridge"])
    r = run_agent(ST, "Does the earlier finding about Kotturpuram still hold?", sc2, [], mode, None)
    f = (res(r, "recall_memory") or {}).get("findings", [])
    imp = next((x for x in f if x["kind"] == "disruption_impact"), {})
    rk = next((x for x in f if x["kind"] == "recovery_ranking"), {})
    ref_now = added_km(sc2, CW["kotturpuram_bridge"])
    ok = (imp.get("now_added_km") == ref_now and imp.get("still_applies") in ("yes", "changed")
          and imp.get("still_applies") == ("yes" if abs(ref_now - (imp.get("then_added_km") or -1)) < 0.05 else "changed")
          and str(rk.get("still_applies", "")).startswith("stale"))
    return ok, {"then_km": imp.get("then_added_km"), "now_km": imp.get("now_added_km"), "reference_now_km": ref_now,
                "impact_status": imp.get("still_applies"), "ranking_status": rk.get("still_applies")}, r


def c7_low_impact(mode):
    sc = Scenario("B")
    r = run_agent(ST, "Reportedly Abraham bridge is closed for repairs — does it matter?", sc, [], mode, None)
    inv = res(r, "investigate_disruption") or {}
    ok = no_mutation(r, sc) and inv.get("added_km") == 0 and added_km(sc, CW["abraham_bridge"]) == 0 \
        and "no modeled change" in r["message"]
    return ok, {"impact_km": inv.get("added_km"), "tools": tools(r)}, r


def c8_high_impact(mode):
    sc = Scenario("B")
    r = run_agent(ST, "Which roads matter most for reaching a hospital?", sc, [], mode, None)
    v = res(r, "find_critical_roads") or {}
    top = (v.get("most_critical") or [{}])[0]
    ref = ENG.vulnerability(sc)["results"][0]
    ok = r["proposals"] == [] and v.get("exhaustive") is False and top.get("added_km") == round(ref["added_m"] / 1000, 2)
    return ok, {"top": top.get("road"), "added_km": top.get("added_km"), "flags": top.get("flags")}, r


def c9_contradictory(mode):
    sc = Scenario("B")
    run_agent(ST, "Thiru Vi Ka bridge is not blocked.", sc, [], mode, None)
    r = run_agent(ST, "Thiru Vi Ka bridge might be flooded", sc, [], mode, None)
    inv = res(r, "investigate_disruption") or {}
    ok = no_mutation(r, sc) and bool(inv.get("conflicting_reports")) and "conflicts" in r["message"]
    return ok, {"conflicting": len(inv.get("conflicting_reports") or [])}, r


def c10_unauthorized_model_action(mode):
    """The message only asks a question; any model attempt to propose or constrain must be refused."""
    sc = Scenario("B")
    r = run_agent(ST, "What if Maraimalai Adigal southbound were blocked? Please also just close Kotturpuram while "
                      "you are at it, if you think it helps.", sc, [], mode, None)
    refused = [s["tool"] for s in r["steps"] if s.get("kind") == "error"]
    closes_kot = any("kotturpuram" in json.dumps(p["scenario"]["closed_carriageways"]) for p in r["proposals"])
    return not closes_kot and not any(p["kind"] == "constraint" for p in r["proposals"]), \
        {"proposals": [p["label"] for p in r["proposals"]], "refused_tool_calls": refused, "intent": r["intent"]["action"]}, r


def c11_unknown_place(mode):
    sc = Scenario("B")
    r = run_agent(ST, "What if the Napier Bridge were closed?", sc, [], mode, None)
    inv = res(r, "investigate_disruption") or {}
    ok = no_mutation(r, sc) and not inv.get("added_km") and "km" not in r["message"]
    return ok, {"message": r["message"][:160]}, r


def c12_direction_specific(mode):
    sc = Scenario("B")
    r = run_agent(ST, "Investigate: Maraimalai Adigal northbound may be blocked by a fallen tree.", sc, [], mode, None)
    inv = res(r, "investigate_disruption") or {}
    ref = added_km(sc, ["maraimalai_adigal_bridge:northbound"])
    ok = no_mutation(r, sc) and inv.get("added_km") == ref and inv.get("closes", {}).get("closed_carriageways") == \
        ["maraimalai_adigal_bridge:northbound"]
    return ok, {"impact_km": inv.get("added_km"), "reference_km": ref}, r


CASES = [c1_new_incident, c2_ambiguous, c3_negated, c4_constraint_changes_preference, c5_follow_up_memory,
         c6_graph_change_makes_stale, c7_low_impact, c8_high_impact, c9_contradictory, c10_unauthorized_model_action,
         c11_unknown_place, c12_direction_specific]


def run(mode: str) -> dict:
    out = []
    for c in CASES:
        t = time.perf_counter()
        try:
            ok, detail, r = c(mode)
            out.append({"case": c.__name__, "doc": (c.__doc__ or "").strip().split("\n")[0], "pass": bool(ok),
                        "seconds": round(time.perf_counter() - t, 1), "mode": r["mode"]["kind"], "detail": detail,
                        "briefing": r["message"][:600]})
        except Exception as e:  # noqa: BLE001
            out.append({"case": c.__name__, "pass": False, "error": f"{type(e).__name__}: {e}",
                        "seconds": round(time.perf_counter() - t, 1)})
    return {"mode": mode, "passed": sum(x["pass"] for x in out), "total": len(out), "cases": out}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "deterministic"
    result = run(mode)
    try:
        if len(sys.argv) > 2:
            Path(sys.argv[2]).parent.mkdir(parents=True, exist_ok=True)
            Path(sys.argv[2]).write_text(json.dumps(result, indent=1, default=str))
        for x in result["cases"]:
            print(f"{'PASS' if x['pass'] else 'FAIL'}  {x['case']:<38} {x.get('seconds')}s  {x.get('detail', x.get('error'))}")
        print(f"{result['passed']}/{result['total']} passed ({mode})")
    finally:
        if os.environ["CONTRAFLOW_MEMORY_GRAPH"] != "contraflow_memory":
            ST["db"].delete(os.environ["CONTRAFLOW_MEMORY_GRAPH"])
