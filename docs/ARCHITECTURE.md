# Architecture

```
web/ (React 19 + TypeScript + MapLibre GL 5)        contraflow/ (Python 3.12, FastAPI)
  App.tsx        scenario state + undo history          api.py        HTTP endpoints, serves web/dist
  MapView.tsx    roads, river, closures, routes,        engine.py     Scenario, evaluate, interventions
                 zone detour heat, hover/click          graphdb.py    FalkorDB loader, GRAPH.COPY clones,
  ScenarioPanel  bridges / carriageways / roads /                     query ledger
                 constraints                            evidence.py   location resolution, Report nodes
  ImpactPanel    aggregate metric + explanations        model.py      OSM -> network model (processed JSON)
  RecoveryPanel  ranked interventions (Apply)           agent/
  EvidencePanel  FalkorDB query ledger                    tools.py        typed allow-listed tools + guards
  AgentPanel     agent conversation, trace,               runtime.py      LLM loop / deterministic, grounding
                 proposals, clarifications                llm.py          OpenAI-compatible client (Ollama)
                                                          deterministic.py rule-based planner (no LLM)
                                                          briefing.py     answer composed from tool results
```

## Request flow: evaluating a scenario

1. The UI sends the scenario: rule, closed carriageways and segments, reversed segments, constraints.
2. `Engine.validate` rejects unknown ids, contraflow on two-way roads, and unknown constraint kinds.
3. With no mutation, the access query runs on the base graph. Otherwise it runs on
   `GRAPH.COPY` → delete closed and reserved arcs → add reversed arcs → access query → `GRAPH.DELETE`.
4. The result is compared with the cached baseline over the same eligible zones.
5. The response includes per-zone deltas, route edge ids, closure explanations and the query ledger.

## Request flow: ranking interventions

The engine generates candidates:
- reopen each closed carriageway;
- reopen each fully closed bridge;
- reopen each closed road segment;
- contraflow candidates from direction-ignoring routes in FalkorDB.

It drops anything excluded by a constraint and evaluates each remaining candidate on its own clone. Candidates are
validated by the same `Engine.validate` as every request (constraint violations are excluded with their code), then
ranked by access lost (ascending), access restored, then net distance recovered. All 23 candidates of the all-closed scenario are ranked in about
1.1 s (120 FalkorDB operations).

## Request flow: the agent

`run_agent` builds a `ToolBox` over a working copy of the scenario.

- **LLM mode:** the model (Ollama `qwen2.5:7b` by default) chooses tools through OpenAI-style function calling. There
  are at most 8 steps, with one nudge if it stops before completing a field-report workflow.
- **Deterministic mode:** rule-based intent parsing drives the same tools.

In both modes the guards run in the tool layer:
- **Ambiguity:** an unresolved or ambiguous location cannot be changed.
- **Direction:** a direction not stated by the user is not applied.
- **Validation:** ids, kinds and arguments are checked; unknown tools are rejected.
- **Intent:** the user's message is parsed once (`agent/intent.py`); mutating tools must match it. Clarifications
  carry the intent, candidates and scenario key; confirmations are executed deterministically and refused if the
  scenario changed.
- **Completion:** in LLM mode, a mutating request that ends without a proposal (and without ambiguity) is completed
  by deterministic orchestration (never for negative or question intents), a re-ranking for the final scenario is added if missing, and the model loop is bounded at 90 s (deterministic follow-ups and graph queries run after that bound, typically 1–3 s more).
- **Canonical candidates:** `Engine.normalize` gives one closure representation; the UI applies a candidate only through `POST /api/apply_candidate` with `{candidate_id, base_key}`; the server regenerates the eligible operations for the current scenario (`Engine.candidate_items`, the same generator the ranking uses), applies the canonical transformation, re-validates constraints and computes the outcome (`409 stale_candidate`, `422 ineligible_candidate` / `constraint_violation`). A client revision counter discards late responses after newer user edits.

The primary answer is composed from tool results by `briefing.compose` and checked for ungrounded numbers. The agent
returns proposals, each a complete scenario. The UI applies them only when the user clicks **Apply**.

## Request flow: investigations, memory and vulnerability

- **Investigation** (`intent: investigate` — "may be", "what if", "reportedly", "which neighbourhoods …"): the
  disruption clause and an optional constraint clause ("keeping X reserved …") are parsed separately. The model is
  offered only read-only investigation tools; whatever it does, the runtime completes the chain deterministically
  (labelled as such): `investigate_disruption` → `assume_constraint` (only if the user stated one) → `rank_recovery`
  under the hypothesis → `compare_options` for the top two beneficial candidates (applied by id with
  `Engine.apply_result` on the hypothesis, then a per-zone route diff). Nothing is proposed; suggested next steps are
  plain instructions.
- **Memory** (`contraflow/memory.py`): graph `contraflow_memory` —
  `(:Investigation)<-[:IN]-(:Report {provenance:'reported_unverified'})-[:REFERS_TO {resolution}]->(:Place)`,
  `(:Finding {provenance:'computed', scenario_key, hypothesis})-[:CONCERNS|AFFECTS {delta_m}]->(:Place)`,
  `(:Finding)-[:ASSUMES]->(:Assumption)`, `(:Proposal)-[:IN]->(:Investigation)`, `(:Decision)-[:ON]->(:Proposal)`.
  Every write is appended to `data/memory/memory.jsonl` and replayed into an empty graph at start-up. `recall_memory`
  re-applies a stored hypothesis to the *current* scenario and re-evaluates it rather than trusting the old number.
- **Vulnerability** (`Engine.vulnerability`): bounded candidate set → one `GRAPH.COPY` clone per candidate failure →
  the same `algo.SPpaths` access query → ranked by zones cut off, then added distance; flags mark prominent roads
  with no effect, less obvious roads with large effect, equal-length alternatives and assistance-point access links.

## Why these choices

| Choice | Reason |
|---|---|
| FalkorDB `algo.SPpaths` with pathCount 1 | Passed 1,704 + 3,266 + engine differential checks. `pathCount > 1` occasionally exceeded the server's 1 s timeout on denser graphs, so alternatives come from clones instead. |
| No `algo.maxFlow` | Returned incorrect values in our tests ([archive](archive/FEASIBILITY_MAXFLOW_NO_GO.md)). |
| One base graph + disposable clones | Isolation is guaranteed by the database. Tests assert the base fingerprint is unchanged and no `cf_sc_*` graphs remain. |
| Processed model JSON | The runtime image needs no GIS stack. `scripts/build_model.py` regenerates it from the raw OSM files and a test asserts the two are identical. |
| Map from local GeoJSON, no tiles or glyphs | Works offline and avoids tile-server usage policies. |
| Local LLM via Ollama + deterministic fallback | Zero cost. The demo never depends on a rate-limited free API. |
