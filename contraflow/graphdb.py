"""FalkorDB access layer: base-graph loading, scenario clones, and a query ledger.

Base graph `contraflow` (see docs/MODEL.md):
  (:J {id, x, y})
  (:J)-[:ROAD {eid, len, name, highway, oneway, crossing, carriageway}]->(:J)
  (:J {id:'__HUB_A__' | '__HUB_B__'})  virtual hub per assistance-point rule
  (:J)-[:ROAD {eid:'hub:<rule>:<node>', len:0, virtual:true}]->(hub)
  (:Zone {id, name, lat, lon, snap_m})-[:ANCHORED_AT]->(:J)
  (:Help {id, name, lat, lon, rules})-[:ANCHORED_AT]->(:J)
  (:Crossing {id, name, roads})-[:HAS_CARRIAGEWAY]->(:Carriageway {id, direction, eids, oneway, lanes})
  (:Carriageway)-[:FROM]->(:J), (:Carriageway)-[:TO]->(:J)
  (:Zone)-[:NEAR {m}]->(:Crossing)
  (:Report {id, text, ts, status, source})-[:REFERS_TO {method, score}]->(:Crossing|:Carriageway|:Zone)
Full-text indexes: Crossing(name, roads), Zone(name), ROAD(name).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager

from falkordb import FalkorDB

from .model import Network, RULES, haversine

log = logging.getLogger("contraflow")
BASE = "contraflow"
SCENARIO_PREFIX = "cf_sc_"
BATCH = 2000


def hub(rule: str) -> str:
    return f"__HUB_{rule}__"


SCHEMA_VERSION = "4"


def model_fingerprint(net: Network) -> str:
    """Content hash of everything loaded into the graph (ids, geometry, lengths, attributes, zones, hospitals,
    crossings) plus the schema version. Any change forces a reload; counts alone are not trusted."""
    import hashlib
    import json
    payload = {"schema": SCHEMA_VERSION, "junctions": net.junctions, "segments": net.segments, "zones": net.zones,
               "help": net.help_points, "crossings": net.crossings, "rules": sorted(RULES)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def connect_db(attempts: int = 60, delay: float = 1.0, **kw) -> "DB":
    """Startup readiness: retry until FalkorDB accepts connections and answers PING."""
    last = None
    for i in range(attempts):
        try:
            db = DB(**kw)
            db.client.connection.ping()
            return db
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("FalkorDB not ready (attempt %d/%d): %s", i + 1, attempts, e)
            time.sleep(delay)
    raise RuntimeError(f"FalkorDB unreachable after {attempts} attempts: {last}")


class Ledger:
    """Records every FalkorDB call made while serving one request."""

    def __init__(self):
        self.entries: list[dict] = []

    def add(self, graph: str, op: str, cypher: str, params: dict | None, ms: float, rows: int):
        short = {k: (v if not isinstance(v, list) or len(v) <= 6 else v[:6] + [f"... +{len(v) - 6}"])
                 for k, v in (params or {}).items()}
        self.entries.append({"graph": graph, "op": op, "cypher": " ".join(cypher.split()), "params": short,
                             "ms": round(ms, 2), "rows": rows})

    def summary(self) -> dict:
        ops: dict[str, dict] = {}
        for e in self.entries:
            o = ops.setdefault(e["op"], {"count": 0, "ms": 0.0, "example": e["cypher"]})
            o["count"] += 1
            o["ms"] = round(o["ms"] + e["ms"], 2)
        return {"queries": len(self.entries), "total_ms": round(sum(e["ms"] for e in self.entries), 2),
                "by_op": ops, "entries": self.entries[:400]}


class Graph:
    """Thin wrapper so every query is timed and recorded in the active ledger."""

    def __init__(self, db: "DB", name: str):
        self.db, self.name, self.g = db, name, db.client.select_graph(name)

    def q(self, op: str, cypher: str, params: dict | None = None, ledger: Ledger | None = None):
        t0 = time.perf_counter()
        res = self.g.query(cypher, params or {})
        ms = (time.perf_counter() - t0) * 1000
        if ledger is not None:
            ledger.add(self.name, op, cypher, params, ms, len(res.result_set))
        return res.result_set


class DB:
    def __init__(self, host: str | None = None, port: int | None = None):
        # Optional credentials for a managed/remote FalkorDB (never hard-coded; read from the environment).
        self.client = FalkorDB(username=os.environ.get("FALKORDB_USERNAME") or None,
                               password=os.environ.get("FALKORDB_PASSWORD") or None,
                               host=host or os.environ.get("FALKORDB_HOST", "localhost"),
                               port=int(port or os.environ.get("FALKORDB_PORT", 6379)))
        self._lock = threading.Lock()

    def graph(self, name: str = BASE) -> Graph:
        return Graph(self, name)

    def list_graphs(self) -> list[str]:
        return [g.decode() if isinstance(g, bytes) else g for g in self.client.connection.execute_command("GRAPH.LIST")]

    def delete(self, name: str):
        try:
            self.client.select_graph(name).delete()
        except Exception as e:  # noqa: BLE001
            if "Invalid graph operation on empty key" not in str(e) and "empty key" not in str(e):
                log.warning("could not delete graph %s: %s", name, e)

    def sweep_scenarios(self) -> int:
        n = 0
        for g in self.list_graphs():
            if g.startswith(SCENARIO_PREFIX):
                self.delete(g)
                n += 1
        return n

    @contextmanager
    def scenario_clone(self, ledger: Ledger | None = None):
        """GRAPH.COPY the base graph into an isolated scenario graph; always deleted afterwards."""
        name = SCENARIO_PREFIX + uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        self.client.connection.execute_command("GRAPH.COPY", BASE, name)
        if ledger is not None:
            ledger.add(name, "graph_copy", f"GRAPH.COPY {BASE} {name}", None, (time.perf_counter() - t0) * 1000, 0)
        try:
            yield self.graph(name)
        finally:
            t1 = time.perf_counter()
            self.delete(name)
            if ledger is not None:
                ledger.add(name, "graph_delete", f"GRAPH.DELETE {name}", None, (time.perf_counter() - t1) * 1000, 0)

    # ------------------------------------------------------------------ loading
    def load_base(self, net: Network, force: bool = False) -> dict:
        """(Re)create the base graph. Idempotent: skipped when the stored fingerprint matches."""
        g = self.graph(BASE)
        fp = model_fingerprint(net)
        if not force and BASE in self.list_graphs():
            try:
                cur = g.q("meta", "MATCH (m:Meta) RETURN m.fingerprint")
                if cur and cur[0][0] == fp:
                    return {"loaded": False, "fingerprint": fp}
            except Exception:
                pass
        self.delete(BASE)
        g = self.graph(BASE)
        t0 = time.perf_counter()
        g.q("ddl", "CREATE INDEX FOR (j:J) ON (j.id)")
        g.q("ddl", "CREATE INDEX FOR ()-[e:ROAD]-() ON (e.eid)")
        g.q("ddl", "CREATE INDEX FOR (z:Zone) ON (z.id)")
        g.q("ddl", "CREATE INDEX FOR (c:Crossing) ON (c.id)")
        g.q("ddl", "CREATE INDEX FOR (c:Carriageway) ON (c.id)")
        g.q("ddl", "CREATE INDEX FOR (r:Report) ON (r.id)")
        for i in range(0, len(net.junctions), BATCH):
            g.q("load", "UNWIND $rows AS r CREATE (j:J) SET j = r", {"rows": net.junctions[i:i + BATCH]})
        keys = ("eid", "len", "name", "highway", "oneway", "lanes", "crossing", "carriageway")
        for i in range(0, len(net.segments), BATCH):
            rows = [{"u": s["u"], "v": s["v"], "p": {k: s[k] for k in keys}} for s in net.segments[i:i + BATCH]]
            g.q("load", "UNWIND $rows AS r MATCH (a:J {id: r.u}), (b:J {id: r.v}) CREATE (a)-[e:ROAD]->(b) SET e = r.p",
                {"rows": rows})
        for rule in RULES:
            nodes = sorted({h["node"] for h in net.help_points if rule in h["rules"]})
            g.q("load", "CREATE (:J {id: $hub, virtual: true})", {"hub": hub(rule)})
            g.q("load", "UNWIND $ns AS n MATCH (a:J {id: n}), (h:J {id: $hub}) "
                        "CREATE (a)-[:ROAD {eid: 'hub:' + $rule + ':' + n, len: 0.0, virtual: true}]->(h)",
                {"ns": nodes, "hub": hub(rule), "rule": rule})
        g.q("load", "UNWIND $rows AS r MATCH (j:J {id: r.node}) "
                    "CREATE (z:Zone {id: r.id, name: r.name, lat: r.lat, lon: r.lon, snap_m: r.snap_m})-[:ANCHORED_AT]->(j)",
            {"rows": net.zones})
        g.q("load", "UNWIND $rows AS r MATCH (j:J {id: r.node}) "
                    "CREATE (h:Help {id: r.id, name: r.name, lat: r.lat, lon: r.lon, rules: r.rules})-[:ANCHORED_AT]->(j)",
            {"rows": net.help_points})
        for x in net.crossings:
            g.q("load", "CREATE (:Crossing {id: $id, name: $name, roads: $roads, lat: $lat, lon: $lon})",
                {"id": x["id"], "name": x["name"], "roads": ", ".join(x["roads"]), "lat": x["lat"], "lon": x["lon"]})
            for c in x["carriageways"]:
                first, last = net.seg(c["eids"][0]), net.seg(c["eids"][-1])
                g.q("load", "MATCH (x:Crossing {id: $x}), (a:J {id: $u}), (b:J {id: $v}) "
                            "CREATE (x)-[:HAS_CARRIAGEWAY]->(c:Carriageway {id: $id, direction: $dir, eids: $eids, "
                            "oneway: $oneway, lanes: $lanes}), (c)-[:FROM]->(a), (c)-[:TO]->(b)",
                    {"x": x["id"], "u": first["u"], "v": last["v"], "id": c["id"], "dir": c["direction"],
                     "eids": c["eids"], "oneway": c["oneway"], "lanes": ", ".join(c["lanes"])})
        near = [{"z": z["id"], "x": x["id"], "m": round(haversine(z["lat"], z["lon"], x["lat"], x["lon"]), 1)}
                for z in net.zones for x in net.crossings]
        g.q("load", "UNWIND $rows AS r MATCH (z:Zone {id: r.z}), (x:Crossing {id: r.x}) "
                    "CREATE (z)-[:NEAR {m: r.m}]->(x)", {"rows": [r for r in near if r["m"] <= 3000]})
        g.q("ddl", "CREATE FULLTEXT INDEX FOR (c:Crossing) ON (c.name, c.roads)")
        g.q("ddl", "CREATE FULLTEXT INDEX FOR (z:Zone) ON (z.name)")
        g.q("ddl", "CREATE FULLTEXT INDEX FOR ()-[e:ROAD]-() ON (e.name)")
        g.q("load", "CREATE (:Meta {fingerprint: $fp, attribution: $a})", {"fp": fp, "a": net.meta["attribution"]})
        return {"loaded": True, "fingerprint": fp, "seconds": round(time.perf_counter() - t0, 3)}
