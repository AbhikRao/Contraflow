"""Measure real FalkorDB/engine latencies on the Adyar network. Writes results/benchmarks.json.
Usage: .venv/bin/python scripts/benchmark.py [--llm]"""

import json
import platform
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contraflow.agent.runtime import run_agent  # noqa: E402
from contraflow.engine import Engine, Scenario  # noqa: E402
from contraflow.graphdb import BASE, DB, Ledger  # noqa: E402
from contraflow.model import build  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def stats(xs):
    xs = sorted(xs)
    return {"n": len(xs), "median_ms": round(st.median(xs), 2), "p95_ms": round(xs[max(0, int(0.95 * len(xs)) - 1)], 2),
            "min_ms": round(xs[0], 2), "max_ms": round(xs[-1], 2)}


def timeit(fn, n):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return stats(out)


def main():
    net = build()
    db = DB()
    t = time.perf_counter()
    load = db.load_base(net, force=True)
    import_ms = (time.perf_counter() - t) * 1000
    eng = Engine(net, db)
    allc = [c["id"] for x in net.crossings for c in x["carriageways"]]
    mab = [c["id"] for c in net.crossing("maraimalai_adigal_bridge")["carriageways"]]
    g = db.graph(BASE)
    from contraflow.engine import ACCESS_QUERY
    res = {
        "environment": {"machine": platform.platform(), "python": platform.python_version(),
                        "falkordb_module": db.client.connection.execute_command("MODULE", "LIST")[0][3],
                        "network": {"junctions": len(net.junctions), "segments": len(net.segments), "zones": len(net.zones)},
                        "date": time.strftime("%Y-%m-%d %H:%M")},
        "import_base_graph_ms": round(import_ms, 1), "import_detail": load,
        "access_query_all_zones_base_graph": timeit(lambda: g.q("bench", ACCESS_QUERY, {"hub": "__HUB_B__"}), 50),
    }
    clone_times = []
    for _ in range(30):
        t = time.perf_counter()
        with db.scenario_clone():
            pass
        clone_times.append((time.perf_counter() - t) * 1000)
    res["graph_copy_plus_delete"] = stats(clone_times)
    db.sweep_scenarios()
    res["evaluate_baseline"] = timeit(lambda: eng.evaluate(Scenario("B")), 30)
    res["evaluate_single_bridge_closure"] = timeit(lambda: eng.evaluate(Scenario("B", mab)), 30)
    res["evaluate_all_crossings_closed"] = timeit(lambda: eng.evaluate(Scenario("B", allc)), 30)
    lg = Ledger()
    eng.interventions(Scenario("B", allc), lg)
    res["interventions_all_closed"] = {**timeit(lambda: eng.interventions(Scenario("B", allc)), 10),
                                       "candidates": len(eng.interventions(Scenario("B", allc))["ranking"]),
                                       "falkordb_operations": len(lg.entries)}
    st_ = {"net": net, "db": db, "engine": eng}
    res["agent_deterministic_field_report"] = timeit(
        lambda: run_agent(st_, "Saidapet bridge is blocked.", Scenario("B"), [], "deterministic", None), 10)
    if "--llm" in sys.argv:
        res["agent_local_llm_field_report"] = timeit(
            lambda: run_agent(st_, "Saidapet bridge is blocked.", Scenario("B"), [], "llm", None), 5)
    db.sweep_scenarios()
    (ROOT / "results" / "benchmarks.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
