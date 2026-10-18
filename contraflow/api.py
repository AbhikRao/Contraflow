"""Contraflow HTTP API (FastAPI). Run: .venv/bin/uvicorn contraflow.api:app --port 8000"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .engine import CONSTRAINT_KINDS, OBJECTIVE, Engine, Scenario, ValidationError
from .graphdb import Ledger, connect_db
from .model import RULES, ROOT, build, geojson

@asynccontextmanager
async def lifespan(_app):
    state()          # load the base graph and baselines before serving
    yield


app = FastAPI(title="Contraflow", version="1.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2000)


class ConstraintIn(BaseModel):
    kind: str
    target: str


class ScenarioIn(BaseModel):
    rule: str = "B"
    closed_carriageways: list[str] = Field(default_factory=list)
    closed_segments: list[str] = Field(default_factory=list)
    reversed_segments: list[str] = Field(default_factory=list)
    constraints: list[ConstraintIn] = Field(default_factory=list)

    def to_engine(self) -> Scenario:
        return Scenario.from_dict(self.model_dump())


class InterventionsIn(BaseModel):
    scenario: ScenarioIn
    include_contraflow: bool = True


class ApplyIn(BaseModel):
    """Only the candidate's id and the scenario key it was ranked for are read. Any other fields a client sends
    (predictions, result scenarios, keys) are ignored: the server recomputes everything."""
    current: ScenarioIn
    candidate_id: str = Field(min_length=1, max_length=200)
    base_key: str = Field(min_length=1, max_length=64)


class AgentIn(BaseModel):
    message: str = Field(min_length=1, max_length=600)
    scenario: ScenarioIn
    history: list[dict] = Field(default_factory=list)
    mode: str | None = None          # "llm" | "deterministic" | None (auto)
    confirm: dict | None = None      # user's choice for a pending clarification


@lru_cache(maxsize=1)
def state():
    net = build()
    db = connect_db()
    load = db.load_base(net)
    swept = db.sweep_scenarios()
    import os
    from pathlib import Path as _P
    from .memory import LOG, MEMORY, Memory
    memory = Memory(db, _P(os.environ.get("CONTRAFLOW_MEMORY_LOG", str(LOG))),
                    os.environ.get("CONTRAFLOW_MEMORY_GRAPH", MEMORY))
    mem_status = memory.ensure()
    eng = Engine(net, db)
    for r in RULES:
        eng.baseline(r)
    return {"net": net, "db": db, "engine": eng, "load": load, "swept": swept, "memory": memory,
            "memory_status": mem_status}


def _err(e: ValidationError):
    raise HTTPException(status_code=422, detail=e.to_dict())


@app.get("/api/health")
def health():
    s = state()
    return {"ok": True, "frontend_built": _frontend_built(), "falkordb_graphs": s["db"].list_graphs(), "load": s["load"]}


@lru_cache(maxsize=1)
def _layers() -> bytes:
    return json.dumps(geojson(state()["net"]), separators=(",", ":")).encode()


@app.get("/api/layers")
def layers():
    return JSONResponse(content=json.loads(_layers()), headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/meta")
def meta():
    net = state()["net"]
    return {
        "name": "Contraflow", "subtitle": "Graph-Native Road Access & Recovery Planning",
        "objective": OBJECTIVE, "rules": RULES, "constraint_kinds": CONSTRAINT_KINDS,
        "crossings": net.crossings and [{k: x[k] for k in ("id", "name", "roads", "lat", "lon")} |
                                        {"carriageways": [{k: c[k] for k in ("id", "direction", "eids", "oneway", "lanes", "len")}
                                                          for c in x["carriageways"]]} for x in net.crossings],
        "zones": [{k: z[k] for k in ("id", "name", "lat", "lon", "snap_m", "node")} for z in net.zones],
        "excluded_zones": net.excluded_zones,
        "help_points": net.help_points,
        "network": {"junctions": len(net.junctions), "segments": len(net.segments),
                    "oneway_segments": sum(s["oneway"] for s in net.segments)},
        "source": net.meta,
    }


@app.post("/api/evaluate")
def evaluate(sc: ScenarioIn):
    try:
        return state()["engine"].evaluate(sc.to_engine(), Ledger())
    except ValidationError as e:
        _err(e)


@app.post("/api/interventions")
def interventions(body: InterventionsIn):
    try:
        return state()["engine"].interventions(body.scenario.to_engine(), Ledger(), body.include_contraflow)
    except ValidationError as e:
        _err(e)


class VulnIn(BaseModel):
    scenario: ScenarioIn
    top_links: int = Field(default=20, ge=0, le=40)


@app.post("/api/vulnerability")
def vulnerability(body: VulnIn):
    """Bounded network-vulnerability analysis on the given scenario (each candidate failure on its own graph copy)."""
    try:
        return state()["engine"].vulnerability(body.scenario.to_engine(), body.top_links, 5, Ledger())
    except ValidationError as e:
        _err(e)


@app.post("/api/apply_candidate")
def apply_candidate(body: ApplyIn):
    """Apply a ranked candidate exactly as it was evaluated (stale or mismatched candidates are refused)."""
    try:
        out = state()["engine"].apply_result(body.current.to_engine(), body.candidate_id, body.base_key, Ledger())
        return {"scenario": out["scenario"].to_dict(), "scenario_key": out["scenario"].key(),
                "candidate": out["candidate"], "computed_by": "server"}
    except ValidationError as e:
        raise HTTPException(status_code=409 if e.code == "stale_candidate" else 422, detail=e.to_dict())


@app.post("/api/agent")
def agent(body: AgentIn):
    from .agent.runtime import run_agent
    try:
        return run_agent(state(), body.message, body.scenario.to_engine(), body.history, body.mode, body.confirm)
    except ValidationError as e:
        _err(e)


@app.post("/api/agent/stream")
def agent_stream(body: AgentIn):
    """Server-sent events: each real tool step as it finishes, then the final reply."""
    import queue
    import threading
    from fastapi.responses import StreamingResponse
    from .agent.runtime import run_agent
    q: queue.Queue = queue.Queue()

    def on_step(step):
        q.put({"type": "step", "step": {k: step.get(k) for k in ("kind", "tool", "args", "result_summary", "ms", "queries",
                                                                 "text")}})

    def work():
        try:
            reply = run_agent(state(), body.message, body.scenario.to_engine(), body.history, body.mode, body.confirm,
                              on_step=on_step)
            q.put({"type": "reply", "reply": reply})
        except ValidationError as e:
            q.put({"type": "error", "detail": e.to_dict()})
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "detail": {"code": "agent_error", "message": str(e)[:300]}})
        q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen():
        yield ": stream open\n\n"
        while True:
            item = q.get()
            if item is None:
                break
            yield "data: " + json.dumps(item, default=str) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/agent/status")
def agent_status():
    from .agent.llm import llm_status
    return llm_status()


@app.get("/api/evidence")
def evidence(target: str | None = None):
    return {"reports": state()["memory"].reports(target)}


@app.get("/api/memory")
def memory_overview():
    m = state()["memory"]
    return {"stats": m.stats(), "investigations": m.investigations(12), "status": state()["memory_status"]}


class ReviewIn(BaseModel):
    scenario: ScenarioIn


@app.post("/api/memory/review")
def memory_review(body: ReviewIn):
    """Recent investigations grouped with their findings; every finding is re-evaluated against the GIVEN scenario so a
    historical result is never presented as current without a fresh computation."""
    from .agent.intent import parse_intent
    from .agent.tools import ToolBox
    st = state()
    try:
        sc = body.scenario.to_engine()
        st["engine"].validate(sc)
    except ValidationError as e:
        _err(e)
    tb = ToolBox(st, sc, None, user_msg="", intent=parse_intent("earlier finding"))
    found = tb.t_recall_memory(None, 12)["findings"]
    groups: dict[str, dict] = {}
    for f in found:
        g = groups.setdefault(f["investigation_id"], {"investigation_id": f["investigation_id"], "when": f["when"],
                                                      "report": f["question"], "places": [], "scenario_then":
                                                      f["scenario_then"], "findings": []})
        for p in f["places"]:
            if p not in g["places"]:
                g["places"].append(p)
        g["findings"].append({k: f.get(k) for k in ("kind", "then", "assumptions", "same_scenario_as_now", "now",
                                                    "still_applies", "then_added_km", "now_added_km",
                                                    "hypothesis_label", "already_in_current_scenario")})
    props = st["memory"].proposals_by_investigation(list(groups))
    for iid, g in groups.items():
        g["proposals"] = props.get(iid, [])
    return {"scenario_key": sc.key(), "stats": st["memory"].stats(), "investigations": list(groups.values())[:8]}


class CaseIn(BaseModel):
    scenario: ScenarioIn


@app.post("/api/case/{investigation_id}")
def case_file(investigation_id: str, body: CaseIn):
    """Evidence-backed case file for one investigation, assembled from the memory graph and re-checked against the
    given scenario. Read-only."""
    from .casefile import build_case
    if not investigation_id.startswith("inv_") or len(investigation_id) > 40:
        raise HTTPException(status_code=422, detail={"code": "bad_id"})
    try:
        sc = body.scenario.to_engine()
        state()["engine"].validate(sc)
    except ValidationError as e:
        _err(e)
    case = build_case(state(), investigation_id, sc)
    if case is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_investigation"})
    return case


@app.post("/api/case/{investigation_id}/robustness")
def case_robustness(investigation_id: str, body: CaseIn):
    """'Test assumptions': re-rank the case's recommendation under a bounded set of single-assumption variations, each on
    FalkorDB graph copies. Read-only; deterministic; not exhaustive."""
    from .casefile import case_robustness as run
    if not investigation_id.startswith("inv_") or len(investigation_id) > 40:
        raise HTTPException(status_code=422, detail={"code": "bad_id"})
    try:
        sc = body.scenario.to_engine()
        state()["engine"].validate(sc)
        out = run(state(), investigation_id, sc)
    except ValidationError as e:
        _err(e)
    if out is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_investigation"})
    return out


class DecisionIn(BaseModel):
    proposal_id: str = Field(min_length=1, max_length=80)
    state: str


@app.post("/api/memory/decision")
def memory_decision(body: DecisionIn):
    """Record the human's decision on an agent proposal (applied / rejected) as a Decision node."""
    try:
        ok = state()["memory"].decide(body.proposal_id, body.state)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"code": "invalid_state", "message": str(e)})
    if not ok:
        raise HTTPException(status_code=404, detail={"code": "unknown_proposal"})
    return {"ok": True}


WEB = (ROOT / "web" / "dist").resolve()


def _frontend_built() -> bool:
    return (WEB / "index.html").is_file()


def _static_target(path: str) -> Path | None:
    """Resolve a request path to a file strictly inside WEB, or None. Rejects traversal after decoding."""
    if "\x00" in path or "\\" in path:
        return None
    target = (WEB / path).resolve()
    if not target.is_relative_to(WEB):
        return None
    return target


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    """Serve the built UI. Only files inside web/dist are reachable; unknown extension-less
    paths get index.html (SPA routing); everything else is 404."""
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="unknown API route")
    if not _frontend_built():
        return JSONResponse(status_code=503, content={
            "detail": "Frontend bundle not built. Run: (cd web && npm ci && npm run build), or use the Vite dev server."})
    target = _static_target(path) if path else WEB / "index.html"
    if target is None:
        raise HTTPException(status_code=404, detail="not found")
    if target.is_file():
        return FileResponse(target)
    if "." in Path(path).name or ".." in Path(path).parts:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(WEB / "index.html")
