"""Scenario evaluation and intervention ranking. FalkorDB performs all routing.

Objective (stated everywhere it is shown): the sum, over neighbourhood anchors that can reach an
assistance point in the baseline, of the modeled shortest directed road distance (meters) from
the anchor to its nearest assistance point of the active rule. A zone that loses access entirely is
reported separately ("cut off"), never folded into the distance sum. This is a modeled access
metric, not travel time, people served, or evacuation benefit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .graphdb import BASE, DB, Graph, Ledger, hub
from .model import Network, RULES

EPS_M = 0.5            # distance changes below this are treated as no change
CONSTRAINT_KINDS = {
    "reserve_emergency": "Reserved for inbound emergency vehicles: removed from public-access routing, "
                         "cannot be offered as a public reopening or reversed.",
    "keep_direction": "Must keep its current direction: no contraflow reversal of its segments.",
    "no_reopen": "Must stay closed in this plan (e.g. structural inspection pending); any scenario that reopens it is "
                 "rejected.",
}

ACCESS_QUERY = """
MATCH (z:Zone)-[:ANCHORED_AT]->(a:J), (h:J {id: $hub})
CALL algo.SPpaths({sourceNode: a, targetNode: h, relTypes: ['ROAD'], weightProp: 'len'})
YIELD path, pathWeight
WITH z, pathWeight, relationships(path) AS rs
RETURN z.id, pathWeight, [r IN rs WHERE NOT coalesce(r.virtual, false) | r.eid], startNode(rs[size(rs) - 1]).id
"""

CANDIDATE_QUERY = """
MATCH (z:Zone {id: $zone})-[:ANCHORED_AT]->(a:J), (h:J {id: $hub})
CALL algo.SPpaths({sourceNode: a, targetNode: h, relTypes: ['ROAD'], weightProp: 'len', relDirection: 'both'})
YIELD path
RETURN [n IN nodes(path) | n.id], [r IN relationships(path) | [startNode(r).id, r.eid]]
"""


@dataclass
class Scenario:
    rule: str = "B"
    closed_carriageways: list[str] = field(default_factory=list)
    closed_segments: list[str] = field(default_factory=list)
    reversed_segments: list[str] = field(default_factory=list)
    constraints: list[dict] = field(default_factory=list)   # {kind, target(carriageway id)}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Scenario":
        d = d or {}
        return cls(rule=d.get("rule", "B"), closed_carriageways=list(d.get("closed_carriageways", [])),
                   closed_segments=list(d.get("closed_segments", [])),
                   reversed_segments=list(d.get("reversed_segments", [])),
                   constraints=list(d.get("constraints", [])))

    def to_dict(self) -> dict:
        return {"rule": self.rule, "closed_carriageways": self.closed_carriageways,
                "closed_segments": self.closed_segments, "reversed_segments": self.reversed_segments,
                "constraints": self.constraints}

    def key(self) -> str:
        """Order-insensitive identity of the scenario (used to reject stale proposals/confirmations)."""
        import hashlib
        import json
        canon = {"rule": self.rule, "closed_carriageways": sorted(self.closed_carriageways),
                 "closed_segments": sorted(self.closed_segments), "reversed_segments": sorted(self.reversed_segments),
                 "constraints": sorted(f"{k.get('kind')}|{k.get('target')}" for k in self.constraints)}
        return hashlib.sha1(json.dumps(canon, sort_keys=True).encode()).hexdigest()[:16]

    def with_changes(self, reopen_carriageways=(), reopen_segments=(), reverse=()) -> "Scenario":
        return Scenario(self.rule, [c for c in self.closed_carriageways if c not in set(reopen_carriageways)],
                        [s for s in self.closed_segments if s not in set(reopen_segments)],
                        self.reversed_segments + [r for r in reverse if r not in self.reversed_segments],
                        self.constraints)


class ValidationError(ValueError):
    """Rejected scenario or action. `code` is machine-readable; `detail` carries the offending ids."""

    def __init__(self, message: str, code: str = "invalid", **detail):
        super().__init__(message)
        self.code, self.detail = code, detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), **self.detail}


class Engine:
    def __init__(self, net: Network, db: DB):
        self.net, self.db = net, db
        self._baseline: dict[str, dict] = {}
        self.help_by_node: dict[str, list[dict]] = {}
        for h in net.help_points:
            self.help_by_node.setdefault(h["node"], []).append(h)

    # ------------------------------------------------------------- validation
    def validate(self, sc: Scenario):
        """Authoritative check for every scenario the engine evaluates (API, candidates, agent, proposals).
        Constraints are enforced here, so no entry point can bypass them."""
        if sc.rule not in RULES:
            raise ValidationError(f"unknown assistance-point rule {sc.rule!r}", "unknown_rule", rule=sc.rule)
        for c in sc.closed_carriageways:
            self._cw(c)
        for e in sc.closed_segments + sc.reversed_segments:
            if e not in self.net._by_eid:
                raise ValidationError(f"unknown road segment {e!r}", "unknown_segment", segment=e)
        for e in sc.reversed_segments:
            if not self.net.seg(e)["oneway"]:
                raise ValidationError(f"segment {e} is two-way; contraflow applies to one-way segments only",
                                      "contraflow_two_way", segment=e)
        closed = set(sc.closed_segments)
        for c in sc.closed_carriageways:
            closed |= set(self._cw(c)["eids"])
        both = sorted(closed & set(sc.reversed_segments))
        if both:
            raise ValidationError(f"segment {both[0]} is both closed and reversed", "closed_and_reversed",
                                  segment=both[0])
        for k in sc.constraints:
            kind, target = k.get("kind"), k.get("target", "")
            if kind not in CONSTRAINT_KINDS:
                raise ValidationError(f"unknown constraint {kind!r}", "unknown_constraint", kind=kind)
            cw = self._cw(target)
            if kind in ("keep_direction", "reserve_emergency"):
                rev = sorted(set(cw["eids"]) & set(sc.reversed_segments))
                if rev:
                    raise ValidationError(f"{kind} on {target} forbids reversing segment {rev[0]}",
                                          "constraint_violation", kind=kind, target=target, segment=rev[0],
                                          action="reverse")
            if kind == "no_reopen" and not set(cw["eids"]) <= closed:
                raise ValidationError(f"no_reopen on {target} requires that carriageway to stay closed",
                                      "constraint_violation", kind=kind, target=target, action="reopen",
                                      segment=sorted(set(cw["eids"]) - closed)[0])

    def _cw(self, cid: str) -> dict:
        try:
            return self.net.carriageway(cid)
        except KeyError:
            raise ValidationError(f"unknown carriageway {cid!r}", "unknown_carriageway", carriageway=cid) from None

    def normalize(self, sc: Scenario) -> Scenario:
        """Canonical scenario: a carriageway whose segments are all closed is a closed carriageway; segments covered
        by a closed carriageway are not listed again; lists sorted and de-duplicated. Every evaluation, candidate
        prediction, candidate application and scenario key uses this single representation."""
        cws = set(sc.closed_carriageways)
        segs = set(sc.closed_segments)
        for x in self.net.crossings:
            for c in x["carriageways"]:
                if set(c["eids"]) <= segs:
                    cws.add(c["id"])
        for c in cws:
            if c in self.net._cw:
                segs -= set(self.net.carriageway(c)["eids"])
        cons, seen = [], set()
        for k in sc.constraints:
            t = (k.get("kind"), k.get("target"))
            if t not in seen:
                seen.add(t)
                cons.append({"kind": t[0], "target": t[1]})
        return Scenario(sc.rule, sorted(cws), sorted(segs), sorted(dict.fromkeys(sc.reversed_segments)),
                        sorted(cons, key=lambda k: (k["kind"], k["target"])))

    def constrained(self, sc: Scenario, kind: str) -> set[str]:
        return {k["target"] for k in sc.constraints if k["kind"] == kind}

    def effective_closed(self, sc: Scenario) -> set[str]:
        """Segments removed from public routing: closures + emergency-reserved carriageways."""
        eids = set(sc.closed_segments)
        for c in set(sc.closed_carriageways) | self.constrained(sc, "reserve_emergency"):
            eids |= set(self._cw(c)["eids"])
        return eids

    # ------------------------------------------------------------- core access
    def _access(self, g: Graph, rule: str, ledger: Ledger) -> dict[str, dict]:
        rows = g.q("access_all_zones", ACCESS_QUERY, {"hub": hub(rule)}, ledger)
        out = {}
        for zid, w, eids, help_node in rows:
            out[zid] = {"dist": round(float(w), 1), "route": eids, "help_node": help_node,
                        "help": [h["name"] for h in self.help_by_node.get(help_node, []) if rule in h["rules"]]}
        return out

    def _mutate(self, g: Graph, sc: Scenario, ledger: Ledger):
        closed = sorted(self.effective_closed(sc) | set(sc.reversed_segments))
        if closed:
            g.q("close_segments", "UNWIND $ids AS i MATCH ()-[e:ROAD {eid: i}]->() DELETE e", {"ids": closed}, ledger)
        if sc.reversed_segments:
            rows = [{"eid": f"rev:{e}", "u": self.net.seg(e)["v"], "v": self.net.seg(e)["u"],
                     "len": self.net.seg(e)["len"], "name": self.net.seg(e)["name"]} for e in sc.reversed_segments]
            g.q("contraflow_reverse", "UNWIND $rows AS r MATCH (a:J {id: r.u}), (b:J {id: r.v}) "
                "CREATE (a)-[:ROAD {eid: r.eid, len: r.len, name: r.name, oneway: true, contraflow: true}]->(b)",
                {"rows": rows}, ledger)

    def baseline(self, rule: str, ledger: Ledger | None = None) -> dict:
        if rule not in self._baseline:
            lg = ledger or Ledger()
            self._baseline[rule] = self._access(self.db.graph(BASE), rule, lg)
        return self._baseline[rule]

    def raw_access(self, sc: Scenario, ledger: Ledger) -> dict:
        self.validate(sc)
        if not (self.effective_closed(sc) or sc.reversed_segments):
            return self._access(self.db.graph(BASE), sc.rule, ledger)
        with self.db.scenario_clone(ledger) as g:
            self._mutate(g, sc, ledger)
            return self._access(g, sc.rule, ledger)

    # ------------------------------------------------------------- evaluation
    def compare(self, ref: dict, cur: dict, rule: str) -> dict:
        base = self.baseline(rule)
        eligible = [z["id"] for z in self.net.zones if z["id"] in base]
        zones, total, cut, affected = [], 0.0, [], 0
        for zid in eligible:
            r, c = ref.get(zid), cur.get(zid)
            row = {"zone": zid, "ref": r["dist"] if r else None, "cur": c["dist"] if c else None,
                   "help": c["help"] if c else [], "help_node": c["help_node"] if c else None}
            if c is None:
                cut.append(zid)
                row["status"] = "cut_off"
            elif r is None:
                row["status"] = "restored"
            else:
                row["delta"] = round(c["dist"] - r["dist"], 1)
                if abs(row["delta"]) > EPS_M:
                    total += row["delta"]
                    affected += 1
                row["status"] = "changed" if abs(row["delta"]) > EPS_M else "same"
            zones.append(row)
        largest = max((z for z in zones if z.get("delta") is not None), key=lambda z: z["delta"], default=None)
        return {"eligible_zones": len(eligible), "total_delta_m": round(total, 1), "affected_zones": affected,
                "cut_off": cut, "largest": {"zone": largest["zone"], "delta_m": largest["delta"]}
                if largest and largest["delta"] > EPS_M else None, "zones": zones}

    def evaluate(self, sc: Scenario, ledger: Ledger | None = None) -> dict:
        ledger = ledger or Ledger()
        t0 = time.perf_counter()
        self.validate(sc)
        sc = self.normalize(sc)
        base = self.baseline(sc.rule, ledger)
        cur = self.raw_access(sc, ledger)
        cmp = self.compare(base, cur, sc.rule)
        closed = self.effective_closed(sc)
        usage = self.carriageway_usage(base)
        return {
            "scenario": sc.to_dict(), "scenario_key": sc.key(), "rule": RULES[sc.rule],
            "metrics": {k: cmp[k] for k in ("eligible_zones", "total_delta_m", "affected_zones", "cut_off", "largest")},
            "baseline_total_m": round(sum(v["dist"] for v in base.values()), 1),
            "zones": cmp["zones"],
            "routes": {z: {"baseline": base[z]["route"], "current": cur[z]["route"] if z in cur else None}
                       for z in base},
            "closed_eids": sorted(closed),
            "closure_explanations": [
                {"carriageway": c, "baseline_routes_using": usage.get(c, []),
                 "note": (f"{len(usage.get(c, []))} baseline zone route(s) to the nearest assistance point used this "
                          f"carriageway." if usage.get(c) else
                          "No baseline zone route to the nearest assistance point used this carriageway, so closing "
                          "it cannot change this metric. This says nothing about its value for other trips.")}
                for c in sc.closed_carriageways],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "ledger": ledger.summary(),
        }

    def carriageway_usage(self, access: dict) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for zid, a in access.items():
            for e in a["route"]:
                cw = self.net._by_eid.get(e, {}).get("carriageway")
                if cw and zid not in out.setdefault(cw, []):
                    out[cw].append(zid)
        return out

    # ------------------------------------------------------------- interventions
    def contraflow_candidates(self, sc: Scenario, cur: dict, ledger: Ledger, limit: int = 24) -> list[str]:
        """FalkorDB generates candidates: route each hurt zone ignoring direction; one-way
        segments traversed against their direction are candidates for reversal."""
        base = self.baseline(sc.rule)
        hurt = [z for z in base if z not in cur or cur[z]["dist"] - base[z]["dist"] > EPS_M]
        banned = self.effective_closed(sc) | set(sc.reversed_segments)
        for c in self.constrained(sc, "keep_direction") | self.constrained(sc, "reserve_emergency"):
            banned |= set(self._cw(c)["eids"])
        counts: dict[str, int] = {}
        if not hurt:
            return []
        with self.db.scenario_clone(ledger) as g:
            self._mutate(g, sc, ledger)
            for z in hurt:
                rows = g.q("candidate_routes_ignoring_direction", CANDIDATE_QUERY, {"zone": z, "hub": hub(sc.rule)},
                           ledger)
                if not rows:
                    continue
                ids, rels = rows[0]
                for i, (start, eid) in enumerate(rels):
                    if start != ids[i] and eid in self.net._by_eid and eid not in banned and self.net.seg(eid)["oneway"]:
                        counts[eid] = counts.get(eid, 0) + 1
        return [e for e, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]

    def interventions(self, sc: Scenario, ledger: Ledger | None = None, include_contraflow: bool = True) -> dict:
        ledger = ledger or Ledger()
        t0 = time.perf_counter()
        self.validate(sc)
        sc = self.normalize(sc)
        base = self.baseline(sc.rule, ledger)
        cur = self.raw_access(sc, ledger)
        now = self.compare(base, cur, sc.rule)
        results, excluded = [], []
        for item in self.candidate_items(sc, cur, ledger, include_contraflow):
            nxt, err = self.check_candidate(sc, item)
            if err:
                excluded.append({**item, "excluded_by": err})
                continue
            after = self.raw_access(nxt, ledger)
            results.append({**item, **self.candidate_effect(cur, after, item, now["total_delta_m"], base, sc.rule),
                            "base_key": sc.key(), "result_scenario": nxt.to_dict(), "result_key": nxt.key()})
        results.sort(key=rank_key)
        return {"scenario": sc.to_dict(), "scenario_key": sc.key(),
                "current": {k: now[k] for k in ("total_delta_m", "affected_zones", "cut_off")},
                "ranking": results, "excluded": excluded, "objective": OBJECTIVE,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1), "ledger": ledger.summary()}

    def apply_candidate(self, sc: Scenario, item: dict) -> Scenario:
        """The single canonical transformation for a candidate (used for prediction AND application).
        Reopening a carriageway also clears any of its segments listed individually, so overlapping
        representations cannot leave it closed."""
        sc = self.normalize(sc)
        if item["type"] == "contraflow":
            nxt = sc.with_changes(reverse=item["segments"])
        elif item["type"] == "reopen_segment":
            nxt = sc.with_changes(reopen_segments=item["segments"])
        else:
            eids = {e for c in item["carriageways"] for e in self._cw(c)["eids"]}
            nxt = sc.with_changes(reopen_carriageways=item["carriageways"], reopen_segments=eids)
        return self.normalize(nxt)

    def candidate_items(self, sc: Scenario, cur: dict, ledger: Ledger, include_contraflow: bool = True) -> list[dict]:
        """The ONLY generator of candidate operations (for ranking and for application). `sc` must be normalized.
        A candidate is an operation + target: its effect is always recomputed server-side."""
        items = []
        for c in sc.closed_carriageways:
            cw = self._cw(c)
            items.append({"id": f"reopen:{c}", "type": "reopen_carriageway", "label": f"Reopen {cw['crossing_name']} "
                          f"({cw['direction']})", "carriageways": [c], "segments": cw["eids"]})
        by_x: dict[str, list[str]] = {}
        for c in sc.closed_carriageways:
            by_x.setdefault(self._cw(c)["crossing"], []).append(c)
        for x, cs in by_x.items():
            if len(cs) > 1:
                items.append({"id": f"reopen_bridge:{x}", "type": "reopen_bridge",
                              "label": f"Reopen all of {self.net.crossing(x)['name']}", "carriageways": cs,
                              "segments": [e for c in cs for e in self._cw(c)["eids"]]})
        for e in sc.closed_segments:
            s_ = self.net.seg(e)
            items.append({"id": f"reopen_seg:{e}", "type": "reopen_segment",
                          "label": f"Reopen {s_['name'] or 'unnamed road'} segment ({s_['len']:.0f} m)",
                          "segments": [e], "carriageways": []})
        if include_contraflow:
            for e in self.contraflow_candidates(sc, cur, ledger):
                s_ = self.net.seg(e)
                items.append({"id": f"reverse:{e}", "type": "contraflow",
                              "label": f"Contraflow: reverse {s_['name'] or 'unnamed one-way road'} ({s_['len']:.0f} m)",
                              "segments": [e], "carriageways": []})
        return items

    def check_candidate(self, sc: Scenario, item: dict) -> tuple[Scenario | None, dict | None]:
        """Canonical transformation + every policy/constraint check, relative to the (normalized) current scenario."""
        nxt = self.apply_candidate(sc, item)
        # Policy: a carriageway reserved for emergency vehicles is never offered as a public reopening.
        hit = sorted({self.net.seg(e)["carriageway"] for e in item["segments"]} & self.constrained(sc, "reserve_emergency"))
        if hit and item["type"] != "contraflow":
            return None, {"code": "constraint_violation", "kind": "reserve_emergency", "target": hit[0],
                          "meaning": CONSTRAINT_KINDS["reserve_emergency"]}
        try:
            self.validate(nxt)                          # same authority as every other entry point
        except ValidationError as err:
            d = err.to_dict()
            return None, {**d, "meaning": CONSTRAINT_KINDS.get(d.get("kind"), str(err))}
        return nxt, None

    def apply_result(self, current: Scenario, candidate_id: str, base_key: str,
                     ledger: Ledger | None = None) -> dict:
        """Server-authoritative application. The client supplies only WHICH candidate (operation + target, by id) and
        the scenario identity it was ranked for. The server re-derives the eligible candidates from the CURRENT
        scenario, rebuilds the transformation, re-checks every constraint and computes the outcome itself; any
        client-supplied result or prediction is ignored."""
        ledger = ledger or Ledger()
        self.validate(current)
        cur_sc = self.normalize(current)
        if base_key != cur_sc.key():
            raise ValidationError("the scenario changed since this candidate was ranked; re-rank first",
                                  "stale_candidate", expected=base_key, actual=cur_sc.key())
        cur = self.raw_access(cur_sc, ledger)
        kind = (candidate_id or "").split(":", 1)[0]
        items = {i["id"]: i for i in self.candidate_items(cur_sc, cur, ledger, include_contraflow=(kind == "reverse"))}
        item = items.get(candidate_id)
        if item is None:
            raise ValidationError(f"{candidate_id!r} is not an eligible candidate for the current scenario",
                                  "ineligible_candidate", candidate_id=candidate_id)
        nxt, err = self.check_candidate(cur_sc, item)
        if err:
            raise ValidationError(f"{candidate_id!r} violates a current constraint: {err.get('message', err['meaning'])}",
                                  "constraint_violation", candidate_id=candidate_id,
                                  **{k: v for k, v in err.items() if k in ("kind", "target", "segment", "action")})
        base = self.baseline(cur_sc.rule, ledger)
        after = self.raw_access(nxt, ledger)
        now = self.compare(base, cur, cur_sc.rule)
        effect = self.candidate_effect(cur, after, item, now["total_delta_m"], base, cur_sc.rule)
        return {"scenario": nxt, "candidate": {**item, **effect, "base_key": cur_sc.key(), "result_key": nxt.key()},
                "ledger": ledger.summary()}

    # ------------------------------------------------------------- vulnerability explorer
    def vulnerability(self, sc: Scenario, top_links: int = 20, prominent_unused: int = 5,
                      ledger: Ledger | None = None) -> dict:
        """Bounded what-if failure analysis over an explicit candidate set (NOT an exhaustive search of the network):
        every bridge and carriageway, the `top_links` road links carrying the most zone routes in the current
        scenario, and the `prominent_unused` longest major roads no zone route uses. Each candidate is closed on its
        own GRAPH.COPY clone and access is recomputed with FalkorDB."""
        ledger = ledger or Ledger()
        t0 = time.perf_counter()
        self.validate(sc)
        sc = self.normalize(sc)
        cur = self.raw_access(sc, ledger)
        closed = self.effective_closed(sc)
        usage: dict[tuple, set] = {}
        for zid, a in cur.items():
            for e in a["route"]:
                seg = self.net._by_eid.get(e)
                if seg and not seg["crossing"]:
                    usage.setdefault(tuple(sorted((seg["u"], seg["v"]))), set()).add(zid)
        links: dict[tuple, list[str]] = {}
        for seg in self.net.segments:
            if not seg["crossing"]:
                links.setdefault(tuple(sorted((seg["u"], seg["v"]))), []).append(seg["eid"])
        cands = []
        for x in self.net.crossings:
            open_cw = [c["id"] for c in x["carriageways"] if not set(c["eids"]) <= closed]
            if len(open_cw) > 1:
                cands.append({"id": f"bridge:{x['id']}", "kind": "bridge", "label": f"{x['name']} (all directions)",
                              "carriageways": open_cw, "segments": [], "highway": "bridge", "prominent": True})
            for c in open_cw:
                cw = self._cw(c)
                cands.append({"id": f"carriageway:{c}", "kind": "carriageway", "label": f"{x['name']} ({cw['direction']})",
                              "carriageways": [c], "segments": [], "highway": "bridge", "prominent": True})
        busiest = sorted(usage.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:top_links]
        unused = sorted([k for k, eids in links.items() if k not in usage and
                         self.net.seg(eids[0])["highway"] in MAJOR and not set(eids) & closed],
                        key=lambda k: -max(self.net.seg(e)["len"] for e in links[k]))[:prominent_unused]
        for k in [b[0] for b in busiest] + unused:
            eids = [e for e in links[k] if e not in closed]
            if not eids:
                continue
            seg = self.net.seg(eids[0])
            cands.append({"id": f"link:{k[0]}-{k[1]}", "kind": "road_link",
                          "label": f"{seg['name'] or 'unnamed road'} ({seg['highway']}, {seg['len']:.0f} m)",
                          "carriageways": [], "segments": eids, "highway": seg["highway"],
                          "prominent": seg["highway"] in PROMINENT})
        base_total = self.compare(self.baseline(sc.rule), cur, sc.rule)["total_delta_m"]
        results = []
        for c in cands:
            nxt = self.normalize(Scenario(sc.rule, sc.closed_carriageways + c["carriageways"],
                                          sc.closed_segments + c["segments"], sc.reversed_segments, sc.constraints))
            try:
                self.validate(nxt)
            except ValidationError:
                continue
            after = self.raw_access(nxt, ledger)
            lost = [z for z in cur if z not in after]
            longer = {z: round(after[z]["dist"] - cur[z]["dist"], 1) for z in cur
                      if z in after and after[z]["dist"] - cur[z]["dist"] > EPS_M}
            added = round(sum(longer.values()), 1)
            users = sorted(usage.get(tuple(sorted((self.net.seg(c["segments"][0])["u"], self.net.seg(c["segments"][0])["v"]))), set())) \
                if c["segments"] else sorted({z for z, a in cur.items() for e in a["route"]
                                             if self.net._by_eid.get(e, {}).get("carriageway") in c["carriageways"]})
            if lost:
                category = "disconnects"
            elif added >= 2000:
                category = "major_detour"
            elif added > EPS_M:
                category = "detour"
            else:
                category = "redundant"
            flags = []
            if c["prominent"] and category == "redundant":
                flags.append("prominent_low_impact")
            if not c["prominent"] and (lost or added >= 1000):
                flags.append("less_obvious_high_impact")
            if users and category == "redundant":
                flags.append("alternative_of_equal_length")
            ends = {n for e in c["segments"] for n in (self.net.seg(e)["u"], self.net.seg(e)["v"])}
            touched = sorted({h["name"] for h in self.net.help_points if sc.rule in h["rules"] and h["node"] in ends})
            if touched:
                flags.append("access_to_assistance_point")    # the link reaches an assistance point's anchor junction
            results.append({**c, "zones_cut_off": lost, "added_m": added, "zones_longer": len(longer),
                            "worst": max(longer.items(), key=lambda kv: kv[1]) if longer else None,
                            "routes_using_now": users, "category": category, "flags": flags,
                            "assistance_points": touched})
        results.sort(key=lambda r: (-len(r["zones_cut_off"]), -r["added_m"], r["id"]))
        return {"scenario": sc.to_dict(), "scenario_key": sc.key(), "current_added_m": base_total,
                "candidate_set": {"bridges_and_carriageways": sum(c["kind"] != "road_link" for c in cands),
                                  "busiest_links": len(busiest), "prominent_unused_links": len(unused),
                                  "evaluated": len(results), "exhaustive": False},
                "criteria": "Ranked by zones newly cut off, then added modeled distance (connected zones). Category: "
                            "disconnects / major_detour (>= 2 km) / detour / redundant (no change on this metric).",
                "results": results, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "ledger": ledger.summary()}

    def candidate_effect(self, cur: dict, after: dict, item: dict, current_total: float, base: dict, rule: str) -> dict:
        """Classify a change relative to the current scenario. Access is compared before distance:
        a zone that loses access is never hidden inside a distance sum."""
        lost, restored, shorter, longer, net = [], [], [], [], 0.0
        for zid in base:
            a, b = cur.get(zid), after.get(zid)
            if a is not None and b is None:
                lost.append(zid)
            elif a is None and b is not None:
                restored.append(zid)
            elif a is not None and b is not None:
                d = round(a["dist"] - b["dist"], 1)
                if d > EPS_M:
                    shorter.append({"zone": zid, "saved_m": d})
                elif d < -EPS_M:
                    longer.append({"zone": zid, "added_m": -d})
                if abs(d) > EPS_M:
                    net += d
        if lost:
            effect = "loses_access"
        elif restored:
            effect = "restores_access"
        elif net > EPS_M:
            effect = "shorter"
        elif net < -EPS_M:
            effect = "longer"
        elif shorter or longer:
            effect = "mixed"
        else:
            effect = "no_change"
        uses = sorted({z for z, b in after.items() for e in b["route"]
                       if e in item["segments"] or e in {f"rev:{s}" for s in item["segments"]}})
        after_cmp = self.compare(base, after, rule)
        share = net / current_total if (current_total > EPS_M and not lost and net > EPS_M) else None
        return {"effect": effect, "recovered_m": round(net, 1), "zones_lost": lost, "zones_restored": restored,
                "zones_improved": sorted(shorter, key=lambda x: -x["saved_m"]),
                "zones_worsened": sorted(longer, key=lambda x: -x["added_m"]),
                "share_of_disruption": round(share, 3) if share is not None else None,
                "after_total_delta_m": after_cmp["total_delta_m"], "after_cut_off": after_cmp["cut_off"],
                "routes_using_it": uses, "why": _why(effect, net, lost, restored, shorter, longer, uses)}


    # ------------------------------------------------------------- recommendation robustness
    def robustness(self, sc: Scenario, ledger: Ledger | None = None, max_variations: int = 5) -> dict:
        """Does the recommended repair stay best when ONE modeled assumption changes? A bounded, deterministic set of
        single-assumption variations is derived from the scenario and its winner (never a fixed table); each is re-ranked
        with the same `interventions` path (graph copies, canonical constraint checks). Not exhaustive, not a probability."""
        ledger = ledger or Ledger()
        t0 = time.perf_counter()
        self.validate(sc)
        sc = self.normalize(sc)
        orig = self.interventions(sc, ledger)
        win = _best(orig["ranking"])
        def run(v):
            try:
                self.validate(v["scenario"])
            except ValidationError as e:
                return {**_vhead(v), "rejected": e.to_dict()}
            r = self.interventions(v["scenario"], ledger) if v.get("ranking") is None else v["ranking"]
            return {**_vhead(v), **self._judge(win, orig, r, v)}

        # sequential on purpose: FalkorDB serialises GRAPH.COPY, so a thread pool measured only ~7% faster
        variations = [run(v) for v in self._variations(sc, win, orig, ledger)[:max_variations]]
        held = [v for v in variations if v.get("original_still_best")]
        tested = [v for v in variations if "rejected" not in v]
        return {"scenario": sc.to_dict(), "scenario_key": sc.key(),
                "original": None if not win else {**_brief(win, orig), "routes_using_it": len(win["routes_using_it"])},
                "current": orig["current"], "variations": variations,
                "summary": (f"the original recommendation stays best in {len(held)} of {len(tested)} tested variation(s)"
                            if win else "no repair improves access in the scenario itself"),
                "method": ("Each variation changes exactly one modeled assumption and is re-ranked from scratch on its own "
                           "FalkorDB graph copies with the same objective and constraint checks. Only the variations listed "
                           "were tested: this is a deterministic comparison, not exhaustive and not a probability."),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1), "ledger": ledger.summary()}

    def _variations(self, sc: Scenario, win: dict | None, orig: dict, ledger: Ledger) -> list[dict]:
        rule_names = {"A": "general hospitals (rule A)", "B": "public hospitals (rule B)"}
        other = "A" if sc.rule == "B" else "B"
        out = [{"id": f"rule:{other}", "kind": "assistance_rule",
                "changed": f"assistance points: {rule_names[other]} instead of {rule_names[sc.rule]}",
                "scenario": Scenario(other, sc.closed_carriageways, sc.closed_segments, sc.reversed_segments, sc.constraints)}]
        # closure direction: the crossing the winner reopens (else the first closed one), each direction on its own
        xs = [self._cw(c)["crossing"] for c in (win or {}).get("carriageways", [])] + \
             [self._cw(c)["crossing"] for c in sc.closed_carriageways]
        x = next((x for x in xs if len(self.net.crossing(x)["carriageways"]) > 1), None)
        if x:
            all_cw = [c["id"] for c in self.net.crossing(x)["carriageways"]]
            closed_here = [c for c in all_cw if c in sc.closed_carriageways]
            others = [c for c in sc.closed_carriageways if c not in all_cw]
            if len(closed_here) == len(all_cw):
                for keep in closed_here:
                    d = self._cw(keep)["direction"]
                    out.append({"id": f"direction:{keep}", "kind": "closure_direction",
                                "changed": f"only {self.net.crossing(x)['name']} {d} is closed (other direction open)",
                                # constraints kept as they are: a variation that breaks one is reported as rejected
                                "scenario": Scenario(sc.rule, others + [keep], sc.closed_segments, sc.reversed_segments,
                                                     sc.constraints)})
            else:
                out.append({"id": f"direction:{x}:all", "kind": "closure_direction",
                            "changed": f"{self.net.crossing(x)['name']} closed in all directions",
                            "scenario": Scenario(sc.rule, others + all_cw, sc.closed_segments, sc.reversed_segments,
                                                 sc.constraints)})
        # one recovery option unavailable
        if win and win["carriageways"]:
            cons = sc.constraints + [{"kind": "no_reopen", "target": c} for c in win["carriageways"]]
            out.append({"id": f"unavailable:{win['id']}", "kind": "option_unavailable",
                        "changed": f"{win['label']} is not possible (must stay closed)", "scenario": Scenario(
                            sc.rule, sc.closed_carriageways, sc.closed_segments, sc.reversed_segments, cons)})
        elif win:
            # no constraint kind targets a road segment: the same graph-copy evaluations minus that option
            out.append({"id": f"unavailable:{win['id']}", "kind": "option_unavailable",
                        "changed": f"{win['label']} is not possible", "scenario": sc,
                        "ranking": {**orig, "ranking": [r for r in orig["ranking"] if r["id"] != win["id"]],
                                    "excluded": orig["excluded"] + [{**win, "excluded_by": {"code": "unavailable"}}]}})
        # an additional commander constraint: reserve the open carriageway that the most zone routes use now
        cur = self.raw_access(sc, ledger)
        usage = self.carriageway_usage(cur)
        taken = set(sc.closed_carriageways) | {k["target"] for k in sc.constraints}
        busy = sorted(((len(z), c) for c, z in usage.items() if c not in taken), key=lambda t: (-t[0], t[1]))
        if busy:
            n, c = busy[0]
            cw = self._cw(c)
            out.append({"id": f"reserve:{c}", "kind": "commander_constraint",
                        "changed": f"commander reserves {cw['crossing_name']} {cw['direction']} for emergency vehicles "
                                   f"(the open carriageway {n} zone route(s) use now)",
                        "scenario": Scenario(sc.rule, sc.closed_carriageways, sc.closed_segments, sc.reversed_segments,
                                             sc.constraints + [{"kind": "reserve_emergency", "target": c}])})
        for v in out:
            v["scenario"] = self.normalize(v["scenario"])
        return out

    def _judge(self, win: dict | None, orig: dict, r: dict, v: dict) -> dict:
        best = _best(r["ranking"])
        cur = r["current"]
        out = {"current": cur, "best": _brief(best, r) if best else None, "scenario_key": r["scenario_key"],
               "ranked": len(r["ranking"]), "excluded": [e["id"] for e in r["excluded"]]}
        same = bool(win and best and best["id"] == win["id"])
        out["original_still_best"] = same
        out["same_crossing"] = bool(win and best and "contraflow" not in (win["type"], best["type"]) and
                                   _crossings(self, win) and _crossings(self, best) == _crossings(self, win))
        o_km, c_km = orig["current"]["total_delta_m"] / 1000, cur["total_delta_m"] / 1000
        disrupt = (f"Disruption in this variation: +{c_km:.2f} km"
                   + (f", {len(cur['cut_off'])} zone(s) cut off" if cur["cut_off"] else "") + f" (scenario: +{o_km:.2f} km)"
                   + (" — the reservation itself removes that carriageway from public routing."
                      if v["kind"] == "commander_constraint" and c_km > o_km + EPS_M / 1000 else "."))
        km = lambda c: f"{c['recovered_m'] / 1000:.2f} km"  # noqa: E731
        mine = next((x for x in r["ranking"] if win and x["id"] == win["id"]), None)
        gone = next((x for x in r["excluded"] if win and x["id"] == win["id"]), None)
        if not win:
            out["status"] = "no original recommendation"
            body = f"Best here: {best['label']} ({km(best)})." if best else "No repair helps here either."
        elif same:
            ru = next((x for x in r["ranking"][1:] if x["effect"] in ("restores_access", "shorter")), None)
            out["status"] = "remains best"
            body = (f"{win['label']} still recovers the most: {km(best)} (was {km(win)}); {len(best['routes_using_it'])} zone "
                    f"route(s) run through it" + (f"; runner-up {ru['label']} {km(ru)}." if ru else "."))
        elif cur["total_delta_m"] <= EPS_M and not cur["cut_off"]:
            out["status"] = "no repair needed"
            body = ("No zone's shortest route to its nearest assistance point uses the closed roads in this variation, so "
                    "there is nothing to recover.")
        elif gone:
            why = gone["excluded_by"]
            what = (why.get("kind") or why.get("code") or "").replace("_", " ")
            tgt = why.get("target")
            if tgt and tgt in self.net._cw:
                tgt = f"{self._cw(tgt)['crossing_name']} {self._cw(tgt)['direction']}"
            body = (f"{win['label']} is ruled out ({what}{' on ' + tgt if tgt else ''}). "
                    + (f"Best eligible: {best['label']}, {km(best)}." if best else "No remaining repair improves access."))
            out["status"] = "original excluded"
        elif mine is None and out["same_crossing"]:
            out["status"] = "same crossing, narrower action"
            body = (f"{win['label']} is not a candidate as such (part of it is open in this variation); the best repair is "
                    f"still on the same crossing: {best['label']}, {km(best)}.")
        elif mine is None:
            out["status"] = "original not applicable"
            body = (f"{win['label']} is not a candidate (its roads are not closed in this variation). "
                    + (f"Best: {best['label']}, {km(best)}." if best else "No evaluated repair improves access."))
        elif best is None:
            out["status"] = "changed: no repair helps"
            body = (f"{win['label']} is still a candidate but has {mine['effect'].replace('_', ' ')} here ({km(mine)}); "
                    f"no evaluated repair improves access.")
        else:
            rank = r["ranking"].index(mine) + 1
            out["status"] = f"changed: original ranks #{rank}"
            body = (f"{best['label']} recovers {km(best)}"
                    + (f" and restores {len(best['zones_restored'])} zone(s)" if best["zones_restored"] else "")
                    + f", vs {mine['label']} {km(mine)}"
                    + (f" ({mine['effect'].replace('_', ' ')})" if mine["effect"] != "shorter" else "") + ".")
        out["reason"] = f"{body} {disrupt}"
        if win and best and not same and mine is not None:
            names = {z["id"]: z["name"] for z in self.net.zones}
            diff = [{**d, "name": names.get(d["zone"], d["zone"])} for d in _zone_gap(best, mine)]
            if diff:
                out["zones_deciding"] = diff
        return out


def _best(ranking: list[dict]) -> dict | None:
    return next((c for c in ranking if c["effect"] in ("restores_access", "shorter")), None)


def _brief(c: dict, r: dict) -> dict:
    return {"id": c["id"], "label": c["label"], "effect": c["effect"], "recovered_km": round(c["recovered_m"] / 1000, 2),
            "share_of_disruption": c["share_of_disruption"], "zones_restored": c["zones_restored"],
            "zones_improved": len(c["zones_improved"]), "routes_using_it": len(c["routes_using_it"]),
            "after_total_delta_km": round(c["after_total_delta_m"] / 1000, 2)}


def _vhead(v: dict) -> dict:
    return {"id": v["id"], "kind": v["kind"], "changed": v["changed"], "scenario": v["scenario"].to_dict()}


def _crossings(eng: "Engine", c: dict) -> set[str]:
    return {eng.net._by_eid[e]["crossing"] for e in c["segments"] if eng.net._by_eid.get(e, {}).get("crossing")}


def _zone_gap(a: dict, b: dict, n: int = 3) -> list[dict]:
    """Zones where the new winner `a` saves the most more than the original `b` (both measured on the same variation)."""
    sa = {z["zone"]: z["saved_m"] for z in a["zones_improved"]}
    sb = {z["zone"]: z["saved_m"] for z in b["zones_improved"]}
    gaps = sorted(((sa.get(z, 0) - sb.get(z, 0), z) for z in set(sa) | set(sb)), reverse=True)
    return [{"zone": z, "new_saves_m": round(sa.get(z, 0), 1), "original_saves_m": round(sb.get(z, 0), 1)}
            for g, z in gaps[:n] if g > EPS_M]


MAJOR = {"motorway", "trunk", "primary", "motorway_link", "trunk_link", "primary_link"}
PROMINENT = MAJOR | {"secondary"}


def rank_key(r: dict):
    """Documented objective: (1) never lose access, (2) restore access, (3) recover distance."""
    return (len(r["zones_lost"]), -len(r["zones_restored"]), -r["recovered_m"], len(r["segments"]), r["id"])


def _why(effect, net, lost, restored, shorter, longer, uses) -> str:
    if effect == "loses_access":
        return (f"HARMFUL: cuts off road access for {len(lost)} zone(s) that currently have it. Never recommended, "
                f"whatever happens to distances elsewhere.")
    if effect == "restores_access":
        extra = f" It also lengthens {len(longer)} other zone route(s)." if longer else ""
        return f"Restores road access for {len(restored)} zone(s) that are currently cut off.{extra}"
    if effect == "shorter":
        w = f" It lengthens {len(longer)} other zone route(s)." if longer else ""
        return (f"After this change, {len(uses)} zone route(s) to the nearest assistance point run through it; net "
                f"{net / 1000:.2f} km shorter in total.{w}")
    if effect == "longer":
        return f"Makes access worse: {len(longer)} zone route(s) get longer (net {abs(net) / 1000:.2f} km)."
    if effect == "mixed":
        return f"Trade-off: {len(shorter)} route(s) shorter and {len(longer)} longer, with no net change."
    if not uses:
        return ("No zone's shortest route to its nearest assistance point would use it, so it does not change this "
                "metric. It may still matter for other trips, directions or destinations.")
    return "Zone routes use it, but alternatives of equal length already exist, so the metric does not change."


OBJECTIVE = ("Interventions are ranked lexicographically: (1) fewest zones newly cut off (any loss of access ranks "
             "last and is flagged harmful), (2) most zones whose access is restored, (3) most net modeled distance "
             "recovered. Distance = sum over neighbourhood anchors connected before and after of the shortest directed "
             "road distance to the nearest assistance point (active rule). Cut-off zones are always listed separately. "
             "Modeled distance only: not travel time, congestion, people served or evacuation outcome.")
