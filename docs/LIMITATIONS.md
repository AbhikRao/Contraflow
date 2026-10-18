# Limitations and assumptions

Contraflow is a **tabletop planning sandbox**. It is not an emergency-response, evacuation or traffic tool and must
not be used for operational decisions.

## What the metric is

The metric is the sum, over neighbourhood anchors reachable in the baseline, of the **modeled shortest directed road
distance** to the nearest assistance point of the chosen rule. Zones that lose access are listed separately.

## What it is not

- **Not travel time.** It ignores speed, congestion, signals, turn restrictions (beyond OSM direction), road
  condition and flood depth.
- **Not capacity or throughput.** Road width, lanes and vehicle volumes are not modeled. Contraflow only reverses
  direction.
- **Not people.** It counts no population, demand, vulnerability or people served. A neighbourhood is a single
  anchor point.
- **Not hospital capability.** Assistance points are OSM hospitals chosen by a declared name rule. Rule B includes a
  Corporation maternity hospital because it is a public hospital; services, capacity and opening status are unknown.
- **Not observed events.** Closures, reports and constraints are user inputs; the demo uses synthetic reports.
  Nothing is verified.

## Interpretation cautions

- **A zero change is about this metric only.** It means no anchor's shortest route to its nearest assistance point
  used the closed road under the active rule. The road may still be important for other destinations, directions,
  capacity or emergency access.
- **Results depend on declared choices:**
  - the assistance-point rule (both are offered and reported);
  - anchor placement (nearest junction within the main strongly connected component);
  - the bounding box (edge effects near its boundary).
- **OSM completeness.** Missing or incorrect one-way tags, names or links in OpenStreetMap carry straight into the
  results.
- **Location resolution is heuristic.** It uses names, full-text search and straight-line proximity to named places.
  It asks when uncertain, but a user's confirmation is trusted as given.

## Agent limitations

- A local 7B model sometimes skips steps or words things loosely. That is why:
  - the primary briefing is generated from tool outputs;
  - its numbers are checked against those outputs;
  - the model's own wording is shown separately.

  The numeric check only confirms that each number appears in this turn's tool outputs; it does not verify units,
  subjects or meaning. The model's own wording is **not** verified and is labelled as such.
- The deterministic planner understands a limited set of phrasings (field reports, reopenings, constraints, recovery
  questions, "why" questions, status).
- LLM replies took about 5–35 s on an Apple M4 laptop in testing (one benchmark run reached 33.8 s; the model loop is bounded at 90 s, but deterministic follow-ups and graph queries can add a few seconds after that bound, so a whole request is not strictly bounded at 90 s); the deterministic planner takes about 1 s.

- Intent parsing is rule-based (regular expressions) into a typed action (operation, polarity, mood, direction).
  Unusual phrasings may be classified as "none" (no action) — the fail-closed outcome — but correct classification
  is not guaranteed for every wording; multi-step requests are reduced to one mutating operation (plus ranking).
- Contraflow candidates are a bounded heuristic (≤ 24 one-way segments), not exhaustive optimization.
- Reloading the base graph (e.g. after the model changes) discards the `Report` nodes linked into the road graph; the
  durable copy in the investigation-memory graph (`contraflow_memory` + `data/memory/memory.jsonl`) is kept.
- Investigation memory is bounded and plain: recall is by place id (crossing / zone) or recency, not semantic search.
  A recalled finding is re-evaluated on the current scenario; rankings computed for another scenario are only
  marked stale, not re-ranked automatically. Reports are never verified — the memory stores who said what, not truth.
- Investigations resolve one disruption per message (plus one assumed constraint).
- Vulnerability analysis evaluates a bounded candidate set (all open crossings and directions, the 20 links most used
  by current zone routes, 5 long unused major roads), not every road. It measures modeled shortest-path distance to
  the nearest assistance point — not travel time, capacity, flooding likelihood or traffic. The top-ranked
  "critical" link in the baseline is the short access road into one hospital's snapped junction: it shows how much
  the metric depends on how an assistance point is attached to the network, not that the street is critical in
  reality (real hospitals usually have several entrances).

## Engineering limits

- The FalkorDB server image ships a 1 s default query timeout. Multi-path `SPpaths` requests (`pathCount > 1`) can
  exceed it on dense graphs, so Contraflow avoids them.
- FalkorDB `algo.maxFlow` returned incorrect results in our tests and is deliberately not used (see `docs/archive`).
- Scenario evaluation is sequential: ranking 23 candidates takes about 1.1 s.
- Startup deletes leftover `cf_sc_*` scenario graphs; do not run two API processes against the same database.
- No authentication or TLS is built in: put the app behind an authenticating proxy for any public deployment and use
  `FALKORDB_USERNAME` / `FALKORDB_PASSWORD` for a protected database.
