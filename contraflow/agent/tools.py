"""Typed, allow-listed tools for the Contraflow planning agent.

The agent can only read the graph, evaluate hypothetical scenarios on isolated clones, record
report evidence, and *propose* scenario changes. It cannot mutate roads in the base graph, run
arbitrary Cypher, or apply its own proposals: the user applies them in the UI.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field

from ..engine import CONSTRAINT_KINDS, Engine, Scenario, ValidationError
from ..evidence import named_crossings, record_report, resolve_location
from ..graphdb import BASE, Ledger
from .intent import Intent, parse_intent


class ToolError(Exception):
    def __init__(self, message: str, code: str = "tool_error"):
        super().__init__(message)
        self.code = code


INVESTIGATION_SPECS = [
    {"name": "investigate_disruption", "description": "Read-only. Assume a reported or hypothetical disruption at a "
     "place (user's wording) and evaluate it on an isolated graph copy: which neighbourhoods lose access or get longer "
     "routes, which crossing their routes used, and where they reroute. Records the report and the computed finding "
     "in investigation memory. Never changes the scenario.",
     "parameters": {"type": "object", "properties": {"location": {"type": "string", "description": "user's wording for the place"}},
                    "required": ["location"]}},
    {"name": "assume_constraint", "description": "Read-only. Assume a commander constraint for this analysis only "
     "(it is not proposed). Use when the user asks to keep/reserve a crossing while investigating.",
     "parameters": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["reserve_emergency", "keep_direction", "no_reopen"]},
                                                     "location": {"type": "string"}}, "required": ["kind", "location"]}},
    {"name": "rank_recovery", "description": "Rank recovery interventions under the investigated hypothesis "
     "(current scenario + assumed disruption + assumed constraints), each evaluated on its own graph copy.",
     "parameters": {"type": "object", "properties": {"top_n": {"type": "integer", "minimum": 1, "maximum": 6}}}},
    {"name": "compare_options", "description": "Compare 2-3 ranked interventions side by side on graph copies: per "
     "neighbourhood distance and which river crossing its route uses under each option. Explains why one wins.",
     "parameters": {"type": "object", "properties": {"candidate_ids": {"type": "array", "items": {"type": "string"},
                                                                       "minItems": 2, "maxItems": 3}},
                    "required": ["candidate_ids"]}},
    {"name": "trace_route", "description": "The actual graph route of one neighbourhood to its nearest assistance point "
     "now and under the investigated hypothesis: roads and crossings used, distance.",
     "parameters": {"type": "object", "properties": {"zone": {"type": "string"}}, "required": ["zone"]}},
    {"name": "find_critical_roads", "description": "Read-only network vulnerability analysis: close each road link in a "
     "bounded candidate set (all bridges/carriageways, the links most used by neighbourhood routes, long unused major "
     "roads) on its own graph copy and rank by zones cut off and added modeled distance.",
     "parameters": {"type": "object", "properties": {"top_links": {"type": "integer", "minimum": 5, "maximum": 30}}}},
    {"name": "recall_memory", "description": "Retrieve earlier investigation findings (optionally about a place) from "
     "the memory graph and re-evaluate each on the CURRENT scenario to say whether it still applies.",
     "parameters": {"type": "object", "properties": {"location": {"type": "string"}}}},
]

SPECS = [
    {"name": "assess_field_report", "description": "Use for any field report that a bridge or road is blocked, "
     "flooded or closed. Resolves the location in the graph (asks for confirmation if ambiguous), inspects it, "
     "records the report as evidence, evaluates the closure on an isolated graph copy and proposes it.",
     "parameters": {"type": "object", "properties": {"report_text": {"type": "string", "description": "the report, verbatim"}},
                    "required": ["report_text"]}},
    {"name": "explain_crossing", "description": "Use for questions like 'why does/doesn't X matter'. Resolves the "
     "crossing, shows which neighbourhood routes use each direction, and evaluates closing it.",
     "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}},
    {"name": "search_location", "description": "Resolve a field-report phrase to a modeled river crossing or road section "
     "using FalkorDB full-text indexes and neighbourhood proximity. Always call this first for any place named by the user.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "the user's wording"}},
                    "required": ["text"]}},
    {"name": "inspect_crossing", "description": "Graph context for a crossing: directed carriageways, lanes, nearby "
     "neighbourhoods, which zones' shortest routes use each carriageway, current status, constraints and reports.",
     "parameters": {"type": "object", "properties": {"crossing_id": {"type": "string"}}, "required": ["crossing_id"]}},
    {"name": "inspect_scenario", "description": "Current scenario: closures, constraints and modeled access impact.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "evaluate_closure", "description": "Hypothetically close a crossing (optionally one direction) or road "
     "segments on an isolated graph copy and return the modeled access impact. Does not change the scenario.",
     "parameters": {"type": "object", "properties": {
         "crossing_id": {"type": "string"},
         "direction": {"type": "string", "enum": ["northbound", "southbound", "two-way", "all"]},
         "segment_ids": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "propose_closure", "description": "Record the field report as evidence and propose closing the resolved "
     "target. The user must apply the proposal. Only for targets returned as resolved by search_location.",
     "parameters": {"type": "object", "properties": {
         "crossing_id": {"type": "string"},
         "direction": {"type": "string", "enum": ["northbound", "southbound", "two-way", "all"]},
         "segment_ids": {"type": "array", "items": {"type": "string"}},
         "report_text": {"type": "string"}}, "required": ["report_text"]}},
    {"name": "propose_reopen", "description": "Propose reopening a closed crossing (or one direction).",
     "parameters": {"type": "object", "properties": {
         "crossing_id": {"type": "string"},
         "direction": {"type": "string", "enum": ["northbound", "southbound", "two-way", "all"]}},
         "required": ["crossing_id"]}},
    {"name": "set_planning_constraint", "description": "Propose a commander constraint on a crossing named by the "
     "user (pass their wording as location, or a crossing_id). Kinds: "
     + "; ".join(f"{k}: {v}" for k, v in CONSTRAINT_KINDS.items()),
     "parameters": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": list(CONSTRAINT_KINDS)},
         "location": {"type": "string", "description": "the user's wording for the crossing"},
         "crossing_id": {"type": "string"},
         "direction": {"type": "string", "enum": ["northbound", "southbound", "two-way", "all"]}},
         "required": ["kind"]}},
    {"name": "rank_interventions", "description": "Evaluate every eligible reopening and contraflow candidate for the "
     "scenario under discussion on isolated graph copies and rank them by modeled distance recovered.",
     "parameters": {"type": "object", "properties": {"top_n": {"type": "integer", "minimum": 1, "maximum": 6}}}},
    {"name": "get_evidence", "description": "Field reports recorded in the graph for a crossing.",
     "parameters": {"type": "object", "properties": {"crossing_id": {"type": "string"}}, "required": ["crossing_id"]}},
] + INVESTIGATION_SPECS

# Smaller, intent-relevant tool sets keep the local model's prompt short (latency) and its choices meaningful.
TOOLSETS = {
    "investigate": ["investigate_disruption", "assume_constraint", "rank_recovery", "compare_options", "trace_route",
                    "recall_memory", "search_location", "inspect_crossing"],
    "recall": ["recall_memory", "trace_route", "inspect_scenario", "search_location"],
    "close": ["assess_field_report", "rank_interventions", "search_location", "inspect_crossing", "compare_options",
              "recall_memory"],
    "reopen": ["propose_reopen", "rank_interventions", "search_location", "inspect_crossing", "compare_options"],
    "constraint": ["set_planning_constraint", "rank_interventions", "search_location", "compare_options"],
    "rank": ["rank_interventions", "compare_options", "trace_route", "inspect_scenario"],
    "vulnerability": ["find_critical_roads", "trace_route", "inspect_crossing", "recall_memory"],
    "why": ["explain_crossing", "trace_route", "inspect_crossing", "evaluate_closure", "recall_memory"],
}


def specs_for(intent) -> list[dict]:
    names = TOOLSETS.get(intent.action) or [s_["name"] for s_ in SPECS]
    return [s_ for s_ in SPECS if s_["name"] in names]


@dataclass
class ToolBox:
    state: dict
    scenario: Scenario
    confirm: dict | None = None
    user_msg: str = ""
    intent: Intent | None = None
    ledger: Ledger = field(default_factory=Ledger)
    steps: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    numbers: set = field(default_factory=set)
    authorized: set = field(default_factory=set)     # targets the USER named (own words) or confirmed — never model text
    authorized_segments: set = field(default_factory=set)
    ambiguous: set = field(default_factory=set)      # ids offered as ambiguous alternatives
    focus: str | None = None
    last_search: dict | None = None
    on_step: object = None                           # optional callback(step) for live progress streaming

    def __post_init__(self):
        self.net = self.state["net"]
        self.eng: Engine = self.state["engine"]
        self.db = self.state["db"]
        self.scenario = self.eng.normalize(self.scenario)          # one canonical representation everywhere
        self.work = copy.deepcopy(self.scenario)
        self.base_key = self.scenario.key()
        self.intent = self.intent or parse_intent(self.user_msg)
        self._user_resolved = False
        self.user_resolution: dict | None = None
        self.mem = self.state.get("memory")
        self.inv_id: str | None = None
        self.hypothesis: Scenario | None = None       # current scenario + assumed disruption/constraints (read-only)
        self.hyp_delta = {"closed_carriageways": [], "closed_segments": [], "constraints": [], "label": []}
        self.last_ranking: dict | None = None
        if self.confirm and self.confirm.get("crossing_id"):
            # runtime has already checked the choice against the pending clarification's candidates
            self.authorized.add(self.confirm["crossing_id"])
            self.focus = self.confirm["crossing_id"]
            self._user_resolved = True

    # ------------------------------------------------------------------ dispatch
    def call(self, name: str, args: dict | None) -> dict:
        args = args or {}
        fn = getattr(self, f"t_{name}", None)
        if fn is None or name not in {s["name"] for s in SPECS}:
            raise ToolError(f"unknown tool {name!r}")
        q0, t0 = len(self.ledger.entries), time.perf_counter()
        try:
            result = fn(**args)
            ok = True
        except TypeError as e:
            result, ok = {"error": f"bad arguments for {name}: {e}"}, False
        except ToolError as e:
            result, ok = {"error": str(e), "code": e.code}, False
        except ValidationError as e:
            result, ok = {"error": str(e), "code": e.code, **e.detail}, False
        self._collect(result)
        step = {"kind": "tool" if ok else "error", "tool": name, "args": args, "result": result,
                "result_summary": result.get("summary") or result.get("error", ""),
                "ms": round((time.perf_counter() - t0) * 1000, 1), "queries": len(self.ledger.entries) - q0}
        self.steps.append(step)
        if callable(self.on_step):
            try:
                self.on_step(step)
            except Exception:  # noqa: BLE001 — progress reporting must never break the agent
                pass
        return result

    def _collect(self, obj):
        """Every number a tool returned (values, list lengths, numbers inside text) may be cited."""
        if isinstance(obj, bool):
            return
        if isinstance(obj, (int, float)):
            self.numbers.add(float(obj))
        elif isinstance(obj, str):
            for n in re.findall(r"\d+(?:\.\d+)?", obj):
                self.numbers.add(float(n))
        elif isinstance(obj, dict):
            for v in obj.values():
                self._collect(v)
        elif isinstance(obj, (list, tuple)):
            self.numbers.add(float(len(obj)))
            for v in obj:
                self._collect(v)

    # ------------------------------------------------------------------ helpers
    def zname(self, zid: str) -> str:
        return next((z["name"] for z in self.net.zones if z["id"] == zid), zid)

    def _crossing(self, crossing_id: str) -> dict:
        try:
            return self.net.crossing(crossing_id)
        except KeyError:
            raise ToolError(f"unknown crossing {crossing_id!r}; valid ids: "
                            + ", ".join(x["id"] for x in self.net.crossings)) from None

    def _carriageways(self, crossing_id: str, direction: str | None) -> list[str]:
        x = self._crossing(crossing_id)
        cws = [c["id"] for c in x["carriageways"] if direction in (None, "", "all") or c["direction"] == direction]
        if not cws:
            raise ToolError(f"{x['name']} has no {direction} carriageway; it has "
                            + ", ".join(c["direction"] for c in x["carriageways"]), "direction_unavailable")
        return cws

    def _authorize_from_user(self):
        """Resolve the USER's own wording once. Only this (or an explicit confirmation) authorizes a target."""
        if self._user_resolved:
            return
        self._user_resolved = True
        if not self.intent.text:
            return
        r = resolve_location(self.db, self.net, self.intent.text, self.ledger)
        self.user_resolution = r
        if r["status"] == "resolved":
            res = r["resolved"]
            if res["kind"] == "road_section":
                self.authorized_segments.update(res["segment_ids"])
            elif res["kind"] == "crossing":
                self.authorized.add(res["id"])
        elif r["status"] == "ambiguous":
            self.ambiguous.update(c["id"] for c in r["candidates"] if c["kind"] == "crossing")

    def authorize_context(self, crossing_id: str) -> bool:
        """Deterministic planner only: 'this/that bridge/it' may refer to the crossing under discussion ONLY when the
        message names no location of its own. A new named or ambiguous location always takes precedence."""
        self._authorize_from_user()
        r = self.user_resolution or {}
        if r.get("status") in ("resolved", "ambiguous") or r.get("candidates") or r.get("matched_zones"):
            return False
        self.authorized.add(crossing_id)
        self.context_authorized = crossing_id
        return True

    def _guard(self, crossing_id: str | None):
        if not crossing_id:
            return
        self._authorize_from_user()
        if crossing_id not in self.authorized:
            if crossing_id in self.ambiguous:
                raise ToolError("This location is ambiguous. Ask the user to confirm which crossing they mean "
                                "before proposing any change.", "ambiguous_location")
            raise ToolError(f"The user's message does not identify {crossing_id}. Changes may only target a location "
                            "resolved from the user's own words or confirmed by the user.", "unauthorized_target")

    def _require_intent(self, operation: str):
        """Fail closed: a mutation is allowed only for an affirmative, non-question user action of the same
        operation (constraint kinds are operations: reserve_emergency, keep_direction, no_reopen)."""
        it = self.intent
        if it.operation != operation or not it.may_mutate:
            have = f"{it.polarity} {it.mood} '{it.operation}'"
            code = "negated_instruction" if it.polarity == "negative" and it.operation == operation else "intent_mismatch"
            raise ToolError(f"The user's request is {have}; proposing '{operation}' is not permitted.", code)

    def _direction(self, direction: str | None) -> str | None:
        """Exact user-authorized scope. If the user stated a direction, only that direction may be used: an omitted
        argument is filled with it and 'all' or a different direction is rejected. If the user stated none, the
        authorized scope is the whole crossing and a model-chosen single direction is corrected to it."""
        stated = self.intent.direction or (self.confirm or {}).get("direction")
        arg = None if direction in (None, "") else direction
        if stated:
            if arg in (None, stated):
                return stated
            raise ToolError(f"The user authorized only the {stated} direction; '{arg}' is not permitted.",
                            "direction_conflict")
        if arg not in (None, "all"):
            self.steps.append({"kind": "thought", "text": f"Guard: direction '{arg}' was not stated by the user; the "
                               "authorized scope is the whole crossing."})
        return None

    def _commit(self, before: Scenario):
        """Every proposed working scenario passes the engine's authoritative validation (constraints included)
        and is stored in canonical form."""
        try:
            self.eng.validate(self.work)
        except ValidationError:
            self.work = before
            raise
        self.work = self.eng.normalize(self.work)

    def _targets(self, crossing_id, direction, segment_ids) -> tuple[list[str], list[str]]:
        segment_ids = segment_ids or []
        for e in segment_ids:
            if e not in self.net._by_eid:
                raise ToolError(f"unknown segment id {e}")
        cws = self._carriageways(crossing_id, direction) if crossing_id else []
        if not cws and not segment_ids:
            raise ToolError("specify crossing_id or segment_ids")
        return cws, segment_ids

    def _impact(self, sc: Scenario, ref: Scenario) -> dict:
        a = self.eng.raw_access(ref, self.ledger)
        b = self.eng.raw_access(sc, self.ledger)
        cmp = self.eng.compare(a, b, sc.rule)
        base_cmp = self.eng.compare(self.eng.baseline(sc.rule), b, sc.rule)
        worst = sorted([z for z in cmp["zones"] if z.get("delta", 0) > 0.5], key=lambda z: -z["delta"])[:5]
        return {"added_km": round(cmp["total_delta_m"] / 1000, 2), "zones_affected": cmp["affected_zones"],
                "zones_cut_off": [self.zname(z) for z in cmp["cut_off"]],
                "largest": [{"zone": self.zname(z["zone"]), "added_km": round(z["delta"] / 1000, 2),
                             "nearest_help_now": (z["help"] or ["?"])[0]} for z in worst],
                "vs_baseline_km": round(base_cmp["total_delta_m"] / 1000, 2)}

    def _label(self, cws: list[str], segs: list[str]) -> str:
        parts = []
        for x in self.net.crossings:
            dirs = [c["direction"] for c in x["carriageways"] if c["id"] in cws]
            if dirs:
                parts.append(x["name"] if len(dirs) == len(x["carriageways"]) else f"{x['name']} ({', '.join(dirs)})")
        if segs:
            names = sorted({self.net.seg(e)["name"] or "unnamed road" for e in segs})
            parts.append(f"{len(segs)} segment(s) of {', '.join(names)}")
        return "; ".join(parts)

    def _propose(self, kind: str, label: str, report_id: str | None = None, scope: dict | None = None):
        prev = self.proposals[-1]["scenario_key"] if self.proposals else self.base_key
        self.proposals.append({"id": f"p{len(self.proposals) + 1}", "kind": kind, "label": label, "state": "proposed",
                               "scenario": self.work.to_dict(), "scenario_key": self.work.key(),
                               "base_key": prev, "report_id": report_id, "scope": scope or {},
                               "operation": self.intent.operation,
                               "authorized_by": "user confirmation" if self.confirm else (
                                   "conversation context" if getattr(self, "context_authorized", None) else "user wording")})
        if self.mem:
            self.proposals[-1]["memory_id"] = f"prop_{self.base_key}_{len(self.proposals)}_{int(time.time() * 1000) % 10**8}"
            self.mem.proposal(self._investigation(), self.proposals[-1]["memory_id"], kind, label, self.work.key(),
                              self.ledger)

    # ------------------------------------------------------------------ tools
    def t_search_location(self, text: str) -> dict:
        r = resolve_location(self.db, self.net, text, self.ledger)
        self.last_search = r
        cands = [{k: c[k] for k in ("kind", "id", "name", "method") if k in c}
                 | ({"distance_m": c["distance_m"]} if "distance_m" in c else {})
                 | ({"segments": c["segments"]} if "segments" in c else {}) for c in r["candidates"]]
        out = {"status": r["status"], "candidates": cands, "direction_hint": r["direction_hint"],
               "reason": r.get("reason"), "matched_zones": [z["name"] for z in r.get("matched_zones", [])]}
        if r["status"] == "resolved":
            res = r["resolved"]
            out["resolved"] = {k: res[k] for k in ("kind", "id", "name", "method", "detail") if k in res}
            if res["kind"] == "road_section":
                out["resolved"]["segment_ids"] = res["segment_ids"]
            else:
                self.focus = res["id"]
            out["note"] = "Search results are informational; they do not authorize changes."
            out["summary"] = f"resolved to {res['name']} via {res['method'].replace('_', ' ')}"
        else:
            self.ambiguous.update(c["id"] for c in r["candidates"] if c["kind"] == "crossing")
            out["summary"] = f"{r['status']}: {r.get('reason', '')}"
        return out

    def t_inspect_crossing(self, crossing_id: str) -> dict:
        x = self._crossing(crossing_id)
        g = self.db.graph(BASE)
        rows = g.q("inspect_crossing", "MATCH (x:Crossing {id: $id})-[:HAS_CARRIAGEWAY]->(c:Carriageway) "
                   "OPTIONAL MATCH (z:Zone)-[n:NEAR]->(x) WITH x, c, z, n ORDER BY n.m "
                   "RETURN c.id, c.direction, c.oneway, c.lanes, collect(DISTINCT [z.name, n.m])[0..4]",
                   {"id": crossing_id}, self.ledger)
        base_usage = self.eng.carriageway_usage(self.eng.baseline(self.work.rule))
        cur_usage = self.eng.carriageway_usage(self.eng.raw_access(self.work, self.ledger))
        cons = {}
        for k in self.work.constraints:
            cons.setdefault(k["target"], []).append(k["kind"])
        cws = []
        for cid, direction, oneway, lanes, near in rows:
            cws.append({"id": cid, "direction": direction, "one_way": oneway, "lanes": lanes or "not tagged",
                        "status": "closed" if cid in self.work.closed_carriageways else "open",
                        "constraints": cons.get(cid, []),
                        "baseline_zone_routes_using": [self.zname(z) for z in base_usage.get(cid, [])],
                        "current_zone_routes_using": [self.zname(z) for z in cur_usage.get(cid, [])]})
        near = [{"zone": n[0], "distance_m": round(n[1])} for n in rows[0][4] if n[0]] if rows else []
        reports = self.mem.reports(crossing_id) if self.mem else []
        self.focus = crossing_id
        used = sum(len(c["baseline_zone_routes_using"]) for c in cws)
        return {"crossing": x["name"], "id": crossing_id, "carries": x["roads"], "carriageways": cws,
                "nearest_neighbourhoods": near, "reports": [{"text": r["text"], "provenance": r["provenance"]} for r in reports[:5]],
                "rule": self.work.rule,
                "summary": f"{x['name']}: {len(cws)} carriageway(s); {used} baseline zone route use(s) under rule {self.work.rule}"}

    def t_inspect_scenario(self) -> dict:
        r = self.eng.evaluate(self.work, self.ledger)
        m = r["metrics"]
        return {"rule": f"{self.work.rule} ({r['rule']['label']})",
                "closed": [self.net.carriageway(c)["crossing_name"] + " " + self.net.carriageway(c)["direction"]
                           for c in self.work.closed_carriageways] + [f"{len(self.work.closed_segments)} other road segment(s)"]
                if self.work.closed_segments else [self.net.carriageway(c)["crossing_name"] + " " +
                                                   self.net.carriageway(c)["direction"] for c in self.work.closed_carriageways],
                "constraints": [f"{k['kind']} on {k['target']}" for k in self.work.constraints],
                "added_km_vs_baseline": round(m["total_delta_m"] / 1000, 2), "zones_affected": m["affected_zones"],
                "zones_cut_off": [self.zname(z) for z in m["cut_off"]],
                "largest": {"zone": self.zname(m["largest"]["zone"]), "added_km": round(m["largest"]["delta_m"] / 1000, 2)}
                if m["largest"] else None,
                "summary": f"{len(self.work.closed_carriageways)} carriageway(s) closed; +{m['total_delta_m'] / 1000:.1f} km vs baseline"}

    def t_evaluate_closure(self, crossing_id: str | None = None, direction: str | None = None,
                           segment_ids: list | None = None) -> dict:
        cws, segs = self._targets(crossing_id, direction, segment_ids)
        nxt = Scenario.from_dict(self.work.to_dict())
        nxt.closed_carriageways = sorted(set(nxt.closed_carriageways) | set(cws))
        nxt.closed_segments = sorted(set(nxt.closed_segments) | set(segs))
        imp = self._impact(nxt, self.work)
        return {"closure": self._label(cws, segs), "carriageways": cws, **imp,
                "summary": f"closing {self._label(cws, segs)} adds {imp['added_km']} km over {imp['zones_affected']} zone(s)"}

    def t_propose_closure(self, report_text: str, crossing_id: str | None = None, direction: str | None = None,
                          segment_ids: list | None = None) -> dict:
        self._require_intent("close")
        self._guard(crossing_id)
        self._authorize_from_user()
        for e in segment_ids or []:
            if e not in self.authorized_segments:
                raise ToolError("segment ids must come from the road section named in the user's own report",
                                "unauthorized_target")
        cws, segs = self._targets(crossing_id, self._direction(direction), segment_ids)
        before = Scenario.from_dict(self.work.to_dict())
        self.work.closed_carriageways = sorted(set(self.work.closed_carriageways) | set(cws))
        self.work.closed_segments = sorted(set(self.work.closed_segments) | set(segs))
        self._commit(before)
        method = "user confirmation" if (self.confirm or {}).get("crossing_id") == crossing_id else "user wording"
        targets = ([{"kind": "crossing", "id": crossing_id, "method": method}] if crossing_id else []) + \
            [{"kind": "zone", "id": z["id"], "method": "road section near zone"} for z in
             (self.last_search or {}).get("matched_zones", []) if segs]
        # evidence linked to the road topology it refers to (base graph), plus a durable copy in investigation memory
        rid = record_report(self.db, report_text, targets, "proposed", self.ledger)
        self._report(report_text, [{"kind": t["kind"], "id": t["id"], "method": t["method"],
                                    "name": self._name(t["kind"], t["id"])} for t in targets], rid=rid)
        imp = self._impact(self.work, before)
        label = f"Close {self._label(cws, segs)}"
        self._propose("close", label, rid, {"carriageways": cws, "segments": segs})
        return {"proposal": label, "report_id": rid, **imp,
                "summary": f"proposed: {label} (+{imp['added_km']} km); report {rid} recorded"}

    def t_propose_reopen(self, crossing_id: str, direction: str | None = None) -> dict:
        self._require_intent("reopen")
        self._guard(crossing_id)
        cws = [c for c in self._carriageways(crossing_id, self._direction(direction)) if c in self.work.closed_carriageways]
        if not cws:
            raise ToolError(f"{self._crossing(crossing_id)['name']} has no closed carriageway in that direction",
                            "nothing_to_reopen")
        reserved = [k for k in self.work.constraints if k["target"] in cws and k["kind"] == "reserve_emergency"]
        if reserved:
            raise ToolError(f"{reserved[0]['target']} is reserved for emergency vehicles; it is not a public reopening",
                            "constraint_violation")
        before = Scenario.from_dict(self.work.to_dict())
        self.work.closed_carriageways = [c for c in self.work.closed_carriageways if c not in cws]
        self._commit(before)
        imp = self._impact(self.work, before)
        label = f"Reopen {self._label(cws, [])}"
        self._propose("reopen", label, scope={"carriageways": cws})
        return {"proposal": label, "recovered_km": round(-imp["added_km"], 2), "vs_baseline_km": imp["vs_baseline_km"],
                "summary": f"proposed: {label}"}

    def t_set_planning_constraint(self, kind: str, crossing_id: str | None = None, location: str | None = None,
                                  direction: str | None = None) -> dict:
        if kind not in CONSTRAINT_KINDS:
            raise ToolError(f"unknown constraint kind {kind}", "unknown_constraint")
        self._require_intent(kind)
        if location and not crossing_id:
            r = self.t_search_location(location)
            if r["status"] != "resolved" or r["resolved"]["kind"] != "crossing":
                return {"needs_confirmation": True, **r, "summary": f"location {r['status']}: {r.get('reason')}"}
            crossing_id = r["resolved"]["id"]
        if not crossing_id:
            raise ToolError("give location or crossing_id")
        self._guard(crossing_id)
        cws = self._carriageways(crossing_id, self._direction(direction))
        new = [{"kind": kind, "target": c} for c in cws
               if not any(k["kind"] == kind and k["target"] == c for k in self.work.constraints)]
        before = Scenario.from_dict(self.work.to_dict())
        self.work.constraints = self.work.constraints + new
        self._commit(before)
        label = f"{kind.replace('_', ' ')}: {self._label(cws, [])}"
        self._propose("constraint", label, scope={"carriageways": cws, "kind": kind})
        return {"constraint": kind, "meaning": CONSTRAINT_KINDS[kind], "carriageways": cws,
                "applies_to": self._label(cws, []), "summary": f"proposed constraint {label}"}

    def t_rank_interventions(self, top_n: int = 4) -> dict:
        top_n = max(1, min(int(top_n or 4), 6))
        r = self.eng.interventions(self.work, self.ledger)
        cur = r["current"]["total_delta_m"]
        ranked = []
        for c in r["ranking"][:top_n]:
            ranked.append({"rank": len(ranked) + 1, "action": c["label"], "type": c["type"], "effect": c["effect"],
                           "zones_cut_off_by_it": [self.zname(z) for z in c["zones_lost"]],
                           "recovered_km": round(c["recovered_m"] / 1000, 2),
                           "share_of_current_disruption_pct": round(c["share_of_disruption"] * 100) if c["share_of_disruption"] is not None else None,
                           "zones_improved": [{"zone": self.zname(z["zone"]), "saved_km": round(z["saved_m"] / 1000, 2)}
                                              for z in c["zones_improved"][:5]],
                           "zones_restored": [self.zname(z) for z in c["zones_restored"]],
                           "zones_worsened": [self.zname(z["zone"]) for z in c["zones_worsened"]],
                           "why": c["why"], "candidate_id": c["id"]})
        excluded = [{"action": c["label"], "excluded_by": c["excluded_by"].get("kind") or c["excluded_by"].get("code")}
                    for c in r["excluded"] if c.get("excluded_by")]
        zero = [c["label"] for c in r["ranking"] if c["effect"] == "no_change"]
        harmful = [{"action": c["label"], "effect": c["effect"], "cuts_off": [self.zname(z) for z in c["zones_lost"]]}
                   for c in r["ranking"] if c["effect"] in ("loses_access", "longer", "mixed")]
        return {"scenario_key": self.work.key(), "evaluated_for": "proposed scenario" if self.work.key() != self.base_key
                else "current scenario",
                "constraints": [f"{k['kind']} on {k['target']}" for k in self.work.constraints],
                "current_added_km": round(cur / 1000, 2), "candidates_evaluated": len(r["ranking"]),
                "ranking": ranked, "excluded_by_constraints": excluded, "no_effect": zero[:6], "harmful": harmful[:6],
                "objective": "modeled sum of per-zone shortest directed road distance to the nearest assistance point",
                "summary": (f"{len(r['ranking'])} candidates; top: {ranked[0]['action']} ({ranked[0]['effect']}, "
                            f"{ranked[0]['recovered_km']} km)" if ranked else "no eligible interventions")}

    def t_assess_field_report(self, report_text: str) -> dict:
        self._require_intent("close")
        r = self.t_search_location(self.intent.text or report_text)   # resolve the user's words, not a paraphrase
        if r["status"] != "resolved":
            return {"needs_confirmation": r["status"] == "ambiguous", "status": r["status"], "reason": r.get("reason"),
                    "candidates": r["candidates"], "summary": f"location {r['status']}: {r.get('reason')}"}
        res = r["resolved"]
        if res["kind"] == "road_section":
            p = self.t_propose_closure(report_text, segment_ids=res["segment_ids"])
            return {"resolved": res["name"], "how": res.get("detail"), **p}
        info = self.t_inspect_crossing(res["id"])
        d = self.intent.direction
        p = self.t_propose_closure(self.intent.text or report_text, crossing_id=res["id"], direction=d or "all")
        return {"resolved": res["name"], "crossing_id": res["id"], "how": res.get("detail"),
                "direction": d or "all",
                "baseline_zone_routes_using": sorted({z for c in info["carriageways"] for z in c["baseline_zone_routes_using"]}),
                **{k: v for k, v in p.items() if k != "summary"},
                "summary": f"{res['name']} ({res['method'].replace('_', ' ')}); {p['summary']}"}

    def t_explain_crossing(self, location: str) -> dict:
        r = self.t_search_location(location)
        if r["status"] != "resolved" or r["resolved"]["kind"] != "crossing":
            return {"needs_confirmation": r["status"] == "ambiguous", **r}
        xid = r["resolved"]["id"]
        info = self.t_inspect_crossing(xid)
        ev = self.t_evaluate_closure(crossing_id=xid, direction="all")
        users = sorted({z for c in info["carriageways"] for z in c["baseline_zone_routes_using"]})
        return {"crossing": info["crossing"], "baseline_zone_routes_using": users, "closing_it": ev,
                "note": "Zero change means no anchor's shortest route to its nearest assistance point uses it under this "
                        "rule; it may still matter for other trips, directions or capacity." if not users else "",
                "summary": f"{info['crossing']}: used by {len(users)} baseline zone route(s); closing adds {ev['added_km']} km"}

    def t_get_evidence(self, crossing_id: str) -> dict:
        self._crossing(crossing_id)
        reps = self.mem.reports(crossing_id) if self.mem else []
        return {"reports": [{"id": r["id"], "text": r["text"], "ts": r["ts"], "provenance": r["provenance"]}
                            for r in reps], "summary": f"{len(reps)} report(s) linked to {crossing_id}"}

    # ================================================================== investigation helpers
    def _name(self, kind: str, pid: str) -> str:
        if kind == "crossing":
            return self.net.crossing(pid)["name"] if pid in self.net._x else pid
        if kind == "zone":
            return self.zname(pid)
        return pid

    def _investigation(self) -> str | None:
        if not self.mem:
            return None
        if self.inv_id is None:
            fp = self.state.get("load", {}).get("fingerprint", "")
            self.inv_id = self.mem.start(self.user_msg, self.intent.to_dict(), self.scenario.to_dict(), self.base_key,
                                         fp, self.state.get("mode_label", "agent"), self.ledger)
        return self.inv_id

    def _report(self, text: str, refs: list[dict], rid: str | None = None) -> str | None:
        if not self.mem:
            return rid
        claim = f"{self.intent.polarity} {self.intent.operation}"
        return self.mem.report(self._investigation(), text, [{**r, "resolution": r.get("resolution", "resolved")}
                                                             for r in refs], self.ledger, claim=claim, rid=rid)

    def _hyp(self) -> Scenario:
        return self.hypothesis or self.work

    def _route_crossings(self, route: list[str]) -> list[str]:
        seen = []
        for e in route or []:
            cw = self.net._by_eid.get(e.replace("rev:", ""), {}).get("carriageway")
            if cw:
                name = self.net.carriageway(cw)["crossing_name"] + " " + self.net.carriageway(cw)["direction"]
                if name not in seen:
                    seen.append(name)
        return seen

    def _zone_id(self, text: str) -> str | None:
        t = text.strip().lower()
        for z in self.net.zones:
            if z["id"] == t or z["name"].lower() == t:
                return z["id"]
        for z in self.net.zones:
            if t and (t in z["name"].lower() or z["name"].lower() in t):
                return z["id"]
        return None

    def _apply_delta(self, sc: Scenario, delta: dict) -> Scenario:
        nxt = Scenario.from_dict(sc.to_dict())
        nxt.closed_carriageways = sorted(set(nxt.closed_carriageways) | set(delta.get("closed_carriageways", [])))
        nxt.closed_segments = sorted(set(nxt.closed_segments) | set(delta.get("closed_segments", [])))
        nxt.constraints = nxt.constraints + [k for k in delta.get("constraints", []) if k not in nxt.constraints]
        return self.eng.normalize(nxt)

    def _impact_detail(self, before: Scenario, after: Scenario) -> dict:
        a = self.eng.raw_access(before, self.ledger)
        b = self.eng.raw_access(after, self.ledger)
        cmp = self.eng.compare(a, b, after.rule)
        affected = []
        for z in cmp["zones"]:
            zid = z["zone"]
            if z["status"] == "cut_off" and zid in a:
                affected.append({"id": zid, "zone": self.zname(zid), "status": "cut_off", "delta_m": None,
                                 "route_used": self._route_crossings(a[zid]["route"]), "reroutes_via": []})
            elif z.get("delta") and abs(z["delta"]) > 0.5:
                affected.append({"id": zid, "zone": self.zname(zid), "status": "longer" if z["delta"] > 0 else "shorter",
                                 "delta_m": z["delta"], "now_km": round(z["cur"] / 1000, 2),
                                 "route_used": self._route_crossings(a[zid]["route"]),
                                 "reroutes_via": self._route_crossings(b[zid]["route"]),
                                 "nearest_help_now": (b[zid]["help"] or ["?"])[0]})
        affected.sort(key=lambda x: (x["status"] != "cut_off", -(x["delta_m"] or 0)))
        return {"added_km": round(cmp["total_delta_m"] / 1000, 2), "zones_affected": cmp["affected_zones"],
                "zones_cut_off": [self.zname(z) for z in cmp["cut_off"] if z in a], "affected": affected}

    # ================================================================== investigation tools (read-only)
    def t_investigate_disruption(self, location: str) -> dict:
        if self.intent.action not in ("investigate", "close", "why"):
            raise ToolError("investigate_disruption is for reported or hypothetical disruptions", "intent_mismatch")
        text = self.intent.primary_text or location
        confirmed = (self.confirm or {}).get("crossing_id")
        # a location the user chose from offered candidates is used as is; resolving again would re-ask forever
        r = {"status": "resolved", "resolved": None} if confirmed else resolve_location(self.db, self.net, text, self.ledger)
        if r["status"] != "resolved":
            self.ambiguous.update(c["id"] for c in r["candidates"] if c["kind"] == "crossing")
            self.last_search = r
            refs = [{"kind": c["kind"], "id": c["id"], "name": c["name"], "resolution": "ambiguous",
                     "method": c.get("method", "")} for c in r["candidates"] if c["kind"] == "crossing"]
            if refs:
                self._report(self.intent.text, refs)
            return {"needs_confirmation": r["status"] == "ambiguous", "status": r["status"], "reason": r.get("reason"),
                    "candidates": [{k: c[k] for k in ("kind", "id", "name") if k in c} | (
                        {"distance_m": c["distance_m"]} if "distance_m" in c else {}) for c in r["candidates"]],
                    "summary": f"location {r['status']}: {r.get('reason')}"}
        res = r["resolved"]
        if (self.confirm or {}).get("crossing_id"):
            res = {"kind": "crossing", "id": self.confirm["crossing_id"], "name": self.net.crossing(self.confirm["crossing_id"])["name"],
                   "method": "user confirmation", "detail": "confirmed by the user"}
        named = [] if confirmed else named_crossings(self.net, text)
        if res["kind"] == "crossing" and len(named) > 1:
            # several crossings named in the report: each keeps its own scope (direction written after its own name)
            cws = [c for xid, d in named for c in self._carriageways(xid, d)]
            delta = {"closed_carriageways": cws, "closed_segments": []}
            label = self._label(cws, [])
            concerns = [{"kind": "crossing", "id": xid, "name": self._name("crossing", xid)} for xid, _ in named]
            res = {**res, "name": " and ".join(c["name"] for c in concerns), "method": "names in the report",
                   "detail": "the report names these crossings"}
            scopes = [{"crossing_id": xid, "name": self._name("crossing", xid), "direction": d} for xid, d in named]
        elif res["kind"] == "crossing":
            scopes = [{"crossing_id": res["id"], "name": res["name"], "direction": self.intent.direction}]
            cws = self._carriageways(res["id"], self.intent.direction)
            delta = {"closed_carriageways": cws, "closed_segments": []}
            label = self._label(cws, [])
            concerns = [{"kind": "crossing", "id": res["id"], "name": res["name"]}]
        else:
            delta = {"closed_carriageways": [], "closed_segments": res["segment_ids"]}
            label = res["detail"]
            concerns = [{"kind": "road_section", "id": f"section:{res['name']}:{len(res['segment_ids'])}", "name": res["name"]}]
        prior = self.mem.reports(concerns[0]["id"]) if self.mem else []
        claim = f"{self.intent.polarity} {self.intent.operation}"
        # contradictory evidence: an earlier report about the same place that says it is open / not blocked
        conflicts = [p for p in prior if (p.get("claim") or "") in ("affirmative reopen", "negative close",
                                                                     "negative investigate")]
        self._report(self.intent.text, [{**concerns[0], "method": res.get("method", "")}])
        before = self._hyp()
        already = [c for c in delta["closed_carriageways"] if c in before.closed_carriageways] + \
            [e for e in delta["closed_segments"] if e in before.closed_segments]
        after = self._apply_delta(before, delta)
        det = self._impact_detail(before, after)
        self.hypothesis = after
        self.hyp_delta["closed_carriageways"] += delta["closed_carriageways"]
        self.hyp_delta["closed_segments"] += delta["closed_segments"]
        self.hyp_delta["label"].append(f"closed {label}")
        self.focus = res["id"] if res["kind"] == "crossing" else self.focus
        summary = (f"Hypothesis: {label} closed. {det['zones_affected']} zone(s) longer, "
                   f"{len(det['zones_cut_off'])} cut off; +{det['added_km']} km modeled distance")
        if self.mem:
            self.mem.finding(self._investigation(), "disruption_impact", summary, before.key(),
                             {"closed_carriageways": delta["closed_carriageways"], "closed_segments": delta["closed_segments"],
                              "label": label},
                             {"added_km": det["added_km"], "zones_affected": det["zones_affected"],
                              "zones_cut_off": det["zones_cut_off"]},
                             concerns, [{"id": a["id"], "name": a["zone"], "delta_m": a["delta_m"], "status": a["status"]}
                                        for a in det["affected"]],
                             ["closure is a hypothesis from an unverified report, evaluated on a graph copy"], self.ledger)
        return {"hypothesis": f"{label} closed (not applied)", "resolved": res["name"], "closes": delta,
                "scopes": scopes if res["kind"] == "crossing" else [],
                "how": res.get("detail", res.get("method")), "direction": self.intent.direction or "all",
                **det, "already_closed": [self._label([c], []) if ":" in c else c for c in already],
                "fully_already_closed": bool(already) and len(already) == len(delta["closed_carriageways"]) +
                len(delta["closed_segments"]), "earlier_reports_here": [{"text": p["text"], "ts": p["ts"]} for p in prior[:3]],
                "conflicting_reports": [{"text": p["text"], "ts": p["ts"]} for p in conflicts[:3]],
                "summary": summary}

    def t_assume_constraint(self, kind: str, location: str) -> dict:
        if kind not in CONSTRAINT_KINDS:
            raise ToolError(f"unknown constraint kind {kind}", "unknown_constraint")
        if self.intent.action != "investigate" or self.intent.aux_constraint != kind:
            raise ToolError("assume_constraint is only for a constraint the user asked to keep during an investigation",
                            "intent_mismatch")
        r = resolve_location(self.db, self.net, self.intent.aux_text or location, self.ledger)
        if r["status"] != "resolved" or r["resolved"]["kind"] != "crossing":
            return {"needs_confirmation": r["status"] == "ambiguous", "status": r["status"], "reason": r.get("reason"),
                    "candidates": [{k: c[k] for k in ("kind", "id", "name") if k in c} for c in r["candidates"]],
                    "summary": f"constraint location {r['status']}"}
        xid = r["resolved"]["id"]
        cws = self._carriageways(xid, self.intent.aux_direction)   # the constraint clause's own direction, never the report's
        cons = [{"kind": kind, "target": c} for c in cws]
        overlap = [c for c in cws if c in self.hyp_delta["closed_carriageways"]]
        if overlap and kind == "reserve_emergency":
            # conflicting scopes: the report closes what the constraint would reserve. Say so; assume nothing.
            return {"error": f"conflicting scope: the reported disruption closes {self._label(overlap, [])}, which the "
                             f"constraint would reserve; a closed carriageway cannot be reserved for emergency access",
                    "code": "conflicting_scope", "summary": f"assumption not applied: conflicts with the hypothesis "
                                                            f"({self._label(overlap, [])})"}
        cand = self._apply_delta(self._hyp(), {"constraints": cons})
        try:
            self.eng.validate(cand)
        except ValidationError as e:
            return {"error": str(e), "code": e.code, "summary": f"assumption rejected: {e}"}
        self.hypothesis = cand
        self.hyp_delta["constraints"] += cons
        self.hyp_delta["label"].append(f"{kind.replace('_', ' ')} on {self._label(cws, [])}")
        return {"assumed": f"{kind.replace('_', ' ')} on {self._label(cws, [])}", "meaning": CONSTRAINT_KINDS[kind],
                "scope": {"crossing_id": xid, "name": self._name("crossing", xid), "direction": self.intent.aux_direction},
                "note": "assumed for this analysis only; not proposed", "summary": f"assumed {kind} on {self._label(cws, [])}"}

    def t_rank_recovery(self, top_n: int = 4) -> dict:
        hyp = self._hyp()
        saved = self.work
        self.work = hyp
        try:
            out = self.t_rank_interventions(top_n)
        finally:
            self.work = saved
        out["evaluated_for"] = "investigated hypothesis" if self.hypothesis else "current scenario"
        out["hypothesis"] = self.hyp_delta["label"]
        out["scenario_key"] = hyp.key()
        self.last_ranking = out
        good = [c for c in out["ranking"] if c["effect"] in ("restores_access", "shorter")]
        if self.mem:
            self.mem.finding(self._investigation(), "recovery_ranking",
                             (f"Best under hypothesis: {good[0]['action']} ({good[0]['recovered_km']} km)" if good
                              else "No beneficial intervention"), hyp.key(),
                             {"closed_carriageways": self.hyp_delta["closed_carriageways"],
                              "closed_segments": self.hyp_delta["closed_segments"], "constraints": self.hyp_delta["constraints"]},
                             {"top": [{"action": c["action"], "recovered_km": c["recovered_km"], "effect": c["effect"]}
                                      for c in out["ranking"][:3]]},
                             [{"kind": "crossing", "id": cw.split(":")[0], "name": self._name("crossing", cw.split(":")[0])}
                              for cw in sorted(set(self.hyp_delta["closed_carriageways"]))], [],
                             [f"assumed: {x}" for x in self.hyp_delta["label"]], self.ledger)
        return out

    def t_compare_options(self, candidate_ids: list) -> dict:
        ids = [str(c) for c in (candidate_ids or [])][:3]
        if len(ids) < 2:
            raise ToolError("give 2-3 candidate ids from a ranking", "bad_arguments")
        hyp = self._hyp()
        base_sc = self.eng.normalize(hyp)
        cur = self.eng.raw_access(base_sc, self.ledger)
        options, afters = [], {}
        for cid in ids:
            try:
                res = self.eng.apply_result(base_sc, cid, base_sc.key(), self.ledger)
            except ValidationError as e:
                options.append({"candidate_id": cid, "error": str(e), "code": e.code})
                continue
            after = self.eng.raw_access(res["scenario"], self.ledger)
            afters[cid] = after
            c = res["candidate"]
            options.append({"candidate_id": cid, "action": c["label"], "effect": c["effect"],
                            "recovered_km": round(c["recovered_m"] / 1000, 2),
                            "zones_cut_off_by_it": [self.zname(z) for z in c["zones_lost"]]})
        valid = [o for o in options if "error" not in o]
        diffs = []
        if len(valid) >= 2:
            a_id, b_id = valid[0]["candidate_id"], valid[1]["candidate_id"]
            for z in self.net.zones:
                zid = z["id"]
                a, b = afters[a_id].get(zid), afters[b_id].get(zid)
                da, db_ = (a or {}).get("dist"), (b or {}).get("dist")
                if (da is None) != (db_ is None) or (da is not None and abs(da - db_) > 0.5):
                    diffs.append({"zone": z["name"],
                                  "option_a_km": round(da / 1000, 2) if da is not None else "cut off",
                                  "option_a_route_crossings": self._route_crossings((a or {}).get("route")),
                                  "option_b_km": round(db_ / 1000, 2) if db_ is not None else "cut off",
                                  "option_b_route_crossings": self._route_crossings((b or {}).get("route")),
                                  "now_km": round(cur[zid]["dist"] / 1000, 2) if zid in cur else "cut off"})
            diffs.sort(key=lambda d: -abs((d["option_a_km"] if isinstance(d["option_a_km"], float) else 99) -
                                          (d["option_b_km"] if isinstance(d["option_b_km"], float) else 99)))
        winner = max(valid, key=lambda o: (-len(o["zones_cut_off_by_it"]), o["recovered_km"])) if valid else None
        return {"evaluated_for": "investigated hypothesis" if self.hypothesis else "current scenario",
                "options": options, "per_zone_differences": diffs[:8], "winner": winner["action"] if winner else None,
                "summary": (f"compared {len(valid)} options; {winner['action']} wins ({winner['recovered_km']} km); "
                            f"{len(diffs)} zone(s) differ") if winner else "no comparable options"}

    def t_trace_route(self, zone: str) -> dict:
        zid = self._zone_id(zone)
        if not zid:
            raise ToolError(f"unknown neighbourhood {zone!r}", "unknown_zone")
        now = self.eng.raw_access(self.work, self.ledger).get(zid)
        out = {"zone": self.zname(zid)}
        for label, acc in (("current", now), ("hypothesis", self.eng.raw_access(self.hypothesis, self.ledger).get(zid)
                                               if self.hypothesis else None)):
            if label == "hypothesis" and not self.hypothesis:
                continue
            if acc is None:
                out[label] = {"status": "cut off"}
            else:
                roads = []
                for e in acc["route"]:
                    n = self.net._by_eid.get(e.replace("rev:", ""), {}).get("name") or "unnamed road"
                    if not roads or roads[-1] != n:
                        roads.append(n)
                out[label] = {"km": round(acc["dist"] / 1000, 2), "nearest_help": (acc["help"] or ["?"])[0],
                              "crossings": self._route_crossings(acc["route"]), "roads": roads[:12],
                              "segments": len(acc["route"])}
        out["summary"] = f"route of {self.zname(zid)}: " + "; ".join(
            f"{k} {v.get('km', 'cut off')} km" for k, v in out.items() if isinstance(v, dict))
        return out

    def t_recall_memory(self, location: str | None = None, limit: int = 4) -> dict:
        if not self.mem:
            return {"findings": [], "summary": "memory unavailable"}
        ids = None
        text = location or self.intent.text
        r = resolve_location(self.db, self.net, text, self.ledger) if text else {"status": "not_found"}
        if r["status"] == "resolved" and r["resolved"]["kind"] == "crossing":
            ids = [r["resolved"]["id"]]
        found, seen = [], set()
        for f in self.mem.recall(ids, 24, self.ledger):          # newest first; identical findings shown once
            sig = (f["kind"], f["summary"], f["scenario_key"])
            if sig not in seen:
                seen.add(sig)
                found.append(f)
        found = found[:max(1, min(int(limit or 4), 12))]
        out = []
        cur_key = self.work.key()
        for f in found:
            item = {"finding_id": f["finding_id"], "kind": f["kind"], "when": f["ts"], "question": f["question"],
                    "investigation_id": f["investigation_id"], "places": f["places"],
                    "scenario_then": f["investigation_scenario"], "hypothesis_label": (f["hypothesis"] or {}).get("label"),
                    "then": f["summary"], "assumptions": f["assumptions"],
                    "same_scenario_as_now": f["scenario_key"] == cur_key}
            item.update(self.recheck_finding(f))
            out.append(item)
        return {"findings": out, "summary": f"{len(out)} earlier finding(s) recalled" + (
            f" about {r['resolved']['name']}" if ids else "")}

    def recheck_finding(self, f: dict) -> dict:
        """Re-evaluate one stored finding against the CURRENT scenario (never trust the stored number)."""
        item: dict = {}
        cur_key = self.work.key()
        if f["kind"] == "disruption_impact":
            hyp = f["hypothesis"]
            now_sc = self._apply_delta(self.work, hyp)
            if now_sc.key() == self.work.key():
                item["now"] = "the hypothesised closure is already part of the current scenario"
                item["still_applies"] = "superseded (closure now applied)"
            else:
                det = self._impact_detail(self.work, now_sc)
                then_km = f["metrics"].get("added_km")
                already = [c for c in hyp.get("closed_carriageways", []) if c in self.work.closed_carriageways]
                if already:
                    item["already_in_current_scenario"] = [self.net.carriageway(c)["crossing_name"] + " " +
                                                           self.net.carriageway(c)["direction"] for c in already]
                item["now"] = (f"re-evaluated on the current scenario: +{det['added_km']} km, "
                               f"{det['zones_affected']} zone(s), cut off: {det['zones_cut_off'] or 'none'}"
                               + (f" (part of the hypothesis is already in the current scenario: "
                                  f"{', '.join(item['already_in_current_scenario'])})" if already else ""))
                item["then_added_km"], item["now_added_km"] = then_km, det["added_km"]
                item["still_applies"] = ("yes" if then_km is not None and abs(det["added_km"] - then_km) < 0.05
                                         else "changed")
        elif f["kind"] == "recovery_ranking":
            hyp = f.get("hypothesis") or {}
            on_hyp = bool(hyp.get("closed_carriageways") or hyp.get("closed_segments") or hyp.get("constraints")) and \
                self._apply_delta(self.work, hyp).key() == f["scenario_key"]
            item["still_applies"] = ("yes (same scenario)" if f["scenario_key"] == cur_key else
                                     "applies to the hypothesis on the current scenario (not applied)" if on_hyp else
                                     "stale: computed for a different scenario; re-rank before relying on it")
        return item

    def t_find_critical_roads(self, top_links: int = 20) -> dict:
        top_links = max(5, min(int(top_links or 20), 30))
        v = self.eng.vulnerability(self.work, top_links, 5, self.ledger)
        res = v["results"]

        def row(r):
            return {"road": r["label"], "category": r["category"], "added_km": round(r["added_m"] / 1000, 2),
                    "zones_cut_off": [self.zname(z) for z in r["zones_cut_off"]], "zones_longer": r["zones_longer"],
                    "routes_using_now": len(r["routes_using_now"]), "flags": r["flags"],
                    "worst_zone": [self.zname(r["worst"][0]), round(r["worst"][1] / 1000, 2)] if r["worst"] else None}
        top = [row(r) for r in res[:5]]
        low = [row(r) for r in res if "prominent_low_impact" in r["flags"]][:4]
        hidden = [row(r) for r in res if "less_obvious_high_impact" in r["flags"]][:4]
        if self.mem:
            self.mem.finding(self._investigation(), "vulnerability", (f"Most critical: {top[0]['road']} (+{top[0]['added_km']} km)"
                             if top else "no candidates"), self.work.key(), {"analysis": "vulnerability"},
                             {"top": top[:3], "evaluated": v["candidate_set"]["evaluated"]}, [], [],
                             ["bounded candidate set, not exhaustive"], self.ledger)
        return {"evaluated": v["candidate_set"]["evaluated"], "exhaustive": False, "criteria": v["criteria"],
                "most_critical": top, "prominent_but_low_impact": low, "less_obvious_but_high_impact": hidden,
                "elapsed_ms": v["elapsed_ms"],
                "summary": f"{v['candidate_set']['evaluated']} candidate failures evaluated on graph copies; most critical: "
                           f"{top[0]['road']} (+{top[0]['added_km']} km)" if top else "no candidates"}
