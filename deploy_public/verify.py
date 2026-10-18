"""Real localhost/public-URL deployment checks; no developer graph is contacted.
Use --live for an actual Qwen stream and concurrent-request rejection check.
"""
import argparse
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

p = argparse.ArgumentParser()
p.add_argument("--url", default="http://127.0.0.1:8008")
p.add_argument("--live", action="store_true")
p.add_argument("--output", type=Path)
a = p.parse_args()
url = a.url.rstrip("/")
results = {"url": url, "started_epoch": time.time(), "checks": []}

def check(name, ok, detail=None):
    results["checks"].append({"name": name, "pass": bool(ok), "detail": detail})
    print(name, "PASS" if ok else "FAIL", detail or "", flush=True)
    assert ok, name

@contextmanager
def visitor():
    c = httpx.Client(base_url=url, headers={"Origin": url}, timeout=90)
    r = c.get("/")
    check("homepage", r.status_code == 200 and '<div id="root">' in r.text)
    check("cookie_http_only", "httponly" in r.headers.get("set-cookie", "").lower())
    check("security_headers", "frame-ancestors 'none'" in r.headers.get("content-security-policy", ""))
    if url.startswith("https://"):
        check("cookie_secure", "; secure" in r.headers.get("set-cookie", "").lower())
    try:
        yield c
    finally:
        c.close()

def post(c, path, body):
    r = c.post(path, json=body)
    check(path, r.status_code == 200, r.status_code)
    return r.json()

with visitor() as one, visitor() as two:
    h = one.get("/api/health").json()
    check("health_does_not_enumerate_graphs", h.get("ok") and "falkordb_graphs" not in h and "load" not in h)
    m = one.get("/api/meta").json()
    check("real_network", m["network"]["segments"] == 8458)
    check("visitor_A_starts_empty", one.get("/api/memory").json()["stats"]["investigations"] == 0)
    check("visitor_B_starts_empty", two.get("/api/memory").json()["stats"]["investigations"] == 0)
    for path in ("/docs", "/redoc", "/openapi.json", "/mcp", "/sse", "/api/mcp", "/api/query", "/.env", "/api/tags", "/v1/models"):
        check("blocked " + path, one.get(path).status_code == 404)
    check("csrf", one.post("/api/evaluate", json={"rule": "B"}, headers={"Origin": "https://attacker.invalid"}).status_code == 403)
    check("body_bound", one.post("/api/evaluate", content='{"x":"' + 'a' * 40000 + '"}', headers={"Content-Type": "application/json"}).status_code == 413)
    check("scenario_bound", one.post("/api/evaluate", json={"closed_segments": ["x"] * 129}).status_code == 422)
    check("no_cookie_denied", httpx.post(url + "/api/evaluate", json={"rule": "B"}, headers={"Origin": url}).status_code == 401)
    cws = {x["id"]: [c["id"] for c in x["carriageways"]] for x in m["crossings"]}
    allc = [c for cs in cws.values() for c in cs]
    for name, closed, expected in [("TVK", cws["thiru_vi_ka_bridge"], 0),
                                   ("MAB", cws["maraimalai_adigal_bridge"], 11920.5), ("ALL", allc, 13554.1)]:
        ev = post(one, "/api/evaluate", {"rule": "B", "closed_carriageways": closed})
        check("locked " + name, abs(ev["metrics"]["total_delta_m"] - expected) < .2, ev["metrics"])
    scenario = {"rule": "B", "closed_carriageways": allc}
    iv = post(one, "/api/interventions", {"scenario": scenario})
    top = iv["ranking"][0]
    check("locked recovery", top["share_of_disruption"] == .879)
    applied = post(one, "/api/apply_candidate", {"current": scenario, "candidate_id": top["id"], "base_key": top["base_key"]})
    check("server_application", applied["computed_by"] == "server")
    investigated = post(one, "/api/agent", {"message": "What if Maraimalai Adigal bridge were closed?", "scenario": {"rule": "B"}, "mode": "deterministic"})
    iid = investigated["investigation_id"]
    check("investigation_no_application", not investigated["proposals"])
    case = post(one, "/api/case/" + iid, {"scenario": {"rule": "B"}})
    check("case_available", bool(case))
    rob = post(one, "/api/case/" + iid + "/robustness", {"scenario": {"rule": "B"}})
    check("test_assumptions", rob.get("available") and len(rob.get("variations", [])) >= 3 and "ledger" not in rob,
          {"summary": rob.get("summary"), "elapsed_ms": rob.get("elapsed_ms")})
    check("cross_visitor_case_denied", two.post("/api/case/" + iid, json={"scenario": {"rule": "B"}}).status_code == 404)
    check("cross_visitor_memory_empty", two.get("/api/memory").json()["stats"]["investigations"] == 0)
    check("cross_visitor_evidence_empty", two.get("/api/evidence").json()["reports"] == [])
    proposal = post(one, "/api/agent", {"message": "Close Abraham bridge.", "scenario": {"rule": "B"}, "mode": "deterministic"})["proposals"][0]
    check("cross_visitor_decision_denied", two.post("/api/memory/decision", json={"proposal_id": proposal["memory_id"], "state": "rejected"}).status_code == 404)
    post(one, "/api/memory/decision", {"proposal_id": proposal["memory_id"], "state": "rejected"})
    status = one.get("/api/agent/status").json()
    check("model_status_private", status["model"] == "qwen2.5:7b" and "base_url" not in status)
    check("ollama_ready", status["llm_available"])
    if a.live:
        t = time.monotonic()
        steps = []
        reply = None
        concurrent_checked = False
        with one.stream("POST", "/api/agent/stream", json={"message": "Kotturpuram bridge northbound might be blocked. Which neighbourhoods are affected?", "scenario": {"rule": "B"}, "mode": "llm"}) as stream:
            check("SSE_content_type", stream.status_code == 200 and stream.headers["content-type"].startswith("text/event-stream"))
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event["type"] == "step":
                    steps.append({"seconds": round(time.monotonic() - t, 2), "kind": event["step"]["kind"], "tool": event["step"].get("tool")})
                    if not concurrent_checked:
                        busy = two.post("/api/agent", json={"message": "Saidapet bridge is blocked.", "scenario": {"rule": "B"}, "mode": "llm"})
                        check("concurrent_model_rejected", busy.status_code == 429, busy.json())
                        concurrent_checked = True
                elif event["type"] == "reply":
                    reply = event["reply"]
                elif event["type"] == "error":
                    check("SSE_worker", False, event)
        duration = round(time.monotonic() - t, 2)
        check("real_Qwen", reply is not None and reply["mode"]["kind"] == "llm", reply and reply["mode"])
        check("stream_before_final", bool(steps) and steps[0]["seconds"] < duration, {"total_s": duration, "steps": steps})
        check("read_only_investigation", reply["proposals"] == [])
        check("other_visitor_still_private", two.get("/api/memory").json()["stats"]["investigations"] == 0)
    # crawler flood: cookieless homepage hits must not lock out a visitor who is using the demo, nor new visitors
    for _ in range(40):
        httpx.get(url + "/", timeout=30)
    check("active_visitor_survives_flood", one.post("/api/evaluate", json={"rule": "B"}).status_code == 200)
    with visitor() as three:
        check("new_visitor_after_flood", three.get("/api/meta").status_code == 200)
    results["finished_epoch"] = time.time()
    results["pass"] = True
if a.output:
    a.output.write_text(json.dumps(results, indent=2) + "\n")
