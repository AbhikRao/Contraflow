"""Contraflow v2 on the real Adyar OSM network: access, closures, interventions.

All access numbers come from FalkorDB (algo.SPpaths on GRAPH.COPY clones) and are
cross-checked against NetworkX on the identical edge set. Nothing is hand-tuned:
  * assistance points come from two rules declared before running (A, B below);
  * zones are all named OSM places in the bbox;
  * river crossings are every drive segment whose geometry intersects the OSM Adyar River line;
  * results are reported for EVERY closure candidate, not a chosen subset.
"""
import json
import re
import statistics as st
import sys
import time
from collections import defaultdict

import osmnx as ox
from falkordb import FalkorDB
from shapely.geometry import LineString, shape

sys.path.insert(0, ".")
from feasibility.routelib import HUB, access, add_help_points, clone, close_roads, load, nx_dist, nx_graph

db = FalkorDB(port=6379)
timings = defaultdict(list)


def timed(key, fn, *a, **k):
    t0 = time.perf_counter()
    r = fn(*a, **k)
    timings[key].append(time.perf_counter() - t0)
    return r


# ------------------------------------------------------------------ real data
G = ox.load_graphml("data/osm/adyar_drive.graphml")
pois = json.load(open("data/osm/adyar_pois.json"))
river = shape(json.load(open("data/osm/adyar_river.json"))["geometry"])


def s(x):
    return ", ".join(map(str, x)) if isinstance(x, list) else ("" if x is None or str(x) == "nan" else str(x))


nodes = [{"id": str(n), "x": d["x"], "y": d["y"]} for n, d in G.nodes(data=True)]
edges, geom = [], {}
for u, v, k, d in G.edges(keys=True, data=True):
    eid = f"{u}_{v}_{k}"
    edges.append({"eid": eid, "u": str(u), "v": str(v), "len": round(float(d["length"]), 2),
                  "highway": s(d.get("highway")), "name": s(d.get("name")), "bridge": s(d.get("bridge")),
                  "oneway": bool(d.get("oneway"))})
    geom[eid] = d.get("geometry") or LineString([(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])])
E = {e["eid"]: e for e in edges}

SPECIALTY = re.compile(r"eye|skin|fertil|ortho|dental|ent\b|nethralaya|urolog|renal|cancer|polyclin|diabet|derma|"
                       r"physio|ivf|maternity|child|kidney|heart|scan|lab|research", re.I)
PUBLIC = re.compile(r"government|corporation|esi\b|primary health|urban health", re.I)
RULES = {
    "A_general_hospitals": [h for h in pois["hospitals"] if not SPECIALTY.search(h["name"])],
    "B_public_hospitals": [h for h in pois["hospitals"] if PUBLIC.search(h["name"])],
}
zones_raw = list({p["name"]: p for p in pois["places"]}.values())


def snap(items):
    return [{**i, "node": str(min(G.nodes, key=lambda n: ox.distance.great_circle(
        i["lat"], i["lon"], G.nodes[n]["y"], G.nodes[n]["x"])))} for i in items]


zones = snap(zones_raw)
zone_nodes = [z["node"] for z in zones]
zname = {z["node"]: z["name"] for z in zones}


def components(eids):
    """Group directed segments into physical structures (undirected connectivity)."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x
    for i in eids:
        parent[find(E[i]["u"])] = find(E[i]["v"])
    groups = defaultdict(list)
    for i in eids:
        groups[find(E[i]["u"])].append(i)
    return list(groups.values())


crossings = components([e["eid"] for e in edges if geom[e["eid"]].intersects(river)])
crossing_names = [" / ".join(sorted({E[i]["name"] or "(unnamed)" for i in grp}))[:70] for grp in crossings]


def run_rule(rule, help_items):
    helps = snap(help_items)
    help_nodes = sorted({h["node"] for h in helps})
    try:
        db.select_graph("adyar_base").delete()
    except Exception:
        pass
    base = db.select_graph("adyar_base")
    import_s = load(base, nodes, edges)
    add_help_points(base, help_nodes)

    def evaluate(g, edge_set):
        acc = timed("access_all_zones", access, g, zone_nodes)
        NX = nx_graph(edge_set, help_nodes)
        mism = sum(1 for z in zone_nodes if not (
            (acc[z] is None and nx_dist(NX, z, HUB) is None) or
            (acc[z] is not None and nx_dist(NX, z, HUB) is not None and abs(acc[z] - nx_dist(NX, z, HUB)) < 1e-3)))
        return {"acc": acc, "cut_off": sorted(zname[z] for z in zone_nodes if acc[z] is None), "nx_mismatches": mism}

    def scenario(closed, reversed_eids=()):
        c = timed("graph_copy", clone, db, "adyar_base", "adyar_sc")
        t0 = time.perf_counter()
        close_roads(c, list(closed) + list(reversed_eids))
        rev = [{"eid": f"rev:{i}", "u": E[i]["v"], "v": E[i]["u"], "len": E[i]["len"], "contraflow": True}
               for i in reversed_eids]
        if rev:
            c.query("UNWIND $rows AS r MATCH (a:J {id: r.u}), (b:J {id: r.v}) "
                    "CREATE (a)-[:ROAD {eid: r.eid, len: r.len, contraflow: true}]->(b)", {"rows": rev})
        timings["mutate"].append(time.perf_counter() - t0)
        gone = set(closed) | set(reversed_eids)
        r = evaluate(c, [e for e in edges if e["eid"] not in gone] + rev)
        c.delete()
        return r

    def impact(r, ref):
        both = [z for z in zone_nodes if r["acc"][z] is not None and ref["acc"][z] is not None]
        inc = sorted(((r["acc"][z] - ref["acc"][z]), zname[z]) for z in both)
        return {"newly_cut_off": [z for z in r["cut_off"] if z not in ref["cut_off"]],
                "restored": [z for z in ref["cut_off"] if z not in r["cut_off"]],
                "added_m": round(sum(x for x, _ in inc), 1),
                "worst": [round(inc[-1][0], 1), inc[-1][1]] if inc else None,
                "nx_mismatches": r["nx_mismatches"]}

    base_r = evaluate(base, edges)
    out = {"assistance_points": [h["name"] for h in helps], "import_s": round(import_s, 3),
           "baseline_cut_off": base_r["cut_off"], "baseline_nx_mismatches": base_r["nx_mismatches"]}

    # 1. every single river crossing closed, one at a time
    out["single_crossing_closures"] = [{"crossing": n, "segments": len(g), **impact(scenario(g), base_r)}
                                       for n, g in zip(crossing_names, crossings)]
    # 2. flood scenario: all river crossings closed
    all_closed = [i for g in crossings for i in g]
    flood = scenario(all_closed)
    out["all_crossings_closed"] = impact(flood, base_r)

    # 3a. intervention type 1: reopen exactly one crossing (recovery priority)
    out["reopen_one_crossing"] = []
    for n, g in zip(crossing_names, crossings):
        r = scenario([i for i in all_closed if i not in set(g)])
        out["reopen_one_crossing"].append({"reopen": n, "vs_flood": impact(r, flood), "vs_baseline": impact(r, base_r)})

    # 3b. intervention type 2: contraflow one one-way segment. Candidates are generated by FalkorDB:
    # for each zone hurt by the flood, route ignoring direction (relDirection 'both') in the flood
    # graph; segments traversed against their direction are candidates.
    fc = clone(db, "adyar_base", "adyar_flood")
    close_roads(fc, all_closed)
    hurt = [z for z in zone_nodes if (flood["acc"][z] is None) != (base_r["acc"][z] is None)
            or (flood["acc"][z] or 0) - (base_r["acc"][z] or 0) > 300]
    cands = set()
    for z in hurt:
        res = fc.query("MATCH (a:J {id: $z}), (h:J {id: $hub}) CALL algo.SPpaths({sourceNode: a, targetNode: h, "
                       "relTypes: ['ROAD'], weightProp: 'len', relDirection: 'both'}) YIELD path "
                       "RETURN [n IN nodes(path) | n.id], [r IN relationships(path) | [startNode(r).id, r.eid]]",
                       {"z": z, "hub": HUB}).result_set
        if res:
            ids, rels = res[0]
            for i, (start, eid) in enumerate(rels):
                if start != ids[i] and eid in E and E[eid]["oneway"]:
                    cands.add(eid)
    fc.delete()
    out["contraflow_candidates"] = len(cands)
    cf = []
    for eid in sorted(cands):
        r = scenario(all_closed, [eid])
        cf.append({"reverse_segment": eid, "road": E[eid]["name"] or "(unnamed)", "len_m": E[eid]["len"],
                   "vs_flood": impact(r, flood)})
    cf.sort(key=lambda x: (len(x["vs_flood"]["restored"]), -x["vs_flood"]["added_m"]), reverse=True)
    out["contraflow_one_segment"] = cf
    return out


RESULT = {"network": {"junctions": len(nodes), "directed_segments": len(edges),
                      "oneway_segments": sum(e["oneway"] for e in edges),
                      "river_crossings": crossing_names, "zones": len(zones)},
          "rules": {}}
for rule, items in RULES.items():
    RESULT["rules"][rule] = run_rule(rule, items)
RESULT["timings_ms"] = {k: {"n": len(v), "median": round(st.median(v) * 1000, 2),
                            "p95": round(sorted(v)[int(0.95 * (len(v) - 1))] * 1000, 2),
                            "min": round(min(v) * 1000, 2), "max": round(max(v) * 1000, 2)} for k, v in timings.items()}
json.dump(RESULT, open("results/osm_scenarios.json", "w"), indent=1, default=str)
print(json.dumps(RESULT["network"], indent=1))
for rule, o in RESULT["rules"].items():
    print(f"\n=== {rule}: {len(o['assistance_points'])} pts {o['assistance_points'] if len(o['assistance_points']) < 6 else ''}")
    print(" baseline cut off:", o["baseline_cut_off"], "nxmis", o["baseline_nx_mismatches"])
    for c in o["single_crossing_closures"]:
        print(f"  close {c['crossing'][:55]:55s} cut={c['newly_cut_off']} +{c['added_m']:.0f} m worst={c['worst']} nxmis={c['nx_mismatches']}")
    a = o["all_crossings_closed"]
    print(f"  ALL CLOSED: cut={a['newly_cut_off']} +{a['added_m']:.0f} m worst={a['worst']} nxmis={a['nx_mismatches']}")
    for r in o["reopen_one_crossing"]:
        v = r["vs_flood"]
        print(f"  reopen {r['reopen'][:50]:50s} restored={v['restored']} saved={-v['added_m']:.0f} m nxmis={v['nx_mismatches']}")
    print("  contraflow candidates:", o["contraflow_candidates"])
    for r in o["contraflow_one_segment"][:5]:
        v = r["vs_flood"]
        print(f"  reverse {r['road'][:40]:40s} {r['len_m']:6.0f} m restored={v['restored']} saved={-v['added_m']:.0f} m nxmis={v['nx_mismatches']}")
print(json.dumps(RESULT["timings_ms"], indent=1))
