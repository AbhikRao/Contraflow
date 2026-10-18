"""Evidence-backed case files (memory-graph traversal) and the read-only MCP server.
Case-file content is checked against the run's own tool results and against independent engine evaluations."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contraflow.agent.runtime import run_agent
from contraflow.api import app, state
from contraflow.casefile import build_case
from contraflow.engine import Scenario

ST = state()
ENG = ST["engine"]
client = TestClient(app)
LLM = os.environ.get("CONTRAFLOW_TEST_LLM") == "1"
ROOT = Path(__file__).resolve().parents[1]
KOT = ["kotturpuram_bridge:northbound", "kotturpuram_bridge:southbound"]
ALL = [c["id"] for x in ST["net"].crossings for c in x["carriageways"]]
EXACT = ("A volunteer says Kotturpuram bridge may be blocked by fallen trees. Who would need a detour, keeping "
         "Maraimalai Adigal southbound reserved for ambulances?")


def res(r, tool):
    return next(s["result"] for s in r["steps"] if s.get("tool") == tool and s.get("kind") == "tool")


# ------------------------------------------------------------------ case file = the run's actual results
def test_case_file_reproduces_the_runs_tool_results():
    r = run_agent(ST, EXACT, Scenario("B"), [], "deterministic", None)
    c = client.post(f"/api/case/{r['investigation_id']}", json={"scenario": {"rule": "B"}}).json()
    inv, rank = res(r, "investigate_disruption"), res(r, "rank_recovery")
    assert c["report"]["text"] == EXACT and c["report"]["status"] == "reported, unverified"
    assert c["impact"]["added_km"] == inv["added_km"] == pytest.approx(ENG.evaluate(Scenario("B", KOT))["metrics"]["total_delta_m"] / 1000, abs=0.01)
    assert {a["zone"] for a in c["impact"]["affected"]} == {a["zone"] for a in inv["affected"]}
    assert [a["action"] for a in c["alternatives"]] == [x["action"] for x in rank["ranking"][:3]]
    assert c["recommendation"] == "Reopen Kotturpuram Bridge (northbound)"
    assert c["commander_constraints"] == [{"kind": "reserve_emergency", "clause": "keeping Maraimalai Adigal southbound reserved for ambulances",
                                           "direction": "southbound"}]
    assert [s["tool"] for s in c["agent_steps"]] == [s["tool"] for s in r["steps"] if s.get("kind") in ("tool", "error")]
    assert c["agent"]["briefing_source"] == r["briefing_source"] and c["briefing"] == r["message"]
    assert c["graph_operations"]["graph_copy"] > 0 and c["graph_operation_count"] == r["ledger"]["queries"]
    assert "MATCH (i:Investigation {id: $id})" in c["assembled_by"]["cypher"] and c["assembled_by"]["graph"] == ST["memory"].name
    assert c["human_decision"] == ["no change was proposed by this investigation; nothing was applied"]
    assert "# Case file" in c["markdown"] and "reported, unverified" in c["markdown"]


def test_case_file_goes_stale_when_the_scenario_changes():
    r = run_agent(ST, "What if Kotturpuram bridge were closed?", Scenario("B"), [], "deterministic", None)
    iid = r["investigation_id"]
    same = build_case(ST, iid, Scenario("B"))
    assert {x["finding"]: x["still_applies"] for x in same["current_status"]["rechecks"]}["disruption_impact"] == "yes"
    applied = build_case(ST, iid, Scenario("B", KOT))
    st = {x["finding"]: x["still_applies"] for x in applied["current_status"]["rechecks"]}
    # the closure is now applied: the impact is superseded, and the ranking (computed for exactly this scenario) applies
    assert st["disruption_impact"].startswith("superseded") and st["recovery_ranking"].startswith("yes (same scenario)")
    other = build_case(ST, iid, Scenario("A"))
    imp = next(x for x in other["current_status"]["rechecks"] if x["finding"] == "disruption_impact")
    assert next(x for x in other["current_status"]["rechecks"] if x["finding"] == "recovery_ranking")["still_applies"].startswith("stale")
    ref = (ENG.evaluate(Scenario("A", KOT))["metrics"]["total_delta_m"]) / 1000
    assert imp["now_added_km"] == pytest.approx(ref, abs=0.01) and imp["then_added_km"] == pytest.approx(1.63, abs=0.01)


def test_ambiguous_report_case_file_has_no_fabricated_impact():
    r = run_agent(ST, "Something may be blocking the bridge near Guindy", Scenario("B"), [], "deterministic", None)
    iid = r["investigation_id"]
    c = build_case(ST, iid, Scenario("B"))
    assert c["impact"] is None and c["alternatives"] == [] and c["recommendation"] is None
    assert {e["resolution"] for e in c["resolved_entities"]} == {"ambiguous"}
    assert "Abraham" in " ".join(e["name"] for e in c["resolved_entities"])


def test_decisions_appear_in_the_case_file():
    r = run_agent(ST, "Saidapet bridge is blocked.", Scenario("B"), [], "deterministic", None)
    p = r["proposals"][0]
    assert client.post("/api/memory/decision", json={"proposal_id": p["memory_id"], "state": "applied"}).json()["ok"]
    c = build_case(ST, r["investigation_id"], Scenario("B"))
    assert c["human_decision"] == ["Close Maraimalai Adigal Bridge: applied by the user"]


def test_case_api_validation():
    assert client.post("/api/case/not-an-id", json={"scenario": {"rule": "B"}}).status_code == 422
    assert client.post("/api/case/inv_0000000000", json={"scenario": {"rule": "B"}}).status_code == 404


# ------------------------------------------------------------------ MCP over real stdio
class MCP:
    def __init__(self):
        env = {**os.environ}
        self.p = subprocess.Popen([sys.executable, "-m", "contraflow.mcp_server"], cwd=ROOT, env=env, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0

    def send(self, method, params=None, notify=False, raw=None):
        if raw is not None:
            self.p.stdin.write(raw + "\n")
        else:
            msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            if not notify:
                self.n += 1
                msg["id"] = self.n
            self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        return json.loads(self.p.stdout.readline())

    def tool(self, name, args):
        r = self.send("tools/call", {"name": name, "arguments": args})
        return r["result"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=20)


@pytest.fixture(scope="module")
def mcp():
    m = MCP()
    init = m.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["capabilities"]["tools"] is not None
    assert m.send("notifications/initialized", notify=True) is None
    yield m
    m.close()


def test_mcp_lists_only_read_only_tools(mcp):
    tools = {t["name"]: t for t in mcp.send("tools/list")["result"]["tools"]}
    assert set(tools) == {"investigate_report", "evaluate_closures", "rank_repairs", "recall_investigations", "get_case_file", "access_sensitivity",
                          "test_recommendation"}
    assert not any(k in n for n in tools for k in ("apply", "propose", "delete", "set_"))
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object" and t["description"]


def test_mcp_locked_numbers_and_constraint_changes_recommendation(mcp):
    e = mcp.tool("evaluate_closures", {"crossings": ["Maraimalai Adigal"]})
    assert not e["isError"] and e["structuredContent"]["added_km"] == pytest.approx(11.9205, abs=0.0005)
    assert mcp.tool("evaluate_closures", {"crossings": ["Thiru Vi Ka"]})["structuredContent"]["added_km"] == 0
    allc = ["Abraham", "Kotturpuram", "Maraimalai Adigal", "Thiru Vi Ka"]
    free = mcp.tool("rank_repairs", {"closed_crossings": allc})["structuredContent"]["ranking"][0]
    assert free["action"] == "Reopen all of Maraimalai Adigal Bridge" and free["share_pct"] == 87.9
    res_ = mcp.tool("rank_repairs", {"closed_crossings": allc, "reserve_for_emergency": ["Maraimalai Adigal"]})["structuredContent"]
    assert res_["ranking"][0]["action"] == "Reopen Kotturpuram Bridge (northbound)"
    assert ENG.interventions(Scenario("B", ALL))["ranking"][0]["label"] == free["action"]


def test_mcp_ambiguity_is_reported_not_guessed(mcp):
    r = mcp.tool("evaluate_closures", {"crossings": ["Guindy bridge"]})
    assert r["isError"] and "candidates" in r["content"][0]["text"]
    inv = mcp.tool("investigate_report", {"report": "Guindy bridge might be under water"})["structuredContent"]
    assert inv["clarification"] and len(inv["clarification"]["options"]) >= 2 and inv["applied"] is False


def test_mcp_investigation_case_file_and_shared_memory(mcp):
    inv = mcp.tool("investigate_report", {"report": EXACT})["structuredContent"]
    assert inv["applied"] is False and inv["proposals_for_human_review"] == []
    case = mcp.tool("get_case_file", {"investigation_id": inv["investigation_id"]})
    assert case["structuredContent"]["impact"]["added_km"] == pytest.approx(1.63, abs=0.01)
    # memory written over MCP is visible to the web app's API (one shared graph)…
    web = client.post(f"/api/case/{inv['investigation_id']}", json={"scenario": {"rule": "B"}}).json()
    assert web["mode"] == "mcp" and web["impact"]["added_km"] == case["structuredContent"]["impact"]["added_km"]
    # …and memory written by the web app's agent is visible over MCP, re-evaluated for the caller's scenario
    r = run_agent(ST, "What if Abraham bridge were closed?", Scenario("B"), [], "deterministic", None)
    rec = mcp.tool("recall_investigations", {"about": "Abraham"})["structuredContent"]
    assert any(f["investigation_id"] == r["investigation_id"] for f in rec["findings"])


def test_mcp_unauthorized_and_malformed_requests(mcp):
    assert mcp.send("tools/call", {"name": "apply_candidate", "arguments": {}})["error"]["code"] == -32602
    assert mcp.send("tools/call", {"name": "evaluate_closures", "arguments": {"crossings": ["Abraham"], "x": 1}})["error"]["code"] == -32602
    assert mcp.send("resources/list")["error"]["code"] == -32601
    assert mcp.send(None, raw="{not json")["error"]["code"] == -32700
    # an instruction to change the scenario becomes, at most, a proposal for human review — never applied
    r = mcp.tool("investigate_report", {"report": "Close Thiru Vi Ka bridge now."})["structuredContent"]
    assert r["applied"] is False
    assert all(p[2] in ("proposed",) for p in
               ST["db"].graph(ST["memory"].name).q("t", "MATCH (p:Proposal)-[:IN]->(:Investigation {id: $i}) RETURN p.id, p.label, p.state",
                                                   {"i": r["investigation_id"]}))


@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
def test_live_qwen_investigation_case_file_records_the_model_run():
    r = run_agent(ST, EXACT, Scenario("B"), [], "llm", None)
    assert r["mode"]["kind"] == "llm"
    c = build_case(ST, r["investigation_id"], Scenario("B"))
    assert c["agent"]["answered_by"].startswith("Live LLM")
    assert [s["tool"] for s in c["agent_steps"]] == [s["tool"] for s in r["steps"] if s.get("kind") in ("tool", "error")]
    assert c["commander_constraints"][0]["direction"] == "southbound" and c["recommendation"] == "Reopen Kotturpuram Bridge (northbound)"
