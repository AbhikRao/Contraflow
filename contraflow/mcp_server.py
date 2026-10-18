"""Contraflow MCP server (stdio, JSON-RPC 2.0, newline-delimited — the MCP stdio transport).

Lets any MCP-capable agent (Claude Desktop, Claude Code, Cursor, …) select Contraflow's graph tools and read the
investigation memory the in-app agent writes. Deliberately READ-ONLY: no tool can propose, apply or delete anything;
scenario changes stay behind the human Apply button in the web UI. Every tool runs the same guarded code paths as the
app (intent parsing, location resolution with explicit ambiguity, engine validation, graph copies).

Run:  .venv/bin/python -m contraflow.mcp_server        (FalkorDB must be running; no extra dependency)
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER = {"name": "contraflow", "title": "Contraflow — graph-native road access & recovery", "version": "1.0.0"}
INSTRUCTIONS = ("Tabletop planning sandbox on Chennai's Adyar road graph in FalkorDB. Metric: modeled shortest directed road "
                "distance from 45 neighbourhood anchors to their nearest assistance point (rule B: 4 public hospitals, rule A: "
                "37 general hospitals) — not travel time, capacity or safety. All tools are read-only; reports are treated as "
                "unverified; changes can only be applied by a human in the web UI.")
RULE = {"type": "string", "enum": ["A", "B"], "default": "B", "description": "assistance-point rule"}
TOOLS = [
    {"name": "investigate_report", "title": "Investigate an unverified road report",
     "description": "Investigate a reported or hypothetical road disruption (free text, e.g. 'Kotturpuram bridge may be blocked, "
                    "keeping Maraimalai Adigal southbound reserved for ambulances'). Resolves the location on the graph (asks "
                    "when ambiguous), evaluates the closure on a graph copy, assumes a stated constraint for analysis only, "
                    "ranks and compares repairs, and stores the investigation in graph memory. Returns the briefing, the "
                    "investigation id and the executed steps. Never applies anything.",
     "inputSchema": {"type": "object", "properties": {"report": {"type": "string", "minLength": 3, "maxLength": 600},
                                                      "rule": RULE}, "required": ["report"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
    {"name": "evaluate_closures", "title": "Evaluate closures",
     "description": "Modeled impact of closing the named river crossings (each resolved on the graph; ambiguous names are "
                    "returned as candidates, never guessed). Optional direction applies to every named crossing.",
     "inputSchema": {"type": "object", "properties": {
         "crossings": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
         "direction": {"type": "string", "enum": ["northbound", "southbound", "all"], "default": "all"}, "rule": RULE},
         "required": ["crossings"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "rank_repairs", "title": "Rank repairs",
     "description": "Rank every eligible reopening / contraflow repair for a scenario (each evaluated on its own graph copy), "
                    "optionally with crossings reserved for emergency vehicles (removed from public repairs).",
     "inputSchema": {"type": "object", "properties": {
         "closed_crossings": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
         "reserve_for_emergency": {"type": "array", "items": {"type": "string"}, "maxItems": 4}, "rule": RULE},
         "required": ["closed_crossings"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "recall_investigations", "title": "Recall investigations from graph memory",
     "description": "Earlier findings from the investigation-memory graph (optionally about one crossing), each RE-EVALUATED "
                    "now against the given scenario: still applies / changed / superseded / stale. Includes findings written "
                    "by the in-app agent.",
     "inputSchema": {"type": "object", "properties": {"about": {"type": "string"},
                                                      "closed_crossings": {"type": "array", "items": {"type": "string"}},
                                                      "rule": RULE}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "get_case_file", "title": "Get an evidence-backed case file",
     "description": "The case file for one investigation, assembled from FalkorDB memory by one multi-hop traversal: report and "
                    "uncertainty, resolved entities, scenario and fingerprint, constraint, assumptions, impact, alternatives, "
                    "recommendation and its basis, executed tools and graph operations, human decision, and a fresh re-check.",
     "inputSchema": {"type": "object", "properties": {"investigation_id": {"type": "string", "pattern": "^inv_[0-9a-f]{6,32}$"},
                                                      "closed_crossings": {"type": "array", "items": {"type": "string"}},
                                                      "rule": RULE}, "required": ["investigation_id"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "test_recommendation", "title": "Test a recommendation against changed assumptions",
     "description": "Does the best repair for a scenario still hold if ONE modeled assumption changes? Re-ranks the scenario "
                    "under a bounded, deterministic set of variations derived from it (the other assistance-point rule, each "
                    "closure direction of the recommended crossing, the recommended option unavailable, the busiest open "
                    "carriageway reserved for emergency vehicles), each on FalkorDB graph copies. Reports the best repair, "
                    "modeled impact and whether the original stays best, per variation. Not exhaustive, not a probability, "
                    "not a safety assessment.",
     "inputSchema": {"type": "object", "properties": {
         "closed_crossings": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
         "reserve_for_emergency": {"type": "array", "items": {"type": "string"}, "maxItems": 4}, "rule": RULE},
         "required": ["closed_crossings"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "access_sensitivity", "title": "Modeled road-access sensitivity",
     "description": "Bounded what-if scan: each of 35 declared candidate roads closed on its own graph copy. A modeled "
                    "sensitivity, not validated infrastructure criticality; the top link is sensitive to how a hospital is "
                    "attached to the graph.",
     "inputSchema": {"type": "object", "properties": {"rule": RULE, "top": {"type": "integer", "minimum": 1, "maximum": 10}},
                     "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]


class ToolFailure(Exception):
    pass


def _state() -> dict:
    """Same components as the web app, but WITHOUT sweeping scenario clones (the app may be using them)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from .engine import Engine
    from .graphdb import connect_db
    from .memory import LOG, MEMORY, Memory
    from .model import RULES, build
    net = build()
    db = connect_db()
    load = db.load_base(net)
    memory = Memory(db, Path(os.environ.get("CONTRAFLOW_MEMORY_LOG", str(LOG))), os.environ.get("CONTRAFLOW_MEMORY_GRAPH", MEMORY))
    memory.ensure()
    eng = Engine(net, db)
    for r in RULES:
        eng.baseline(r)
    return {"net": net, "db": db, "engine": eng, "load": load, "memory": memory, "mode_label": "mcp"}


class Server:
    def __init__(self):
        self._st: dict | None = None

    @property
    def st(self) -> dict:
        if self._st is None:
            self._st = _state()
        return self._st

    # ------------------------------------------------------------------ helpers
    def _crossing(self, name: str) -> str:
        from .evidence import resolve_location
        from .graphdb import Ledger
        st = self.st
        if name in st["net"]._x:
            return name
        r = resolve_location(st["db"], st["net"], name, Ledger())
        if r["status"] == "resolved" and r["resolved"]["kind"] == "crossing":
            return r["resolved"]["id"]
        cands = [c["name"] for c in r.get("candidates", []) if c.get("kind") == "crossing"]
        raise ToolFailure(f"could not resolve {name!r} to one river crossing ({r['status']}: {r.get('reason')})"
                          + (f"; candidates: {', '.join(cands)}" if cands else "") + ". Name the crossing explicitly.")

    def _scenario(self, args: dict, closed_key: str = "closed_crossings"):
        from .engine import Scenario
        net = self.st["net"]
        cws = [c["id"] for name in args.get(closed_key, []) or [] for c in net.crossing(self._crossing(name))["carriageways"]]
        return Scenario(args.get("rule", "B"), cws)

    # ------------------------------------------------------------------ tools
    def investigate_report(self, a: dict) -> tuple[str, dict]:
        from .agent.runtime import run_agent
        from .engine import Scenario
        r = run_agent(self.st, a["report"], Scenario(a.get("rule", "B")), [], "deterministic", None)
        out = {"briefing": r["message"], "investigation_id": r.get("investigation_id"), "intent": r["intent"],
               "clarification": r.get("clarification") and {"question": r["clarification"]["question"],
                                                             "options": [o["label"] for o in r["clarification"]["options"]]},
               "proposals_for_human_review": [p["label"] for p in r["proposals"]],
               "applied": False, "answered_by": r["mode"]["label"], "briefing_source": r.get("briefing_source"),
               "steps": [{"tool": s.get("tool"), "ok": s.get("kind") == "tool", "summary": s.get("result_summary")}
                         for s in r["steps"] if s.get("kind") in ("tool", "error")],
               "graph_operations": r.get("ledger", {}).get("queries")}
        text = r["message"] + (f"\n\nInvestigation id: {out['investigation_id']} (use get_case_file)" if out["investigation_id"] else "")
        if out["clarification"]:
            text += f"\n\nClarification needed: {out['clarification']['question']} Options: {', '.join(out['clarification']['options'])}"
        return text, out

    def evaluate_closures(self, a: dict) -> tuple[str, dict]:
        from .engine import Scenario
        st = self.st
        d = a.get("direction", "all")
        cws = []
        for name in a["crossings"]:
            x = st["net"].crossing(self._crossing(name))
            sel = [c["id"] for c in x["carriageways"] if d == "all" or c["direction"] == d]
            if not sel:
                raise ToolFailure(f"{x['name']} has no {d} carriageway")
            cws += sel
        ev = st["engine"].evaluate(Scenario(a.get("rule", "B"), cws))
        m = ev["metrics"]
        zn = {z["id"]: z["name"] for z in st["net"].zones}
        out = {"closed": cws, "added_km": round(m["total_delta_m"] / 1000, 4), "zones_affected": m["affected_zones"],
               "zones_cut_off": [zn[z] for z in m["cut_off"]], "baseline_total_km": round(ev["baseline_total_m"] / 1000, 2),
               "largest": m["largest"] and {"zone": zn[m["largest"]["zone"]], "km": round(m["largest"]["delta_m"] / 1000, 2)},
               "graph_operations": ev["ledger"]["queries"]}
        return (f"Closing {', '.join(cws)}: +{out['added_km']} km modeled distance across {out['zones_affected']} zone(s); "
                f"cut off: {out['zones_cut_off'] or 'none'}."), out

    def rank_repairs(self, a: dict) -> tuple[str, dict]:
        st = self.st
        sc = self._scenario(a)
        for name in a.get("reserve_for_emergency", []) or []:
            sc.constraints += [{"kind": "reserve_emergency", "target": c["id"]}
                               for c in st["net"].crossing(self._crossing(name))["carriageways"]]
        st["engine"].validate(sc)
        iv = st["engine"].interventions(sc)
        top = [{"action": c["label"], "effect": c["effect"], "recovered_km": round(c["recovered_m"] / 1000, 2),
                "share_pct": None if c["share_of_disruption"] is None else round(c["share_of_disruption"] * 100, 1)}
               for c in iv["ranking"][:6]]
        out = {"current_added_km": round(iv["current"]["total_delta_m"] / 1000, 2), "ranking": top,
               "excluded_by_constraints": [c["label"] for c in iv["excluded"]], "objective": iv["objective"],
               "graph_operations": iv["ledger"]["queries"]}
        best = next((t for t in top if t["effect"] in ("restores_access", "shorter")), None)
        return ((f"Best repair: {best['action']} — recovers {best['recovered_km']} km ({best['share_pct']}% of "
                 f"{out['current_added_km']} km)." if best else "No eligible repair improves access.")), out

    def test_recommendation(self, a: dict) -> tuple[str, dict]:
        st = self.st
        sc = self._scenario(a)
        for name in a.get("reserve_for_emergency", []) or []:
            sc.constraints += [{"kind": "reserve_emergency", "target": c["id"]}
                               for c in st["net"].crossing(self._crossing(name))["carriageways"]]
        r = st["engine"].robustness(sc)
        out = {"original": r["original"], "current_added_km": round(r["current"]["total_delta_m"] / 1000, 2),
               "variations": [{k: v.get(k) for k in ("changed", "status", "best", "original_still_best", "reason",
                                                     "zones_deciding", "rejected", "current")} for v in r["variations"]],
               "summary": r["summary"], "method": r["method"], "graph_operations": r["ledger"]["queries"],
               "elapsed_ms": r["elapsed_ms"]}
        lines = [f"- {v['changed']}: {v.get('status') or 'rejected'} — {v.get('reason') or v.get('rejected')}"
                 for v in r["variations"]]
        head = (f"Recommendation: {r['original']['label']} ({r['original']['recovered_km']} km). " if r["original"] else "")
        return head + r["summary"] + ".\n" + "\n".join(lines) + "\n" + r["method"], out

    def recall_investigations(self, a: dict) -> tuple[str, dict]:
        from .agent.intent import parse_intent
        from .agent.tools import ToolBox
        tb = ToolBox(self.st, self._scenario(a), None, user_msg="", intent=parse_intent("earlier finding"))
        r = tb.t_recall_memory(a.get("about"), 8)
        lines = [f"- [{f.get('still_applies', 'n/a')}] {f['then']} (investigation {f['investigation_id']}, {f['when']})"
                 for f in r["findings"]]
        return r["summary"] + ":\n" + "\n".join(lines), r

    def get_case_file(self, a: dict) -> tuple[str, dict]:
        from .casefile import build_case
        case = build_case(self.st, a["investigation_id"], self._scenario(a))
        if case is None:
            raise ToolFailure(f"no investigation {a['investigation_id']} in graph memory")
        return case["markdown"], case

    def access_sensitivity(self, a: dict) -> tuple[str, dict]:
        from .engine import Scenario
        v = self.st["engine"].vulnerability(Scenario(a.get("rule", "B")))
        n = int(a.get("top", 5))
        top = [{"road": r["label"], "added_km": round(r["added_m"] / 1000, 2), "category": r["category"], "flags": r["flags"],
                "assistance_points": r.get("assistance_points", [])} for r in v["results"][:n]]
        out = {"candidate_set": v["candidate_set"], "criteria": v["criteria"], "top": top,
               "caveat": "Bounded, modeled sensitivity; not validated infrastructure criticality. A link flagged "
                         "access_to_assistance_point mostly reflects how that hospital is attached to the graph."}
        return out["caveat"] + "\n" + "\n".join(f"- {t['road']}: +{t['added_km']} km ({t['category']})" for t in top), out

    # ------------------------------------------------------------------ JSON-RPC
    def handle(self, msg: dict) -> dict | None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:                       # notification (e.g. notifications/initialized): no response
            return None
        if method == "initialize":
            want = params.get("protocolVersion")
            return self._ok(mid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                                  "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                                  "instructions": INSTRUCTIONS})
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in {t["name"] for t in TOOLS}:
                return self._err(mid, -32602, f"unknown tool {name!r}")
            spec = next(t for t in TOOLS if t["name"] == name)
            missing = [k for k in spec["inputSchema"].get("required", []) if k not in args]
            extra = [k for k in args if k not in spec["inputSchema"]["properties"]]
            if missing or extra:
                return self._err(mid, -32602, f"invalid arguments for {name}: missing {missing}, unexpected {extra}")
            try:
                text, data = getattr(self, name)(args)
                return self._ok(mid, {"content": [{"type": "text", "text": text}],
                                      "structuredContent": json.loads(json.dumps(data, default=str)), "isError": False})
            except Exception as e:  # noqa: BLE001 — tool errors are reported to the model, per MCP
                msg_ = str(e) if isinstance(e, ToolFailure) else f"{type(e).__name__}: {e}"
                return self._ok(mid, {"content": [{"type": "text", "text": msg_}], "isError": True})
        return self._err(mid, -32601, f"method not found: {method}")

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main():
    out = sys.stdout
    sys.stdout = sys.stderr               # stdout carries protocol messages only; everything else goes to stderr
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            out.flush()
            continue
        try:
            resp = server.handle(msg)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            resp = Server._err(msg.get("id"), -32603, "internal error")
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
