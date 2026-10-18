"""Durable, graph-backed investigation memory.

Stored in its own FalkorDB graph (`contraflow_memory`), separate from the road graph, so rebuilding the base
graph never erases it. Every write is also appended to `data/memory/memory.jsonl`; if the memory graph is empty at
startup (e.g. a fresh database container) it is rebuilt by replaying that log.

Provenance is explicit on every node:
  (:Investigation {id, ts, question, intent, scenario_key, scenario, model_fp, mode})
  (:Report {id, text, ts, provenance:'reported_unverified'})-[:IN]->(:Investigation)
  (:Report)-[:REFERS_TO {resolution:'resolved'|'ambiguous'|'confirmed_by_user', method}]->(:Place)
  (:Place {id, kind:'crossing'|'zone'|'road_section', name})          references into verified OSM topology
  (:Finding {id, kind, provenance:'computed', scenario_key, hypothesis, metrics, summary})-[:IN]->(:Investigation)
  (:Finding)-[:CONCERNS]->(:Place)    (:Finding)-[:AFFECTS {delta_m, status}]->(:Place {kind:'zone'})
  (:Assumption {id, text, provenance:'modeled_assumption'})<-[:ASSUMES]-(:Finding)
  (:Proposal {id, kind, label, scenario_key, state})-[:IN]->(:Investigation)
  (:Decision {id, state:'applied'|'rejected', ts, provenance:'user_approved'|'user_rejected'})-[:ON]->(:Proposal)
A reported closure is never stored as a fact: it is a Report plus, at most, a computed Finding about a hypothesis.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from .graphdb import DB, Ledger

MEMORY = "contraflow_memory"
LOG = Path(__file__).resolve().parents[1] / "data" / "memory" / "memory.jsonl"
_lock = threading.Lock()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Memory:
    def __init__(self, db: DB, log_path: Path = LOG, graph: str = MEMORY):
        self.db, self.log, self.name = db, log_path, graph
        self.g = db.graph(graph)

    # ------------------------------------------------------------------ durability
    def ensure(self) -> dict:
        """Create indexes; replay the log into an empty memory graph (e.g. after a fresh DB container)."""
        exists = self.name in self.db.list_graphs()
        n = 0
        if exists:
            try:
                n = self.g.q("mem", "MATCH (i:Investigation) RETURN count(i)")[0][0]
            except Exception:
                n = 0
        replayed = 0
        if n == 0 and self.log.exists():
            for line in self.log.read_text().splitlines():
                if line.strip():
                    op = json.loads(line)
                    self._apply(op["cypher"], op["params"])
                    replayed += 1
        for ddl in ("CREATE INDEX FOR (i:Investigation) ON (i.id)", "CREATE INDEX FOR (p:Place) ON (p.id)",
                    "CREATE INDEX FOR (f:Finding) ON (f.id)", "CREATE INDEX FOR (p:Proposal) ON (p.id)",
                    "CREATE INDEX FOR (r:Report) ON (r.id)"):
            try:
                self.g.q("mem_ddl", ddl)
            except Exception:
                pass
        return {"graph": self.name, "existed": exists, "replayed_ops": replayed,
                "investigations": self.g.q("mem", "MATCH (i:Investigation) RETURN count(i)")[0][0]}

    def _apply(self, cypher: str, params: dict, ledger: Ledger | None = None):
        return self.g.q("memory_write", cypher, params, ledger)

    def _write(self, cypher: str, params: dict, ledger: Ledger | None = None):
        with _lock:
            res = self._apply(cypher, params, ledger)
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open("a") as f:
                f.write(json.dumps({"cypher": cypher, "params": params}) + "\n")
        return res

    # ------------------------------------------------------------------ writes
    def start(self, question: str, intent: dict, scenario: dict, scenario_key: str, model_fp: str, mode: str,
              ledger: Ledger | None = None) -> str:
        iid = _id("inv")
        self._write("CREATE (:Investigation {id: $id, ts: $ts, seq: $seq, question: $q, intent: $intent, scenario_key: $sk, "
                    "scenario: $sc, model_fp: $fp, mode: $mode})",
                    {"id": iid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "seq": time.time_ns(), "q": question[:600],
                     "intent": json.dumps(intent), "sk": scenario_key, "sc": json.dumps(scenario), "fp": model_fp,
                     "mode": mode}, ledger)
        return iid

    def place(self, pid: str, kind: str, name: str, ledger: Ledger | None = None):
        self._write("MERGE (p:Place {id: $id}) ON CREATE SET p.kind = $k, p.name = $n", {"id": pid, "k": kind, "n": name},
                    ledger)

    def report(self, iid: str, text: str, refs: list[dict], ledger: Ledger | None = None, claim: str = "",
               rid: str | None = None) -> str:
        rid = rid or _id("rep")
        self._write("MATCH (i:Investigation {id: $iid}) CREATE (:Report {id: $id, text: $t, ts: $ts, seq: $seq, claim: $c, "
                    "provenance: 'reported_unverified'})-[:IN]->(i)",
                    {"iid": iid, "id": rid, "t": text[:600], "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "seq": time.time_ns(), "c": claim},
                    ledger)
        for r in refs:
            self.place(r["id"], r["kind"], r.get("name", r["id"]), ledger)
            self._write("MATCH (rep:Report {id: $rid}), (p:Place {id: $pid}) "
                        "CREATE (rep)-[:REFERS_TO {resolution: $res, method: $m}]->(p)",
                        {"rid": rid, "pid": r["id"], "res": r.get("resolution", "resolved"), "m": r.get("method", "")},
                        ledger)
        return rid

    def finding(self, iid: str, kind: str, summary: str, scenario_key: str, hypothesis: dict, metrics: dict,
                concerns: list[dict], affects: list[dict], assumptions: list[str], ledger: Ledger | None = None) -> str:
        fid = _id("fnd")
        self._write("MATCH (i:Investigation {id: $iid}) CREATE (:Finding {id: $id, kind: $k, summary: $s, "
                    "provenance: 'computed', scenario_key: $sk, hypothesis: $h, metrics: $m, ts: $ts, seq: $seq})-[:IN]->(i)",
                    {"iid": iid, "id": fid, "k": kind, "s": summary[:800], "sk": scenario_key, "h": json.dumps(hypothesis),
                     "m": json.dumps(metrics), "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "seq": time.time_ns()}, ledger)
        for c in concerns:
            self.place(c["id"], c["kind"], c.get("name", c["id"]), ledger)
            self._write("MATCH (f:Finding {id: $fid}), (p:Place {id: $pid}) CREATE (f)-[:CONCERNS]->(p)",
                        {"fid": fid, "pid": c["id"]}, ledger)
        for a in affects:
            self.place(a["id"], "zone", a.get("name", a["id"]), ledger)
            self._write("MATCH (f:Finding {id: $fid}), (p:Place {id: $pid}) "
                        "CREATE (f)-[:AFFECTS {delta_m: $d, status: $st}]->(p)",
                        {"fid": fid, "pid": a["id"], "d": a.get("delta_m"), "st": a.get("status", "longer")}, ledger)
        for t in assumptions:
            self._write("MATCH (f:Finding {id: $fid}) CREATE (f)-[:ASSUMES]->(:Assumption {id: $id, text: $t, "
                        "provenance: 'modeled_assumption'})", {"fid": fid, "id": _id("asm"), "t": t[:300]}, ledger)
        return fid

    def proposal(self, iid: str, pid: str, kind: str, label: str, scenario_key: str, ledger: Ledger | None = None):
        self._write("MATCH (i:Investigation {id: $iid}) CREATE (:Proposal {id: $id, kind: $k, label: $l, "
                    "scenario_key: $sk, state: 'proposed'})-[:IN]->(i)",
                    {"iid": iid, "id": pid, "k": kind, "l": label[:300], "sk": scenario_key}, ledger)

    def decide(self, proposal_id: str, state: str, ledger: Ledger | None = None) -> bool:
        if state not in ("applied", "rejected"):
            raise ValueError("state must be applied or rejected")
        rows = self.g.q("mem", "MATCH (p:Proposal {id: $id}) RETURN count(p)", {"id": proposal_id})
        if not rows or rows[0][0] == 0:
            return False
        self._write("MATCH (p:Proposal {id: $id}) SET p.state = $st CREATE (:Decision {id: $did, state: $st, ts: $ts, seq: $seq, "
                    "provenance: $prov})-[:ON]->(p)",
                    {"id": proposal_id, "st": state, "did": _id("dec"), "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "seq": time.time_ns(),
                     "prov": "user_approved" if state == "applied" else "user_rejected"}, ledger)
        return True

    # ------------------------------------------------------------------ reads
    def recall(self, place_ids: list[str] | None = None, limit: int = 5, ledger: Ledger | None = None) -> list[dict]:
        """Most recent computed findings, optionally about given crossings/zones, with their investigation context."""
        if place_ids:
            rows = self.g.q("memory_recall_by_place",
                            "MATCH (f:Finding)-[:CONCERNS|AFFECTS]->(p:Place) WHERE p.id IN $ids "
                            "MATCH (f)-[:IN]->(i:Investigation) "
                            "OPTIONAL MATCH (f)-[:ASSUMES]->(a:Assumption) OPTIONAL MATCH (f)-[:CONCERNS]->(pl:Place) "
                            "RETURN DISTINCT f.id, f.kind, f.summary, f.scenario_key, f.hypothesis, f.metrics, f.ts, "
                            "i.id, i.question, i.scenario_key, collect(DISTINCT a.text), i.scenario, collect(DISTINCT pl.name) "
                            "ORDER BY coalesce(f.seq, 0) DESC, f.ts DESC LIMIT $n",
                            {"ids": place_ids, "n": limit}, ledger)
        else:
            rows = self.g.q("memory_recall_recent",
                            "MATCH (f:Finding)-[:IN]->(i:Investigation) OPTIONAL MATCH (f)-[:ASSUMES]->(a:Assumption) "
                            "OPTIONAL MATCH (f)-[:CONCERNS]->(pl:Place) "
                            "RETURN f.id, f.kind, f.summary, f.scenario_key, f.hypothesis, f.metrics, f.ts, "
                            "i.id, i.question, i.scenario_key, collect(DISTINCT a.text), i.scenario, collect(DISTINCT pl.name) "
                            "ORDER BY coalesce(f.seq, 0) DESC, f.ts DESC LIMIT $n",
                            {"n": limit}, ledger)
        out = []
        for r in rows:
            out.append({"finding_id": r[0], "kind": r[1], "summary": r[2], "scenario_key": r[3],
                        "hypothesis": json.loads(r[4] or "{}"), "metrics": json.loads(r[5] or "{}"), "ts": r[6],
                        "investigation_id": r[7], "question": r[8], "investigation_scenario_key": r[9],
                        "assumptions": [a for a in r[10] if a], "investigation_scenario": json.loads(r[11] or "{}"),
                        "places": [x for x in r[12] if x]})
        return out

    def reports(self, place_id: str | None = None, limit: int = 20) -> list[dict]:
        if place_id:
            rows = self.g.q("memory_reports", "MATCH (r:Report)-[l:REFERS_TO]->(p:Place) WHERE p.id = $id "
                            "RETURN r.id, r.text, r.ts, r.provenance, collect(p.id), collect(l.resolution), r.claim "
                            "ORDER BY coalesce(r.seq, 0) DESC, r.ts DESC LIMIT $n", {"id": place_id, "n": limit})
        else:
            rows = self.g.q("memory_reports", "MATCH (r:Report) OPTIONAL MATCH (r)-[l:REFERS_TO]->(p:Place) "
                            "RETURN r.id, r.text, r.ts, r.provenance, collect(p.id), collect(l.resolution), r.claim "
                            "ORDER BY coalesce(r.seq, 0) DESC, r.ts DESC LIMIT $n", {"n": limit})
        return [{"id": r[0], "text": r[1], "ts": r[2], "provenance": r[3], "targets": r[4], "resolutions": r[5],
                 "claim": r[6]} for r in rows]

    def attach_trace(self, iid: str, trace: dict, ledger: Ledger | None = None):
        """Persist what was actually executed for this investigation (tool sequence, results summaries, graph operations,
        who wrote the briefing), so the reasoning path survives the session."""
        self._write("MATCH (i:Investigation {id: $id}) SET i.trace = $t", {"id": iid, "t": json.dumps(trace, default=str)},
                    ledger)

    CASE_QUERY = 'MATCH (i:Investigation {id: $id}) OPTIONAL MATCH (r:Report)-[:IN]->(i) OPTIONAL MATCH (r)-[rl:REFERS_TO]->(rp:Place) WITH i, collect(DISTINCT [r.id, r.text, r.claim, r.provenance, rp.id, rp.name, rp.kind, rl.resolution, rl.method]) AS reports OPTIONAL MATCH (f:Finding)-[:IN]->(i) OPTIONAL MATCH (f)-[:ASSUMES]->(a:Assumption) OPTIONAL MATCH (f)-[:CONCERNS]->(cp:Place) WITH i, reports, f, collect(DISTINCT a.text) AS assumptions, collect(DISTINCT cp.name) AS concerns OPTIONAL MATCH (f)-[af:AFFECTS]->(zp:Place) WITH i, reports, f, assumptions, concerns, collect(DISTINCT [zp.name, af.delta_m, af.status]) AS affects WITH i, reports, collect([f.id, f.kind, f.summary, f.metrics, f.hypothesis, f.scenario_key, f.provenance, f.ts, assumptions, concerns, affects]) AS findings OPTIONAL MATCH (p:Proposal)-[:IN]->(i) OPTIONAL MATCH (d:Decision)-[:ON]->(p) RETURN i.id, i.ts, i.question, i.intent, i.scenario, i.scenario_key, i.model_fp, i.mode, i.trace, reports, findings, collect(DISTINCT [p.id, p.label, p.kind, p.state, d.provenance, d.ts])'

    def case_file(self, iid: str, ledger: Ledger | None = None) -> dict | None:
        """One multi-hop traversal: Investigation <- Report -> Place, <- Finding -> (Assumption, Place, zone), <- Proposal <- Decision."""
        rows = self.g.q("memory_case_file", self.CASE_QUERY, {"id": iid}, ledger)
        if not rows or rows[0][0] is None:
            return None
        (i_id, ts, question, intent, scenario, skey, fp, mode, trace, reports, findings, props) = rows[0]
        rep = [dict(zip(("id", "text", "claim", "provenance", "place_id", "place", "place_kind", "resolution", "method"), r))
               for r in reports if r[0]]
        fnd = []
        for f in findings:
            if not f[0]:
                continue
            fnd.append({"id": f[0], "kind": f[1], "summary": f[2], "metrics": json.loads(f[3] or "{}"),
                        "hypothesis": json.loads(f[4] or "{}"), "scenario_key": f[5], "provenance": f[6], "ts": f[7],
                        "assumptions": [a for a in f[8] if a], "concerns": [c for c in f[9] if c],
                        "affects": [{"zone": z[0], "delta_m": z[1], "status": z[2]} for z in f[10] if z[0]]})
        fnd.sort(key=lambda x: x["ts"] or "")
        return {"investigation_id": i_id, "ts": ts, "report_text": question, "intent": json.loads(intent or "{}"),
                "scenario": json.loads(scenario or "{}"), "scenario_key": skey, "model_fingerprint": fp, "mode": mode,
                "trace": json.loads(trace) if trace else None, "reports": rep, "findings": fnd,
                "proposals": [dict(zip(("id", "label", "kind", "state", "decision", "decided_at"), p)) for p in props if p[0]],
                "assembled_by": {"graph": self.name, "cypher": self.CASE_QUERY}}

    def proposals_by_investigation(self, ids: list[str]) -> dict[str, list[dict]]:
        rows = self.g.q("memory_proposals", "MATCH (p:Proposal)-[:IN]->(i:Investigation) WHERE i.id IN $ids "
                        "RETURN i.id, p.label, p.state ORDER BY coalesce(p.seq, 0)", {"ids": ids})
        out: dict[str, list[dict]] = {}
        for iid, label, state in rows:
            out.setdefault(iid, []).append({"label": label, "state": state})
        return out

    def investigations(self, limit: int = 10) -> list[dict]:
        rows = self.g.q("memory_investigations",
                        "MATCH (i:Investigation) OPTIONAL MATCH (f:Finding)-[:IN]->(i) "
                        "OPTIONAL MATCH (p:Proposal)-[:IN]->(i) "
                        "RETURN i.id, i.ts, i.question, i.scenario_key, i.mode, count(DISTINCT f), "
                        "collect(DISTINCT [p.id, p.label, p.state]) ORDER BY coalesce(i.seq, 0) DESC, i.ts DESC LIMIT $n", {"n": limit})
        return [{"id": r[0], "ts": r[1], "question": r[2], "scenario_key": r[3], "mode": r[4], "findings": r[5],
                 "proposals": [p for p in r[6] if p and p[0]]} for r in rows]

    def stats(self) -> dict:
        q = lambda c: self.g.q("mem", c)[0][0]  # noqa: E731
        return {"investigations": q("MATCH (i:Investigation) RETURN count(i)"), "reports": q("MATCH (r:Report) RETURN count(r)"),
                "findings": q("MATCH (f:Finding) RETURN count(f)"), "proposals": q("MATCH (p:Proposal) RETURN count(p)"),
                "decisions": q("MATCH (d:Decision) RETURN count(d)")}
