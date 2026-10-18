# Contraflow: FalkorDB Feasibility Gate

**Date:** 2026-10-15 (Day 1 of Graph Hacks)
**Verdict:** **NO-GO, recommend pivot to REOPEN.** The gate stopped at Step 3 (correctness), as the gate rules require.

## Summary

FalkorDB's `algo.maxFlow` returns **wrong maximum-flow values** on small graphs, including one source to one sink. It also returns per-edge flows that break flow conservation.

- This reproduces on every build tested: 6.0.2 (pinned), v4.22.0, `edge-c` (C main) and `edge` (Rust main, pulled today).
- Our independent certificate caught every wrong answer (cut ≠ flow, or a conservation error) and cross-checked it against NetworkX.
- Because the core computation Contraflow is pitched on is unreliable in the sponsor engine, the gate condition "FalkorDB max-flow works reliably on the pinned version" fails.
- Per the agreed rule, the OSM import, benchmarks and demo-premise experiments (Steps 4–6) **were not run**.

## 1. Environment

| Item | Value |
|---|---|
| Host | macOS 27.2, arm64, Apple M4 (10 cores), 16 GB RAM |
| Docker | Colima profile `contraflow` (4 vCPU / 6 GiB). Docker context switched to `colima-contraflow`. The Compose plugin isn't installed, so `infra/start_falkordb.sh` starts the server |
| Python | 3.12.13 via `uv`, pinned in `pyproject.toml` (falkordb 1.2.2, networkx 3.4.2, osmnx 2.0.6) |
| Pinned FalkorDB | `falkordb/falkordb:6.0.2@sha256:107455e8…ba6` (module ver 60002) |
| Procedures (6.0.2) | `algo.maxFlow`, `algo.BFS`, `algo.SPpaths`, `algo.SSpaths`, `algo.WCC`, … (no A*/CCH) |
| v4.22.0 extras | `algo.AStar`, `db.idx.cch.query` (optional, not needed) |
| Server default | `TIMEOUT 1000` ms is baked into the image's module args. Long queries are killed after 1 s unless reconfigured |

Graph creation and mutation (`UNWIND`/`CREATE`/`SET`/`DELETE`), edge-property indexes, and `algo.BFS` over a temporary relationship type all work. `GRAPH.COPY` was not exercised because the gate stopped first.

## 2. Engine constraints confirmed by testing

- **Multi-edges are rejected.** "relationship type 'ROAD' contains multi-edges; maxFlow requires a simple adjacency matrix" (6.0.2); "must not contain multi-edges (tensors)" (v4.22.0).
  - **Normalization chosen:** each duplicate parallel road gets its own split junction (`split:<eid>`) with an in-arc and an out-arc of the same capacity. No physical road is merged or dropped, and the flow value is unchanged. This is tested (`parallel_roads`: 3+4+2 = 9 ✓).
- **Decimal and zero capacities are accepted.** Opposite-direction roads are separate arcs, and reverse-residual arcs are kept as a separate `RES` type, never treated as drivable roads.

## 3. Correctness results

My min-cut reconstruction runs inside FalkorDB:
1. Write the returned flows onto the edges.
2. Create `RES` arcs for forward spare capacity (cap − flow > 0) and reverse residual (flow > 0).
3. Run `algo.BFS` from a temporary super-source to find the source side S.
4. The cut is the set of `ROAD` edges from S to outside S.

A second, independent Python reconstruction and NetworkX's `maximum_flow_value` cross-check it.

| Test set | 6.0.2 (pinned) | v4.22.0 |
|---|---|---|
| 10 hand-built fixtures (single path, directed-only, opposite directions, zero capacity, 3 parallel roads, disconnected, multi-source/multi-sink, decimal capacities, residual-reverse case) | **10/10 pass**, all certificate checks | **10/10 pass** |
| 60 seeded random perturbations (closures by deletion and zero capacity, capacity changes) | **58/60.** rand59: FalkorDB 14,550 vs true 15,750. rand4: value correct but edge flows break conservation by 600 | 59/60 + rand59 **timed out** at 1 s; with a 30 s timeout it ran **4.1 s** and returned the same wrong 14,550 |
| 300 random graphs (20–150 nodes, 1–4 sources, 1–3 sinks) | 296/300 correct | not run |
| Same 300, pruned to the live core (dead ends removed) | 297/300 (**pruning doesn't fix it**) | not run |
| 382 random graphs, **one source, one sink** | **381/382** (1 wrong: 2,550 vs 2,700) | not run |

**Minimal standalone reproduction** (raw Cypher, no Contraflow code): `feasibility/falkordb_maxflow_bug_repro.py`.
- It has 13 edges, source n15 and sink n75. `algo.maxFlow` returns **2550**; the true value is **2700**.
- The 2,700 is hand-verifiable: paths n15→n33→n75 (2,400), n15→n33→n7→n69→n75 (150), and n15→n40→n30→n53→n25→n5→n73→n69→n75 (150), all within capacity.
- **Wrong on all four builds:** 60002, 42200, edge, edge-c.

**Observed failure rate:** about 0.3–1.3% of random graphs. That understates the risk for Contraflow: a real road network has thousands of edges and every what-if scenario is a fresh solve, so the chance that *some* solve during a live demo is wrong is substantial. The per-edge flows we'd animate on the map are also unreliable, even when the total is right.

**My reconstruction is not the failing part.** Whenever FalkorDB's flow is truly maximal, the in-DB cut equals the flow and matches NetworkX. Every wrong FalkorDB answer was flagged by the certificate.

## 4. Steps 4–6 (OSM network, benchmarks, demo premise)

**Not executed**, deliberately: "If correctness fails, do not proceed to larger experiments."

The only timing evidence is incidental and must not be read as a benchmark: about 2–3 ms per `algo.maxFlow` on 6.0.2 for ~170-edge graphs, versus 4.1 s on v4.22.0 for one 160-edge graph.

## 5. Is there a rescue? (For the owner's review only, not pursued)

**Certify-and-repair** would be possible:
1. Run FalkorDB `maxFlow`.
2. Certify it with the in-DB residual cut.
3. If the certificate fails, find augmenting paths with `algo.SPpaths` over `RES` arcs and push flow until no path remains (Ford–Fulkerson warm-started from FalkorDB's answer).

**Against it:**
- The headline computation becomes our repair loop rather than FalkorDB's algorithm.
- Repair-loop latency on 2–5k edges is unmeasured.
- Demoing "the sponsor's max-flow is wrong" to a FalkorDB-sponsored panel is a risky narrative.
- It is exactly the "rescue a weak foundation" work the gate was designed to prevent.

I don't recommend it.

**Optional and outward-facing (needs your approval):** report the minimal repro to FalkorDB on GitHub. It's a genuine, small, hand-verifiable correctness bug.

## 6. Scientific limitations recorded for any future use

Static max-flow is a steady-state throughput ceiling of an estimated capacity model. It doesn't predict evacuation time, traffic dynamics or safety. Shelter beds are a stock and must not be turned into an hourly rate.

## 7. Files

| Path | Purpose |
|---|---|
| `infra/docker-compose.yml`, `infra/start_falkordb.sh` | Pinned FalkorDB 6.0.2 by digest |
| `pyproject.toml` | Pinned Python dependencies and pytest path |
| `feasibility/flowlib.py` | Loader, parallel-road normalization, `algo.maxFlow` wrapper, in-DB residual/min-cut, Python and NetworkX verification |
| `tests/test_maxflow_correctness.py` | Fixtures, multi-edge rejection test, 60 seeded perturbations |
| `feasibility/falkordb_maxflow_bug_repro.py` | Standalone 13-edge bug repro |
| `feasibility/repro.py`, `debug_random.py`, `prune_experiment.py`, `single_pair_experiment.py` | Failure isolation experiments |
| `results/correctness_log.json` | Per-case results (both versions) |
| `results/minimal_wrong_maxflow_single_pair.json`, `results/minimal_wrong_maxflow_6.0.2.json`, `results/repro_case*.json` | Minimal failing graphs |
| `results/prune_experiment_6379.json`, `results/single_pair_experiment_6379.json` | Failure-rate experiments |
| `results/environment.json` | Versions, digests, server config |

**Reproduce:**
```bash
./infra/start_falkordb.sh
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r pyproject.toml
FALKOR_PORTS=6379 .venv/bin/pytest -q feasibility/test_maxflow_correctness_archive.py
# Historical max-flow experiment: deliberately retains the documented failing case.
# Run only against a separate development database, never the public demo.
.venv/bin/python feasibility/falkordb_maxflow_bug_repro.py   # prints WRONG
```
