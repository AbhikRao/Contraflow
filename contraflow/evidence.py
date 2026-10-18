"""Graph-backed location resolution and report evidence.

Resolution never guesses silently. It uses three FalkorDB lookups:
  1. full-text over Crossing(name, roads)                -> direct name match ("Kotturpuram bridge")
  2. full-text over Zone(name), then (:Zone)-[:NEAR]->(:Crossing) ordered by distance
                                                         -> local alias ("Saidapet bridge")
  3. full-text over ROAD(name)                           -> ordinary named road ("Sardar Patel Road")
If more than one target remains plausible the result is 'ambiguous' and the caller must ask.
Reports are stored as (:Report)-[:REFERS_TO]->(:Crossing|:Zone) so evidence is queryable later.
"""

from __future__ import annotations

import re
import time
import uuid

from .graphdb import BASE, DB, Ledger
from .model import Network, haversine

STOP = {"the", "a", "an", "is", "are", "was", "has", "have", "been", "be", "at", "on", "in", "near", "of", "to",
        "and", "or", "bridge", "bridges", "road", "rd", "street", "st", "blocked", "closed", "close", "flooded",
        "flooding", "submerged", "water", "over", "under", "report", "reports", "reported", "says", "said", "it",
        "this", "that", "please", "now", "due", "for", "from", "with", "impassable", "shut", "collapsed", "debris",
        "keep", "reserve", "available", "inbound", "outbound", "emergency", "access", "vehicles", "ambulance",
        "ambulances", "reopen", "reopened", "open", "opened", "cleared", "again", "why", "does", "doesn", "not",
        "what", "which", "matter", "crossing", "crossings", "northbound", "southbound", "north", "south", "bound",
        "direction", "lane", "lanes", "we", "should", "can", "our", "only", "use", "used", "carriageway", "side",
        "towards", "toward", "traffic", "all", "both", "i", "hear", "heard", "team", "field", "unit", "says",
        "confirmed", "confirm", "there", "also", "after", "rain", "rains", "heavy", "by", "let", "s", "us", "do", "dont",
        "don", "t", "no", "contraflow", "reverse", "reversed", "unsafe", "inspection", "pending", "structural"}
ZONE_RATIO = 1.6        # nearest crossing must be this much closer than the next to resolve by proximity
ZONE_MARGIN_M = 500.0


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP and len(t) > 1]


def named_crossings(net, text: str) -> list[tuple[str, str | None]]:
    """Every crossing named explicitly in the text, in order, each with the direction written right after ITS name
    (up to the next named crossing). A direction never carries over from one named crossing to another."""
    hits = []
    for x in net.crossings:
        base = re.sub(r"\s+bridge$", "", x["name"], flags=re.I)
        for m in re.finditer(r"\b" + re.escape(base) + r"\b", text, re.I):
            hits.append((m.start(), m.end(), x["id"]))
    hits.sort()
    out, seen = [], set()
    for i, (s, e, xid) in enumerate(hits):
        if xid in seen:
            continue
        seen.add(xid)
        nxt = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        out.append((xid, direction_hint(text[e:min(nxt, e + 40)])))
    return out


def direction_hint(text: str) -> str | None:
    t = text.lower()
    north = re.search(r"\bnorth[- ]?bound\b|\bnorthward|\bgoing north\b|\bnb\b", t)
    south = re.search(r"\bsouth[- ]?bound\b|\bsouthward|\bgoing south\b|\bsb\b", t)
    if north and not south:
        return "northbound"
    if south and not north:
        return "southbound"
    return None


def _ft(q: list[str]) -> str:
    return "|".join(q)


BRIDGE_WORDS = re.compile(r"\b(bridge|crossing|crossings)\b", re.I)
ROAD_WORDS = re.compile(r"\b(road|rd|street|st|salai|avenue|main|lane|highway|flyover|nagar\s+road)\b", re.I)
MAX_PROXIMITY_M = 1500.0
SECTION_M = 500.0


def resolve_location(db: DB, net: Network, text: str, ledger: Ledger) -> dict:
    g = db.graph(BASE)
    toks = tokens(text)
    out = {"query": text, "tokens": toks, "direction_hint": direction_hint(text), "candidates": []}
    if not toks:
        return {**out, "status": "not_found", "reason": "No place or road name found in the text."}
    has_bridge, has_road = bool(BRIDGE_WORDS.search(text)), bool(ROAD_WORDS.search(text))
    order = (["crossing", "zone", "road"] if has_bridge else ["road", "crossing", "zone"] if has_road
             else ["crossing", "zone_area", "road"])
    for step in order:
        res = {"crossing": _by_crossing_name, "zone": _by_zone_proximity, "zone_area": _area_only,
               "road": _by_road_name}[step](g, net, text, toks, out, ledger)
        if res:
            return res
    return {**out, "status": "not_found",
            "reason": "No crossing, neighbourhood or road in the modeled network matched."}


def _by_crossing_name(g, net, text, toks, out, ledger):
    rows = g.q("fulltext_crossing", "CALL db.idx.fulltext.queryNodes('Crossing', $q) YIELD node, score "
               "RETURN node.id, node.name, node.roads, score ORDER BY score DESC", {"q": _ft(toks)}, ledger)
    if not rows:
        return None
    direct = [{"kind": "crossing", "id": r[0], "name": r[1], "method": "crossing_name",
               "detail": f"the report names it or the road it carries ({r[2]})", "score": round(float(r[3]), 3)} for r in rows]
    top = [c for c in direct if c["score"] >= direct[0]["score"] * 0.8]
    if len(top) == 1:
        return {**out, "candidates": top, "status": "resolved", "resolved": top[0]}
    return {**out, "candidates": top, "status": "ambiguous", "reason": "Several crossings match these words."}


def _zones(g, toks, ledger):
    zrows = g.q("fulltext_zone", "CALL db.idx.fulltext.queryNodes('Zone', $q) YIELD node, score "
                "RETURN node.id, node.name, score ORDER BY score DESC", {"q": _ft(toks)}, ledger)
    phrase = " ".join(toks)
    exact = [r for r in zrows if " ".join(tokens(r[1])) in phrase]
    return exact or zrows[:2]


def _near(g, zones, ledger):
    near = g.q("zone_near_crossings", "UNWIND $zs AS zid MATCH (z:Zone {id: zid})-[n:NEAR]->(x:Crossing) "
               "RETURN z.id, z.name, x.id, x.name, n.m ORDER BY n.m", {"zs": [z[0] for z in zones]}, ledger)
    seen, cands = set(), []
    for zid, zname, xid, xname, m in near:
        if xid not in seen:
            seen.add(xid)
            cands.append({"kind": "crossing", "id": xid, "name": xname, "method": "zone_proximity",
                          "detail": f"the modeled crossing nearest the {zname} neighbourhood, {round(m)} m away", "distance_m": round(m),
                          "via_zone": zid})
    return cands


def _by_zone_proximity(g, net, text, toks, out, ledger):
    zones = _zones(g, toks, ledger)
    if not zones:
        return None
    cands = _near(g, zones, ledger)
    out = {**out, "matched_zones": [{"id": z[0], "name": z[1]} for z in zones], "candidates": cands[:3]}
    if not cands:
        return {**out, "status": "not_found", "reason": f"No modeled river crossing within 3 km of {zones[0][1]}."}
    c0 = cands[0]
    clear = len(cands) == 1 or (cands[1]["distance_m"] >= c0["distance_m"] * ZONE_RATIO
                                and cands[1]["distance_m"] - c0["distance_m"] >= ZONE_MARGIN_M)
    if clear and c0["distance_m"] <= MAX_PROXIMITY_M:
        return {**out, "status": "resolved", "resolved": c0}
    why = (f"Two crossings are similarly close to {zones[0][1]} ({c0['distance_m']} m vs {cands[1]['distance_m']} m)."
           if not clear else f"The nearest crossing to {zones[0][1]} is {c0['distance_m']} m away; too far to assume.")
    return {**out, "status": "ambiguous", "reason": why}


def _area_only(g, net, text, toks, out, ledger):
    zones = _zones(g, toks, ledger)
    if not zones:
        return None
    return {**out, "matched_zones": [{"id": z[0], "name": z[1]} for z in zones], "candidates": _near(g, zones, ledger)[:3],
            "status": "ambiguous",
            "reason": f"The report names an area ({zones[0][1]}), not a specific road or crossing."}


def _by_road_name(g, net, text, toks, out, ledger):
    parts = re.split(r"\b(?:near|at|by|close to|next to|around|in)\b", text, maxsplit=1, flags=re.I)
    road_toks = tokens(parts[0]) or toks
    zone_toks = tokens(parts[1]) if len(parts) > 1 else toks
    rows = []
    for q, op in ((" ".join(road_toks), "fulltext_road_all_words"), (_ft(road_toks), "fulltext_road_any_word")):
        rows = g.q(op, "CALL db.idx.fulltext.queryRelationships('ROAD', $q) YIELD relationship, score "
                   "RETURN relationship.name, relationship.eid, score ORDER BY score DESC LIMIT 400", {"q": q}, ledger)
        if rows:
            break
    if not rows:
        return None
    by_name: dict[str, list[str]] = {}
    for name, eid, _ in rows:
        by_name.setdefault(name.split(",")[0].strip(), []).append(eid)   # merge OSM multi-name variants
    roads = []
    for name, eids in by_name.items():
        segs = [net.seg(e) for e in eids if e in net._by_eid]
        roads.append({"kind": "road", "id": name, "name": name, "method": "road_name", "segments": len(segs)})
    roads.sort(key=lambda r: -r["segments"])
    roads = [r for r in roads if r["segments"] >= 0.2 * roads[0]["segments"]]   # drop incidental name matches
    out = {**out, "candidates": roads[:4]}
    zones = [z for z in (_zones(g, zone_toks, ledger) if zone_toks else []) if z[1].lower() not in roads[0]["name"].lower()]
    if len(roads) == 1 and zones:
        z = next(z for z in net.zones if z["id"] == zones[0][0])
        local = [e for e in by_name[roads[0]["name"]] if e in net._by_eid and
                 min(haversine(z["lat"], z["lon"], c[1], c[0]) for c in net.seg(e)["coords"]) <= SECTION_M]
        if local:
            section = {**roads[0], "kind": "road_section", "segment_ids": sorted(local),
                       "detail": f"{len(local)} directed segments of {roads[0]['name']} within {SECTION_M:.0f} m of {z['name']}"}
            return {**out, "candidates": [section], "matched_zones": [{"id": z["id"], "name": z["name"]}],
                    "status": "resolved", "resolved": section}
    return {**out, "status": "ambiguous",
            "reason": (f"{roads[0]['name']} has {roads[0]['segments']} directed segments; the report does not say which "
                       "section. Name a nearby neighbourhood or select it on the map.") if len(roads) == 1 else
                      "Several roads match these words."}


def record_report(db: DB, text: str, targets: list[dict], status: str, ledger: Ledger) -> str:
    rid = "r_" + uuid.uuid4().hex[:10]
    g = db.graph(BASE)
    g.q("record_report", "CREATE (:Report {id: $id, text: $text, ts: $ts, status: $status, "
        "source: 'user-entered via planning agent (unverified)'})",
        {"id": rid, "text": text[:500], "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": status}, ledger)
    for t in targets:
        label = {"crossing": "Crossing", "carriageway": "Carriageway", "zone": "Zone"}.get(t["kind"])
        if label:
            g.q("link_report", f"MATCH (r:Report {{id: $rid}}), (x:{label} {{id: $tid}}) "
                "CREATE (r)-[:REFERS_TO {method: $m}]->(x)", {"rid": rid, "tid": t["id"], "m": t.get("method", "")}, ledger)
    return rid


def list_reports(db: DB, target: str | None = None, ledger: Ledger | None = None) -> list[dict]:
    g = db.graph(BASE)
    if target:
        rows = g.q("reports_for_target", "MATCH (r:Report)-[l:REFERS_TO]->(x) WHERE x.id = $t "
                   "OR (x:Carriageway AND x.id STARTS WITH $t) RETURN r.id, r.text, r.ts, r.status, r.source, "
                   "collect(x.id), collect(l.method) ORDER BY r.ts DESC LIMIT 20", {"t": target}, ledger)
    else:
        rows = g.q("reports_all", "MATCH (r:Report) OPTIONAL MATCH (r)-[l:REFERS_TO]->(x) RETURN r.id, r.text, r.ts, "
                   "r.status, r.source, collect(x.id), collect(l.method) ORDER BY r.ts DESC LIMIT 20", None, ledger)
    return [{"id": r[0], "text": r[1], "ts": r[2], "status": r[3], "source": r[4], "targets": r[5], "methods": r[6]}
            for r in rows]
