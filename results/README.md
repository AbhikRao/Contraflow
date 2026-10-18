# Evaluation evidence

These are recorded runs, not a claim that every suite was rerun for each documentation release. Use a separate development database when reproducing them; do not run evaluation scripts against the live demo.

| Evidence | Purpose | Reproduction / implementation |
|---|---|---|
| [demo_run.json](demo_run.json) | Locked demonstration, both assistance-point rules, closures and recovery | `scripts/record_demo.py` |
| [routing_correctness.json](routing_correctness.json) | 1,704 differential routing checks | `tests/test_routing_correctness.py` |
| [osm_scenarios.json](osm_scenarios.json) | 3,266 historical OSM routing comparisons | `feasibility/osm_scenarios.py` |
| [benchmarks.json](benchmarks.json) | Graph operations, ranking and agent timing | `scripts/benchmark.py` |
| [generalization_deterministic.json](eval/generalization_deterministic.json), [generalization_llm.json](eval/generalization_llm.json) | Held-out investigation prompts in rules and Qwen modes | `scripts/generalization_eval.py` |
| [memory_concurrency.json](eval/memory_concurrency.json) | Memory integrity during concurrent investigations | `scripts/memory_concurrency_check.py` |
| [latency_ab.json](eval/latency_ab.json) | Recorded sensitivity-scan latency comparison | `scripts/latency_ab.py` |
| [robustness_run.json](robustness/robustness_run.json), [perf.json](robustness/perf.json) | Assumption testing and measured performance | `scripts/robustness_perf.py` |
| [compose_fresh_clone.log](setup/compose_fresh_clone.log) | Historical clean-checkout Compose build, health and browser verification | Recorded October 16; predates the final UI cleanup |

The remaining root JSON files document the archived max-flow feasibility gate, minimal counterexamples and pruning experiments. See [max-flow findings](../docs/archive/FEASIBILITY_MAXFLOW_NO_GO.md) and [routing feasibility](../docs/archive/FEASIBILITY_V2_ROUTING_GO.md). The rejected max-flow approach is not used in production.

Security, constraints, intent, stale proposals, candidate prediction/application equivalence and browser regressions are retained as executable tests in [`tests/`](../tests/). Public-gateway verification is retained in [`deploy_public/`](../deploy_public/). Historical measurements are hardware- and version-dependent.
