"""Contraflow feasibility core: load a capacitated road graph into FalkorDB, run
FalkorDB's algo.maxFlow, reconstruct the residual network and min cut inside
FalkorDB, and verify the result independently.

Graph model (one FalkorDB graph per scenario):
  (:J {id})                          junction / OSM node (or a split node)
  (:J)-[:ROAD {eid, cap, ...}]->(:J) one directed, drivable road segment
  (:J)-[:RES]->(:J)                  temporary residual arcs (cut reconstruction)
  (:SS)                              temporary super-source for residual BFS

FalkorDB's maxFlow rejects multi-edges (same type, same direction, same node
pair). We never merge parallel physical roads: `normalize_parallel` inserts a
split junction into every duplicate so each physical segment keeps its own
capacity and identity. Flow-wise this is exactly equivalent to the original
multigraph (the split node has one in-arc and one out-arc of equal capacity).
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import networkx as nx

BATCH = 2000
TOL = 1e-6


@dataclass
class Edge:
    eid: str
    u: str
    v: str
    cap: float
    attrs: dict = field(default_factory=dict)


def normalize_parallel(edges: list[Edge]) -> tuple[list[Edge], list[dict]]:
    """Split duplicate (u, v) arcs with a virtual junction. Returns new edges and a log."""
    seen: dict[tuple[str, str], int] = defaultdict(int)
    out, log = [], []
    for e in edges:
        k = (e.u, e.v)
        seen[k] += 1
        if seen[k] == 1:
            out.append(e)
            continue
        mid = f"split:{e.eid}"
        out.append(Edge(e.eid, e.u, mid, e.cap, {**e.attrs, "split": "in"}))
        out.append(Edge(f"{e.eid}#out", mid, e.v, e.cap, {**e.attrs, "split": "out", "of": e.eid}))
        log.append({"eid": e.eid, "u": e.u, "v": e.v, "cap": e.cap, "split_node": mid})
    return out, log


# ---------------------------------------------------------------- FalkorDB I/O

def load_graph(g, edges: list[Edge], node_props: dict[str, dict] | None = None) -> float:
    """Create junctions and ROAD edges. Returns seconds taken."""
    t0 = time.perf_counter()
    nodes = sorted({e.u for e in edges} | {e.v for e in edges})
    g.query("CREATE INDEX FOR (j:J) ON (j.id)")
    g.query("CREATE INDEX FOR ()-[e:ROAD]-() ON (e.eid)")
    for i in range(0, len(nodes), BATCH):
        rows = [{"id": n, **((node_props or {}).get(n, {}))} for n in nodes[i:i + BATCH]]
        g.query("UNWIND $rows AS r CREATE (j:J) SET j = r", {"rows": rows})
    for i in range(0, len(edges), BATCH):
        rows = [{"u": e.u, "v": e.v, "p": {"eid": e.eid, "cap": float(e.cap), **e.attrs}}
                for e in edges[i:i + BATCH]]
        g.query("UNWIND $rows AS r MATCH (a:J {id: r.u}), (b:J {id: r.v}) "
                "CREATE (a)-[e:ROAD]->(b) SET e = r.p", {"rows": rows})
    return time.perf_counter() - t0


def falkor_maxflow(g, sources: list[str], sinks: list[str]) -> tuple[float, dict[str, float], float]:
    """Run algo.maxFlow. Returns (value, {eid: flow} for flow-carrying edges, seconds)."""
    q = ("MATCH (s:J) WHERE s.id IN $src WITH collect(s) AS S "
         "MATCH (t:J) WHERE t.id IN $dst WITH S, collect(t) AS T "
         "CALL algo.maxFlow({sourceNodes: S, targetNodes: T, capacityProperty: 'cap', "
         "relationshipTypes: ['ROAD']}) YIELD edges, edgeFlows, maxFlow "
         "RETURN maxFlow, [e IN edges | e.eid], edgeFlows")
    t0 = time.perf_counter()
    res = g.query(q, {"src": sources, "dst": sinks}).result_set
    dt = time.perf_counter() - t0
    value, eids, flows = res[0]
    return float(value), dict(zip(eids, map(float, flows))), dt


def falkor_mincut(g, sources: list[str], flows: dict[str, float]) -> dict:
    """Residual graph + source-side BFS + cut, computed inside FalkorDB.

    For each ROAD u->v with capacity c and flow f:
      forward residual  u->v  if c - f > 0
      backward residual v->u  if f > 0   (cancelling flow, NOT a drivable road)
    Source side S = nodes reachable from a temporary super-source over RES.
    Cut = ROAD edges u->v with u in S, v not in S.
    """
    t0 = time.perf_counter()
    g.query("MATCH ()-[e:ROAD]->() SET e.flow = 0.0")
    items = list(flows.items())
    for i in range(0, len(items), BATCH):
        g.query("UNWIND $rows AS r MATCH ()-[e:ROAD {eid: r[0]}]->() SET e.flow = r[1]",
                {"rows": items[i:i + BATCH]})
    g.query("MATCH (a)-[e:ROAD]->(b) WHERE e.cap - e.flow > $tol CREATE (a)-[:RES]->(b)", {"tol": TOL})
    g.query("MATCH (a)-[e:ROAD]->(b) WHERE e.flow > $tol CREATE (b)-[:RES]->(a)", {"tol": TOL})
    g.query("CREATE (:SS {id: '__ss__'})")
    g.query("MATCH (ss:SS), (s:J) WHERE s.id IN $src CREATE (ss)-[:RES]->(s)", {"src": sources})
    reach = g.query("MATCH (ss:SS) CALL algo.BFS(ss, -1, 'RES') YIELD nodes "
                    "RETURN [n IN nodes | n.id]").result_set[0][0]
    side = set(reach) | set(sources)
    g.query("MATCH (j:J) SET j.side = CASE WHEN j.id IN $S THEN 'S' ELSE 'T' END", {"S": list(side)})
    cut = g.query("MATCH (a:J {side: 'S'})-[e:ROAD]->(b:J {side: 'T'}) "
                  "RETURN e.eid, a.id, b.id, e.cap, e.flow").result_set
    back = g.query("MATCH (a:J {side: 'T'})-[e:ROAD]->(b:J {side: 'S'}) "
                   "RETURN max(e.flow)").result_set[0][0]
    g.query("MATCH ()-[r:RES]->() DELETE r")
    g.query("MATCH (ss:SS) DELETE ss")
    return {
        "source_side": side,
        "cut": [{"eid": r[0], "u": r[1], "v": r[2], "cap": r[3], "flow": r[4]} for r in cut],
        "cut_capacity": sum(r[3] for r in cut),
        "max_back_flow": back or 0.0,
        "seconds": time.perf_counter() - t0,
    }


# --------------------------------------------------------- independent checks

def nx_maxflow(edges: list[Edge], sources: list[str], sinks: list[str]) -> float:
    """Independent NetworkX max flow on the ORIGINAL multigraph (parallel arcs summed)."""
    G = nx.DiGraph()
    for e in edges:
        if G.has_edge(e.u, e.v):
            G[e.u][e.v]["capacity"] += e.cap
        else:
            G.add_edge(e.u, e.v, capacity=e.cap)
    for s in sources:
        G.add_edge("__S__", s)          # no capacity attr = infinite in NetworkX
    for t in sinks:
        G.add_edge(t, "__T__")
    if "__S__" not in G or "__T__" not in G:
        return 0.0
    return float(nx.maximum_flow_value(G, "__S__", "__T__"))


def python_mincut(edges: list[Edge], sources: list[str], flows: dict[str, float]) -> float:
    """Second, independent residual reconstruction in Python. Returns cut capacity."""
    adj = defaultdict(list)
    for e in edges:
        f = flows.get(e.eid, 0.0)
        if e.cap - f > TOL:
            adj[e.u].append(e.v)
        if f > TOL:
            adj[e.v].append(e.u)
    S, dq = set(sources), deque(sources)
    while dq:
        x = dq.popleft()
        for y in adj[x]:
            if y not in S:
                S.add(y)
                dq.append(y)
    return sum(e.cap for e in edges if e.u in S and e.v not in S)


def verify(edges: list[Edge], sources, sinks, value, flows, cut) -> dict:
    """All certificate checks. Edges here are the normalized (loaded) edges."""
    cap = {e.eid: e.cap for e in edges}
    over = [k for k, f in flows.items() if f > cap[k] * (1 + 1e-9) + TOL or f < -TOL]
    bal = defaultdict(float)
    for e in edges:
        f = flows.get(e.eid, 0.0)
        bal[e.u] -= f
        bal[e.v] += f
    terminals = set(sources) | set(sinks)
    worst_cons = max([abs(b) for n, b in bal.items() if n not in terminals] or [0.0])
    net_out = -sum(bal[s] for s in sources)
    tol = max(TOL, 1e-9 * max(1.0, value))
    return {
        "capacity_ok": not over,
        "conservation_ok": worst_cons <= tol * 10,
        "worst_conservation_error": worst_cons,
        "source_outflow_matches_value": math.isclose(net_out, value, rel_tol=1e-9, abs_tol=tol * 10),
        "cut_equals_flow": math.isclose(cut["cut_capacity"], value, rel_tol=1e-9, abs_tol=tol * 10),
        "cut_edges_saturated": all(abs(c["cap"] - c["flow"]) <= tol * 10 for c in cut["cut"]),
        "no_back_flow_across_cut": cut["max_back_flow"] <= tol * 10,
        "python_cut_equals_flow": math.isclose(python_mincut(edges, sources, flows), value,
                                               rel_tol=1e-9, abs_tol=tol * 10),
    }
