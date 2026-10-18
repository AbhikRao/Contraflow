"""Two MCP agent processes write to ONE memory graph + journal concurrently; verify counts and journal replay.
Isolated graph/log (never the demo memory). Usage: .venv/bin/python scripts/memory_concurrency_check.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOG = os.path.join(tempfile.mkdtemp(prefix="cf_conc_"), "m.jsonl")
ENV = {**os.environ, "CONTRAFLOW_MEMORY_GRAPH": "contraflow_memory_conc", "CONTRAFLOW_MEMORY_LOG": LOG}
REPORTS = ["What if Kotturpuram bridge were closed?", "What if Abraham bridge were closed?", "What if Maraimalai Adigal northbound were closed?",
           "What if Thiru Vi Ka bridge were closed?", "Reportedly Kotturpuram southbound is blocked"]


def agent(tag, out):
    p = subprocess.Popen([sys.executable, "-m", "contraflow.mcp_server"], cwd=ROOT, env=ENV, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def rpc(i, m, prm):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": prm}) + "\n"); p.stdin.flush()
        return json.loads(p.stdout.readline())
    rpc(0, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": tag, "version": "1"}})
    ids = []
    for i, q in enumerate(REPORTS, 1):
        r = rpc(i, "tools/call", {"name": "investigate_report", "arguments": {"report": f"{q}"}})["result"]
        ids.append(r["structuredContent"]["investigation_id"])
    rec = rpc(99, "tools/call", {"name": "recall_investigations", "arguments": {}})["result"]["structuredContent"]
    out[tag] = {"ids": ids, "recalled": [f["investigation_id"] for f in rec["findings"]]}
    p.stdin.close(); p.wait(timeout=30)


from contraflow.graphdb import connect_db  # noqa: E402
from contraflow.memory import Memory  # noqa: E402
db = connect_db()
if "contraflow_memory_conc" in db.list_graphs():
    db.delete("contraflow_memory_conc")
out: dict = {}
ts = [threading.Thread(target=agent, args=(t, out)) for t in ("agent_a", "agent_b")]
[t.start() for t in ts]
[t.join() for t in ts]
m = Memory(db, Path(LOG), "contraflow_memory_conc")
live = m.stats()
lines = Path(LOG).read_text().splitlines()
ok_json = all(json.loads(x) for x in lines)
db.delete("contraflow_memory_conc")
replayed = Memory(db, Path(LOG), "contraflow_memory_conc")
rs = replayed.ensure()
after = replayed.stats()
cross = {a: sorted(set(out[b]["ids"]) & set(out[a]["recalled"])) for a, b in (("agent_a", "agent_b"), ("agent_b", "agent_a"))}
db.delete("contraflow_memory_conc")
print(json.dumps({"investigations_written": len(out["agent_a"]["ids"]) + len(out["agent_b"]["ids"]), "graph_stats": live,
                  "journal_lines": len(lines), "journal_lines_valid_json": ok_json, "replayed_ops": rs["replayed_ops"],
                  "stats_after_replay_equal": after == live,
                  "each_agent_recalls_the_other_agents_findings": {k: len(v) > 0 for k, v in cross.items()}}, indent=1))
