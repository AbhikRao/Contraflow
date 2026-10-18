"""Builds the Contraflow network model from the persisted OSM extract.

Everything here is deterministic and derived from files in data/osm/ (ODbL, (c) OpenStreetMap
contributors). Nothing in this module talks to FalkorDB; graphdb.py loads the model.

Model decisions (documented in docs/MODEL.md):
  * Directed drive segments come straight from OSMnx (two-way road = two arcs).
  * Zone anchors are named OSM places snapped to the nearest junction inside the largest
    strongly connected component. Anchors further than MAX_SNAP_M from any such junction lie
    outside the modeled extract and are excluded (with the reason recorded).
  * River crossings are drive segments whose geometry intersects the OSM "Adyar River" line.
    Segments are grouped into carriageways (undirected connectivity) and carriageways into
    physical bridges (by the "... Bridge" name token).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OSM = ROOT / "data" / "osm"
PROCESSED = ROOT / "data" / "processed" / "network.json"
MAX_SNAP_M = 600.0

SPECIALTY = re.compile(r"eye|skin|fertil|ortho|dental|ent\b|nethralaya|urolog|renal|cancer|polyclin|diabet|derma|"
                       r"physio|ivf|maternity|child|kidney|heart|scan|lab|research", re.I)
PUBLIC = re.compile(r"government|corporation|esi\b|primary health|urban health", re.I)
RULES = {
    "B": {"id": "B", "label": "Public hospitals",
          "definition": "OSM amenity=hospital whose name contains Government / Corporation / ESI / "
                        "Primary Health / Urban Health."},
    "A": {"id": "A", "label": "General hospitals",
          "definition": "OSM amenity=hospital excluding names with specialty keywords "
                        "(eye, skin, fertility, ortho, dental, ENT, cancer, maternity, research, ...)."},
}


def haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _s(x) -> str:
    if isinstance(x, list):
        return ", ".join(dict.fromkeys(map(str, x)))
    return "" if x is None or str(x) == "nan" else str(x)


@dataclass
class Network:
    junctions: list[dict]
    segments: list[dict]                 # eid, u, v, len, name, highway, oneway, lanes, crossing, carriageway, coords
    zones: list[dict]                    # id, name, lat, lon, node, snap_m
    excluded_zones: list[dict]
    help_points: list[dict]              # id, name, lat, lon, node, snap_m, rules
    crossings: list[dict]                # id, name, carriageways[{id, direction, eids, lanes, oneway}]
    river: list[list[float]]
    meta: dict = field(default_factory=dict)

    def seg(self, eid: str) -> dict:
        return self._by_eid[eid]

    def carriageway(self, cid: str) -> dict:
        return self._cw[cid]

    def crossing(self, xid: str) -> dict:
        return self._x[xid]

    def index(self):
        self._by_eid = {s["eid"]: s for s in self.segments}
        self._cw = {c["id"]: {**c, "crossing": x["id"], "crossing_name": x["name"]}
                    for x in self.crossings for c in x["carriageways"]}
        self._x = {x["id"]: x for x in self.crossings}
        return self


def _snap(G, nodes, lat, lon):
    best, bd = None, float("inf")
    for n in nodes:
        d = haversine(lat, lon, G.nodes[n]["y"], G.nodes[n]["x"])
        if d < bd:
            best, bd = n, d
    return str(best), round(bd, 1)


@lru_cache(maxsize=1)
def build() -> Network:
    """Load the processed model (fast, no GIS dependencies); build it from raw OSM files if absent."""
    if PROCESSED.exists():
        d = json.loads(PROCESSED.read_text())
        return Network(**d).index()
    net = build_from_osm()
    PROCESSED.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED.write_text(json.dumps(net.__dict__, separators=(",", ":")))
    return net.index()


def build_from_osm() -> Network:
    import networkx as nx
    import osmnx as ox
    from shapely.geometry import LineString, shape

    G = ox.load_graphml(OSM / "adyar_drive.graphml")
    pois = json.loads((OSM / "adyar_pois.json").read_text())
    river_geom = shape(json.loads((OSM / "adyar_river.json").read_text())["geometry"])
    meta = json.loads((OSM / "adyar_drive.meta.json").read_text())

    junctions = [{"id": str(n), "x": round(d["x"], 7), "y": round(d["y"], 7)} for n, d in G.nodes(data=True)]
    segments, geoms = [], {}
    for u, v, k, d in G.edges(keys=True, data=True):
        eid = f"{u}_{v}_{k}"
        g = d.get("geometry") or LineString([(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])])
        geoms[eid] = g
        hw = d.get("highway")
        segments.append({
            "eid": eid, "u": str(u), "v": str(v), "len": round(float(d["length"]), 2),
            "name": _s(d.get("name")), "highway": _s(hw[0] if isinstance(hw, list) else hw),
            "oneway": bool(d.get("oneway")), "lanes": _s(d.get("lanes")),
            "crossing": "", "carriageway": "",
            "coords": [[round(x, 6), round(y, 6)] for x, y in g.coords],
        })
    by_eid = {s["eid"]: s for s in segments}

    # ---- crossings: segments intersecting the river, grouped into carriageways and bridges
    xs = [s for s in segments if geoms[s["eid"]].intersects(river_geom)]
    parent: dict[str, str] = {}

    def find(a):
        while parent.setdefault(a, a) != a:
            a = parent[a]
        return a
    for s in xs:
        parent[find(s["u"])] = find(s["v"])
    groups: dict[str, list[dict]] = {}
    for s in xs:
        groups.setdefault(find(s["u"]), []).append(s)
    bridges: dict[str, dict] = {}
    for segs in groups.values():
        names = {n.strip() for s in segs for n in s["name"].split(",")}
        bname = next((n for n in sorted(names) if n.endswith("Bridge")), sorted(names)[0] if names else "Unnamed crossing")
        xid = slug(bname)
        b = bridges.setdefault(xid, {"id": xid, "name": bname, "roads": sorted(names - {bname}), "carriageways": []})
        dirs = set()
        for s in segs:
            dy = G.nodes[int(s["v"])]["y"] - G.nodes[int(s["u"])]["y"]
            dirs.add("northbound" if dy > 0 else "southbound")
        direction = "two-way" if len(dirs) > 1 else dirs.pop()
        cid = f"{xid}:{direction}"
        b["carriageways"].append({"id": cid, "direction": direction, "eids": sorted(s["eid"] for s in segs),
                                  "oneway": all(s["oneway"] for s in segs),
                                  "lanes": sorted({s["lanes"] for s in segs if s["lanes"]}),
                                  "len": round(max(s["len"] for s in segs), 1)})
        for s in segs:
            by_eid[s["eid"]]["crossing"] = xid
            by_eid[s["eid"]]["carriageway"] = cid
    crossings = sorted(bridges.values(), key=lambda b: b["name"])
    for b in crossings:
        b["carriageways"].sort(key=lambda c: c["direction"])
        pts = [c for cw in b["carriageways"] for e in cw["eids"] for c in by_eid[e]["coords"]]
        b["lon"] = round(sum(p[0] for p in pts) / len(pts), 6)
        b["lat"] = round(sum(p[1] for p in pts) / len(pts), 6)

    # ---- zone anchors (largest SCC only)
    scc = max(nx.strongly_connected_components(G), key=len)
    zones, excluded = [], []
    for p in {p["name"]: p for p in pois["places"]}.values():
        node, d = _snap(G, scc, p["lat"], p["lon"])
        z = {"id": slug(p["name"]), "name": p["name"], "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
             "node": node, "snap_m": d, "osm": p["osm"]}
        if d > MAX_SNAP_M:
            excluded.append({**z, "reason": f"OSM place point is {d:.0f} m from the nearest junction of the "
                                            f"modeled network (limit {MAX_SNAP_M:.0f} m): outside the extract."})
        else:
            zones.append(z)

    help_points = []
    for h in pois["hospitals"]:
        rules = [r for r, rx in (("A", None), ("B", PUBLIC)) if (rx.search(h["name"]) if rx else not SPECIALTY.search(h["name"]))]
        if not rules:
            continue
        node, d = _snap(G, scc, h["lat"], h["lon"])
        help_points.append({"id": f"h{len(help_points)}", "name": h["name"], "lat": round(h["lat"], 6),
                            "lon": round(h["lon"], 6), "node": node, "snap_m": d, "rules": rules, "osm": h["osm"]})

    river = [[round(x, 6), round(y, 6)] for x, y in
             (river_geom.coords if river_geom.geom_type == "LineString" else
              [c for part in river_geom.geoms for c in part.coords])]
    meta = {**meta, "max_snap_m": MAX_SNAP_M, "largest_scc_junctions": len(scc), "rules": RULES,
            "attribution": "Road network, places, hospitals and river: (c) OpenStreetMap contributors, ODbL 1.0"}
    return Network(junctions, segments, zones, excluded, help_points, crossings, river, meta)


def geojson(net: Network) -> dict:
    """Static map layers for the UI (served once, cached by the browser)."""
    def fc(features):
        return {"type": "FeatureCollection", "features": features}
    roads = [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": s["coords"]},
              "properties": {"eid": s["eid"], "name": s["name"], "highway": s["highway"], "oneway": s["oneway"],
                             "len": s["len"], "crossing": s["crossing"], "carriageway": s["carriageway"]}}
             for s in net.segments]
    return {
        "roads": fc(roads),
        "river": fc([{"type": "Feature", "geometry": {"type": "LineString", "coordinates": net.river},
                      "properties": {"name": "Adyar River"}}]),
        "zones": fc([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [z["lon"], z["lat"]]},
                      "properties": {k: z[k] for k in ("id", "name", "node", "snap_m")}} for z in net.zones]),
        "help": fc([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [h["lon"], h["lat"]]},
                     "properties": {"id": h["id"], "name": h["name"], "rules": ",".join(h["rules"]),
                                    "node": h["node"]}} for h in net.help_points]),
        "crossings": fc([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [x["lon"], x["lat"]]},
                          "properties": {"id": x["id"], "name": x["name"]}} for x in net.crossings]),
    }
