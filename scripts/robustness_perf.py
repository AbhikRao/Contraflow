"""Timing and outcomes of Engine.robustness on the established scenarios (3 runs each). Does not sweep clones.
Usage: .venv/bin/python scripts/robustness_perf.py OUT.json"""
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contraflow.engine import Scenario  # noqa: E402
from contraflow.mcp_server import _state  # noqa: E402

st = _state(); eng = st["engine"]
K = ["kotturpuram_bridge:northbound", "kotturpuram_bridge:southbound"]
ALL = [c["id"] for x in st["net"].crossings for c in x["carriageways"]]
CASES = {"kotturpuram_closed_B": Scenario("B", K),
         "demo_kotturpuram_reserve_maraimalai_sb_B": Scenario("B", K, constraints=[{"kind": "reserve_emergency",
                                                                                   "target": "maraimalai_adigal_bridge:southbound"}]),
         "all_crossings_closed_B": Scenario("B", ALL), "all_crossings_closed_A": Scenario("A", ALL)}
out = {}
for name, sc in CASES.items():
    runs = [eng.robustness(sc) for _ in range(3)]
    r = runs[-1]
    assert all([v.get("status") for v in x["variations"]] == [v.get("status") for v in r["variations"]] for x in runs)
    out[name] = {"median_ms": statistics.median(x["elapsed_ms"] for x in runs), "runs_ms": [x["elapsed_ms"] for x in runs],
                 "graph_ops": r["ledger"]["queries"], "graph_copies": r["ledger"]["by_op"]["graph_copy"]["count"],
                 "original": r["original"] and r["original"]["label"], "summary": r["summary"],
                 "variations": [{"changed": v["changed"], "status": v.get("status") or "rejected",
                                 "best": (v.get("best") or {}).get("label"), "recovered_km": (v.get("best") or {}).get("recovered_km"),
                                 "disruption_km": round(v["current"]["total_delta_m"] / 1000, 2) if v.get("current") else None,
                                 "reason": v.get("reason")} for v in r["variations"]]}
    print(name, out[name]["median_ms"], out[name]["graph_ops"], r["summary"])
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
