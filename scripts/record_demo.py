"""Run the locked demo sequence against FalkorDB and record every number used in the README,
the video script and the UI walkthrough. Writes results/demo_run.json and prints a summary.
Usage: .venv/bin/python scripts/record_demo.py"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contraflow.agent.runtime import run_agent  # noqa: E402
from contraflow.engine import Engine, Scenario  # noqa: E402
from contraflow.graphdb import DB  # noqa: E402
from contraflow.model import build  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main():
    net, db = build(), DB()
    db.load_base(net)
    eng = Engine(net, db)
    zn = {z["id"]: z["name"] for z in net.zones}
    bridge = {x["name"]: [c["id"] for c in x["carriageways"]] for x in net.crossings}
    allc = [c for v in bridge.values() for c in v]
    out = {"recorded": time.strftime("%Y-%m-%d %H:%M"), "rules": {}}

    def summary(r):
        m = r["metrics"]
        return {"total_delta_km": round(m["total_delta_m"] / 1000, 2), "affected_zones": m["affected_zones"],
                "cut_off": [zn[z] for z in m["cut_off"]],
                "largest": {"zone": zn[m["largest"]["zone"]], "delta_km": round(m["largest"]["delta_m"] / 1000, 2)}
                if m["largest"] else None}

    for rule in ("B", "A"):
        base = eng.baseline(rule)
        o = {"baseline_total_km": round(sum(v["dist"] for v in base.values()) / 1000, 2), "single_bridge": {},
             "single_carriageway": {}}
        for name, cws in bridge.items():
            o["single_bridge"][name] = summary(eng.evaluate(Scenario(rule, cws)))
            if len(cws) > 1:
                for c in cws:
                    o["single_carriageway"][c] = summary(eng.evaluate(Scenario(rule, [c])))
        o["all_crossings_closed"] = summary(eng.evaluate(Scenario(rule, allc)))
        iv = eng.interventions(Scenario(rule, allc))
        o["recovery_ranking_all_closed"] = [
            {"rank": i + 1, "action": c["label"], "effect": c["effect"], "zones_lost": [zn[z] for z in c["zones_lost"]],
             "recovered_km": round(c["recovered_m"] / 1000, 2),
             "share_pct": round(c["share_of_disruption"] * 100, 1) if c["share_of_disruption"] is not None else None,
             "zones_improved": [zn[z["zone"]] for z in c["zones_improved"]]}
            for i, c in enumerate(iv["ranking"])]
        res = eng.interventions(Scenario(rule, allc, constraints=[{"kind": "reserve_emergency", "target": c}
                                                                  for c in bridge["Maraimalai Adigal Bridge"]]))
        o["ranking_with_maraimalai_reserved"] = [
            {"rank": i + 1, "action": c["label"], "recovered_km": round(c["recovered_m"] / 1000, 2)}
            for i, c in enumerate(res["ranking"][:4])]
        out["rules"][rule] = o

    st_ = {"net": net, "db": db, "engine": eng}
    agent = []
    pending = None
    for msg, sc, conf in [("Saidapet bridge is blocked.", Scenario("B"), None),
                          ("Guindy bridge is under water.", Scenario("B"), None),
                          ("Guindy bridge is under water.", Scenario("B"), "maraimalai_adigal_bridge"),
                          ("Guindy bridge has reopened.", Scenario("B", allc), None),
                          ("Saidapet bridge is not blocked.", Scenario("B"), None),
                          ("What should we reopen first?", Scenario("B", allc), None),
                          ("Keep Maraimalai Adigal available for inbound emergency access.", Scenario("B", allc), None),
                          ("Why doesn't closing Thiru Vi Ka matter?", Scenario("B"), None)]:
        if conf:
            conf = {"crossing_id": conf, "pending": pending}
        r = run_agent(st_, msg, sc, [], "deterministic", conf)
        pending = (r["clarification"] or {}).get("pending")
        agent.append({"message": msg, "confirm": conf, "reply": r["message"], "grounding": r["grounding"],
                      "tools": [s.get("tool") for s in r["steps"] if s.get("tool")],
                      "proposals": [p["label"] for p in r["proposals"]],
                      "clarification": r["clarification"] and [o["label"] for o in r["clarification"]["options"]]})
    out["agent_deterministic_replay"] = agent
    db.sweep_scenarios()
    (ROOT / "results" / "demo_run.json").write_text(json.dumps(out, indent=1))
    b = out["rules"]["B"]
    print("rule B baseline", b["baseline_total_km"], "km")
    for k, v in b["single_bridge"].items():
        print(f"  close {k:26s} {v}")
    print("  all closed", b["all_crossings_closed"])
    for r in b["recovery_ranking_all_closed"][:5]:
        print("   ", r)
    print("  reserved:", b["ranking_with_maraimalai_reserved"][:2])
    a = out["rules"]["A"]
    print("rule A all closed", a["all_crossings_closed"], a["recovery_ranking_all_closed"][0])


if __name__ == "__main__":
    main()
