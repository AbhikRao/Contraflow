"""Contraflow v2 core: road access and routing computed in FalkorDB.

Graph model (one FalkorDB graph per scenario, scenarios made with GRAPH.COPY):
  (:J {id, x, y})                                junction (OSM node)
  (:J)-[:ROAD {eid, len, highway, name}]->(:J)   one directed drivable segment
  (:Zone {id, name})-[:AT]->(:J)                 modeled neighborhood anchor
  (:Help {id, name})-[:AT]->(:J)                 designated assistance point
  (:J)-[:ROAD {eid:'hub:<help>', len:0}]->(:HUB) virtual edge: assistance point -> hub

Access of a zone = SPpaths(zone junction -> HUB). Empty result = no road access.
Because hub edges are zero-length, the path weight is the distance to the
nearest reachable assistance point. Closures delete ROAD edges in a clone.
"""

from __future__ import annotations

import time

import networkx as nx

BATCH = 2000
HUB = "__HUB__"


def load(g, nodes: list[dict], edges: list[dict]) -> float:
    """nodes: {id,...}; edges: {eid,u,v,len,...}. Returns seconds."""
    t0 = time.perf_counter()
    g.query("CREATE INDEX FOR (j:J) ON (j.id)")
    g.query("CREATE INDEX FOR ()-[e:ROAD]-() ON (e.eid)")
    for i in range(0, len(nodes), BATCH):
        g.query("UNWIND $rows AS r CREATE (j:J) SET j = r", {"rows": nodes[i:i + BATCH]})
    for i in range(0, len(edges), BATCH):
        rows = [{"u": e["u"], "v": e["v"], "p": {k: v for k, v in e.items() if k not in ("u", "v")}}
                for e in edges[i:i + BATCH]]
        g.query("UNWIND $rows AS r MATCH (a:J {id: r.u}), (b:J {id: r.v}) "
                "CREATE (a)-[e:ROAD]->(b) SET e = r.p", {"rows": rows})
    return time.perf_counter() - t0


def add_help_points(g, help_nodes: list[str]):
    g.query("MERGE (:J {id: $hub})", {"hub": HUB})
    g.query("UNWIND $hs AS h MATCH (a:J {id: h}), (z:J {id: $hub}) "
            "CREATE (a)-[:ROAD {eid: 'hub:' + h, len: 0.0, virtual: true}]->(z)",
            {"hs": help_nodes, "hub": HUB})


def route(g, src: str, dst: str, k: int = 1, timeout: int | None = None) -> list[tuple[float, list[str]]]:
    """Up to k shortest directed routes by length (meters). [] if unreachable."""
    res = g.query("MATCH (a:J {id: $s}), (b:J {id: $t}) "
                  "CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['ROAD'], "
                  "weightProp: 'len', pathCount: $k}) YIELD path, pathWeight "
                  "RETURN pathWeight, [n IN nodes(path) | n.id]",
                  {"s": src, "t": dst, "k": k}, timeout=timeout).result_set
    return [(float(w), ids) for w, ids in res]


def access(g, zone_nodes: list[str]) -> dict[str, float | None]:
    """Distance from each zone junction to the nearest assistance point (None = cut off)."""
    out = {}
    for z in zone_nodes:
        r = route(g, z, HUB)
        out[z] = r[0][0] if r else None
    return out


def clone(db, src_name: str, dst_name: str):
    try:
        db.select_graph(dst_name).delete()
    except Exception:
        pass
    db.connection.execute_command("GRAPH.COPY", src_name, dst_name)
    return db.select_graph(dst_name)


def close_roads(g, eids: list[str]) -> int:
    return g.query("UNWIND $ids AS i MATCH ()-[e:ROAD {eid: i}]->() DELETE e RETURN count(*)",
                   {"ids": eids}).result_set[0][0]


# ------------------------------------------------------------------ reference

def nx_graph(edges: list[dict], help_nodes: list[str] | None = None) -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    for e in edges:
        G.add_edge(e["u"], e["v"], key=e["eid"], len=e["len"])
    for h in help_nodes or []:
        G.add_edge(h, HUB, key=f"hub:{h}", len=0.0)
    return G


def nx_dist(G, s, t):
    try:
        return nx.dijkstra_path_length(G, s, t, weight=lambda u, v, d: min(x["len"] for x in d.values()))
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None
