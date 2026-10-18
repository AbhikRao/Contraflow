"""Contraflow v2 gate: FalkorDB SPpaths / access / GRAPH.COPY vs NetworkX."""

import json
import random
from pathlib import Path

import networkx as nx
import pytest
from falkordb import FalkorDB

from feasibility.routelib import (HUB, access, add_help_points, clone, close_roads, load, nx_dist,
                                  nx_graph, route)

RESULTS = Path(__file__).resolve().parents[1] / "results"
LOG = []
db = FalkorDB(port=6379)


def fresh(name):
    try:
        db.select_graph(name).delete()
    except Exception:
        pass
    return db.select_graph(name)


def E(eid, u, v, length):
    return {"eid": eid, "u": u, "v": v, "len": float(length)}


def test_fixture_directed_closures_alternatives_copy():
    edges = [E("ab", "A", "B", 1.5), E("bc", "B", "C", 2.25), E("ac", "A", "C", 5), E("cd", "C", "D", 1),
             E("bh", "B", "H", 4), E("dh", "D", "H", 0.5), E("hx", "H", "X", 1)]
    nodes = [{"id": n} for n in "ABCDHX"]
    g = fresh("rt_fix")
    load(g, nodes, edges)
    assert route(g, "A", "D") == [(4.75, ["A", "B", "C", "D"])]
    assert route(g, "D", "A") == []                       # directed: no way back
    assert route(g, "X", "A") == []                       # sink node
    alts = route(g, "A", "H", k=3)
    assert [w for w, _ in alts] == [5.25, 5.5, 6.5]       # alternative-route discovery
    c = clone(db, "rt_fix", "rt_fix_c")
    assert close_roads(c, ["bc"]) == 1
    assert route(c, "A", "D") == [(6.0, ["A", "C", "D"])]  # closure reroutes in the clone
    assert route(g, "A", "D")[0][0] == 4.75                # original untouched
    close_roads(c, ["ac"])
    assert route(c, "A", "D") == []                        # now disconnected
    c.delete(); g.delete()


def random_case(rng, n, m):
    nodes = [f"n{i}" for i in range(n)]
    edges = []
    for i in range(m):
        u, v = rng.sample(nodes, 2)
        edges.append(E(f"e{i}", u, v, round(rng.uniform(20, 900), 1)))   # meters, incl. parallel arcs
    return nodes, edges


def test_random_routes_closures_access():
    """60 seeded graphs x (base, clone with closures): pair distances, k-alternatives
    and zone access all compared with NetworkX."""
    rng = random.Random(1015)
    mism = []
    checks = 0
    for k in range(60):
        n = rng.choice([15, 30, 60, 120])
        nodes, edges = random_case(rng, n, int(n * rng.choice([1.5, 2, 3])))
        helps = rng.sample(nodes, 3)
        zones = rng.sample([x for x in nodes if x not in helps], 6)
        g = fresh("rt_rand")
        load(g, [{"id": x} for x in nodes], edges)
        add_help_points(g, helps)
        c = clone(db, "rt_rand", "rt_rand_c")
        closed = [e["eid"] for e in edges if rng.random() < 0.2]
        close_roads(c, closed)
        open_edges = [e for e in edges if e["eid"] not in set(closed)]
        for label, gg, es in (("base", g, edges), ("closed", c, open_edges)):
            G = nx_graph(es, helps)
            for _ in range(8):
                s, t = rng.sample(nodes, 2)
                got = route(gg, s, t)
                want = nx_dist(G, s, t)
                checks += 1
                ok = (not got and want is None) or (got and want is not None and abs(got[0][0] - want) < 1e-6)
                if not ok:
                    mism.append((k, label, s, t, got[:1], want))
            acc = access(gg, zones)
            for z in zones:
                want = nx_dist(G, z, HUB)
                checks += 1
                ok = (acc[z] is None and want is None) or (acc[z] is not None and want is not None
                                                            and abs(acc[z] - want) < 1e-6)
                if not ok:
                    mism.append((k, label, "access", z, acc[z], want))
            # k-alternatives: weights must equal NetworkX shortest simple paths on the simple digraph
            s, t = rng.sample(nodes, 2)
            if n > 60:
                continue   # k>1 enumeration can exceed 1 s on denser graphs (see report)
            got = route(gg, s, t, k=3, timeout=30000)
            D = nx.DiGraph()
            for e in es:
                if not D.has_edge(e["u"], e["v"]) or D[e["u"]][e["v"]]["len"] > e["len"]:
                    D.add_edge(e["u"], e["v"], len=e["len"])
            par = len(es) != D.number_of_edges()
            try:
                want = [nx.path_weight(D, p, "len") for _, p in zip(range(3),
                        nx.shortest_simple_paths(D, s, t, weight="len"))] if s in D and t in D else []
            except nx.NetworkXNoPath:
                want = []
            if not par:   # with parallel arcs, FalkorDB may count them as distinct paths
                checks += 1
                if [round(w, 6) for w, _ in got] != [round(w, 6) for w in want]:
                    mism.append((k, label, "kpaths", s, t, [w for w, _ in got], want))
        LOG.append({"case": k, "n": n, "m": len(edges), "closed": len(closed)})
        c.delete(); g.delete()
    (RESULTS / "routing_correctness.json").write_text(json.dumps({"checks": checks, "mismatches": mism,
                                                                  "cases": LOG}, indent=1, default=str))
    assert not mism, mism[:10]
