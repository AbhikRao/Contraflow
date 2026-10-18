"""Latency A/B for the live agent: per-intent tool subsets (current) vs. offering every tool (previous behaviour).
Same prompts, same local model, same deterministic completion. Usage:
  .venv/bin/python scripts/latency_ab.py OUT.json        (Ollama running; isolated memory graph, deleted afterwards)"""
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("CONTRAFLOW_MEMORY_GRAPH", "contraflow_memory_latency")
os.environ.setdefault("CONTRAFLOW_MEMORY_LOG", os.path.join(tempfile.mkdtemp(prefix="cf_lat_"), "m.jsonl"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contraflow.agent.runtime as rt  # noqa: E402
from contraflow.agent.tools import SPECS  # noqa: E402
from contraflow.api import state  # noqa: E402
from contraflow.engine import Scenario  # noqa: E402

ST = state()
PROMPTS = [
    ("Kotturpuram bridge might be flooded. Who is affected?", Scenario("B")),
    ("What if Maraimalai Adigal northbound were blocked, keeping Kotturpuram reserved for emergency vehicles?", Scenario("B")),
    ("Which roads matter most for hospital access?", Scenario("B")),
    ("Saidapet bridge is blocked.", Scenario("B")),
]


def run(label, all_tools):
    orig = rt.specs_for
    if all_tools:
        rt.specs_for = lambda intent: SPECS
    out = []
    try:
        for msg, sc in PROMPTS:
            calls = {"n": 0}
            real = rt.llm.chat

            def counting(*a, **k):
                calls["n"] += 1
                return real(*a, **k)
            rt.llm.chat = counting
            t = time.perf_counter()
            r = rt.run_agent(ST, msg, sc, [], "llm", None)
            rt.llm.chat = real
            out.append({"prompt": msg, "seconds": round(time.perf_counter() - t, 1), "model_turns": calls["n"],
                        "tools_offered": len(SPECS) if all_tools else len(orig(rt.parse_intent(msg))),
                        "tool_calls": sum(1 for s in r["steps"] if s.get("kind") == "tool"), "mode": r["mode"]["kind"],
                        "proposals": len(r["proposals"]), "ungrounded": r["grounding"]["ungrounded"]})
    finally:
        rt.specs_for = orig
    return {"condition": label, "runs": out, "median_s": statistics.median(x["seconds"] for x in out),
            "total_model_turns": sum(x["model_turns"] for x in out)}


if __name__ == "__main__":
    rt.run_agent(ST, "What should we reopen first?", Scenario("B"), [], "llm", None)   # warm the model
    res = [run("per-intent tool subsets (current)", False), run("all tools offered (previous)", True),
           run("per-intent tool subsets (current, repeat)", False)]
    try:
        Path(sys.argv[1]).parent.mkdir(parents=True, exist_ok=True)
        Path(sys.argv[1]).write_text(json.dumps(res, indent=1))
        for c in res:
            print(c["condition"], "median", c["median_s"], "s; model turns", c["total_model_turns"])
            for x in c["runs"]:
                print("   ", x["seconds"], "s", x["model_turns"], "turns", x["tools_offered"], "tools", x["prompt"][:50])
    finally:
        ST["db"].delete(os.environ["CONTRAFLOW_MEMORY_GRAPH"])
