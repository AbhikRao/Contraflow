"""Correctness gate: FalkorDB algo.maxFlow + in-DB min-cut vs NetworkX.

Run against the pinned server (port 6379) and, for comparison, v4.22.0 (6380):
    FALKOR_PORTS=6379 .venv/bin/pytest -q feasibility/test_maxflow_correctness_archive.py   (archived: expected to FAIL, documents the engine bug)
"""

import json
import os
import random
from pathlib import Path

import pytest
from falkordb import FalkorDB

from feasibility.flowlib import (Edge, falkor_maxflow, falkor_mincut, load_graph,
                                 normalize_parallel, nx_maxflow, verify)

PORTS = [int(p) for p in os.environ.get("FALKOR_PORTS", "6379").split(",")]
RESULTS = Path(__file__).resolve().parents[1] / "results"
LOG: list[dict] = []


def E(eid, u, v, cap):
    return Edge(eid, u, v, cap)


def run_case(port, name, raw_edges, sources, sinks):
    """Load, solve, cut, verify. Returns the check dict (also logged)."""
    edges, splits = normalize_parallel(raw_edges)
    g = FalkorDB(port=port).select_graph(f"t_{name}")
    try:
        g.delete()
    except Exception:
        pass
    load_graph(g, edges)
    value, flows, _ = falkor_maxflow(g, sources, sinks)
    cut = falkor_mincut(g, sources, flows)
    ref = nx_maxflow(raw_edges, sources, sinks)
    checks = verify(edges, sources, sinks, value, flows, cut)
    checks["matches_networkx"] = abs(value - ref) <= 1e-6 * max(1.0, ref)
    g.delete()
    LOG.append({"port": port, "case": name, "falkordb": value, "networkx": ref,
                "cut_capacity": cut["cut_capacity"], "cut_size": len(cut["cut"]),
                "splits": len(splits), **checks})
    return checks, value, ref, cut


CASES = {
    # name: (edges, sources, sinks, expected)
    "single_path": ([E("a", "A", "B", 5), E("b", "B", "C", 3)], ["A"], ["C"], 3),
    "doc_example": ([E("a", "A", "B", 10), E("b", "A", "C", 5), E("c", "B", "C", 8)], ["A"], ["C"], 13),
    "directed_only": ([E("a", "A", "B", 5), E("b", "C", "B", 5)], ["A"], ["C"], 0),
    "opposite_directions": ([E("ab", "A", "B", 7), E("ba", "B", "A", 4), E("bc", "B", "C", 9)], ["A"], ["C"], 7),
    "zero_capacity_road": ([E("a", "A", "B", 0), E("b", "A", "C", 4), E("c", "C", "B", 6)], ["A"], ["B"], 4),
    "parallel_roads": ([E("p1", "A", "B", 3), E("p2", "A", "B", 4), E("p3", "A", "B", 2), E("bc", "B", "C", 100)],
                       ["A"], ["C"], 9),
    "disconnected": ([E("a", "A", "B", 5), E("b", "C", "D", 5)], ["A"], ["D"], 0),
    "multi_source_multi_sink": ([E("1", "S1", "M", 4), E("2", "S2", "M", 6), E("3", "M", "T1", 3),
                                 E("4", "M", "T2", 5), E("5", "S2", "T2", 2)], ["S1", "S2"], ["T1", "T2"], 10),
    "fractional_caps": ([E("a", "A", "B", 1.5), E("b", "A", "C", 2.25), E("c", "B", "D", 1.0),
                         E("d", "C", "D", 3.75)], ["A"], ["D"], 3.25),
    "residual_reverse_needed": (  # classic case where augmenting must cancel flow on B->C
        [E("ab", "A", "B", 1), E("ac", "A", "C", 1), E("bc", "B", "C", 1), E("bd", "B", "D", 1),
         E("cd", "C", "D", 1)], ["A"], ["D"], 2),
}


@pytest.mark.parametrize("port", PORTS)
@pytest.mark.parametrize("name", list(CASES))
def test_fixture(port, name):
    edges, src, dst, expected = CASES[name]
    checks, value, ref, _ = run_case(port, name, edges, src, dst)
    assert abs(value - expected) < 1e-6 and abs(ref - expected) < 1e-6
    assert all(v for v in checks.values() if isinstance(v, bool)), checks


@pytest.mark.parametrize("port", PORTS)
def test_raw_multiedge_rejected(port):
    """Documents the engine constraint that motivates normalize_parallel."""
    g = FalkorDB(port=port).select_graph("t_multi")
    try:
        g.delete()
    except Exception:
        pass
    g.query("CREATE (a:J {id:'A'})-[:ROAD {cap:1.0}]->(b:J {id:'B'}), (a)-[:ROAD {cap:2.0}]->(b)")
    with pytest.raises(Exception) as ex:
        g.query("MATCH (s:J {id:'A'}), (t:J {id:'B'}) CALL algo.maxFlow({sourceNodes:[s], targetNodes:[t], "
                "capacityProperty:'cap', relationshipTypes:['ROAD']}) YIELD maxFlow RETURN maxFlow")
    LOG.append({"port": port, "case": "raw_multiedge", "error": str(ex.value)})
    g.delete()


def random_graph(rng, n=40, m=160):
    nodes = [f"n{i}" for i in range(n)]
    edges = []
    for i in range(m):
        u, v = rng.sample(nodes, 2)
        edges.append(Edge(f"e{i}", u, v, float(rng.choice([300, 600, 900, 1200, 1800, 2400, 3600]))))
    return nodes, edges


@pytest.mark.parametrize("port", PORTS)
def test_random_perturbations(port):
    """60 reproducible perturbations: closures (delete / zero cap), capacity changes,
    multiple sources & sinks, parallel arcs from random sampling."""
    rng = random.Random(20261015)
    failures = []
    for k in range(60):
        nodes, edges = random_graph(rng)
        src = rng.sample(nodes[:10], rng.choice([1, 2, 3]))
        dst = rng.sample(nodes[-10:], rng.choice([1, 2, 3]))
        mode = k % 3
        if mode == 0:     # close roads by removing them
            edges = [e for e in edges if rng.random() > 0.15]
        elif mode == 1:   # close roads by zero capacity
            edges = [Edge(e.eid, e.u, e.v, 0.0 if rng.random() < 0.15 else e.cap) for e in edges]
        else:             # capacity changes (e.g. lane loss / contraflow gain)
            edges = [Edge(e.eid, e.u, e.v, e.cap * rng.choice([0.5, 1.0, 1.0, 1.5, 2.0])) for e in edges]
        checks, value, ref, _ = run_case(port, f"rand{k}", edges, src, dst)
        if not all(v for v in checks.values() if isinstance(v, bool)):
            failures.append((k, value, ref, checks))
    assert not failures, failures


def teardown_module(module):
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "correctness_log.json").write_text(json.dumps(LOG, indent=1, default=str))
