# Contraflow V2: Road Access and Recovery Feasibility (no max-flow)

**Date:** 2026-10-15
**Timebox:** 90 min
**Verdict:** **GO, Contraflow V2 (conditional).** See "Risks" at the end.

The max-flow investigation is kept unchanged in [`FEASIBILITY_MAXFLOW_NO_GO.md`](FEASIBILITY_MAXFLOW_NO_GO.md).

## What was tested

All on the pinned FalkorDB 6.0.2 container (`contraflow-falkordb`).

### 1. Deterministic fixture
**File:** `tests/test_routing_correctness.py::test_fixture_directed_closures_alternatives_copy`

This one test checks:
- directed shortest paths;
- that no reverse route exists;
- that a node with no way out can't reach anything;
- the 3 alternative routes (5.25 / 5.5 / 6.5);
- a `GRAPH.COPY` clone, with:
  - a closure that reroutes the clone (4.75 → 6.0);
  - the original left untouched;
  - a second closure that disconnects A from D.

**Result: pass.**

### 2. Randomized NetworkX comparison
**File:** `tests/test_routing_correctness.py::test_random_routes_closures_access`

- 60 seeded directed graphs, 15–120 nodes, including parallel arcs.
- For each graph: the base graph, plus a clone with about 20% of roads closed.
- Compared: point-to-point distances, unreachable pairs, zone → nearest-assistance-point access, and k=3 alternative routes (graphs of 60 nodes or fewer).
- **1,704 checks, 0 mismatches** (`results/routing_correctness.json`).

### 3. Real OSM network
**Files:** `feasibility/osm_scenarios.py` → `results/osm_scenarios.json`

- **71 scenario evaluations × 46 zones = 3,266 zone-access checks against NetworkX, 0 mismatches.**

## Data

All of it is real, from OpenStreetMap (ODbL, © OpenStreetMap contributors), fetched through OSMnx 2.0.6 and the Overpass API.

| Item | Value |
|---|---|
| Area | bbox W 80.215, S 12.995, E 80.275, N 13.035 (Saidapet → Kotturpuram → Adyar → Besant Nagar) |
| Drive network | 3,483 junctions, **8,458 directed segments** (788 one-way). Two-way roads are two arcs; OSM one-way tags are respected |
| Zones | 46 named OSM places (suburb/neighbourhood/quarter), each snapped to its nearest junction |
| Assistance points: rule A (declared before running) | 37 "general hospitals" = OSM `amenity=hospital` minus names matching specialty keywords |
| Assistance points: rule B (declared before running) | 4 public hospitals (Corporation Hospital; Government Hospital, Adyar; Kanamapet Government Hospital; Chennai Corporation 24 Hours Maternity Hospital) |
| River crossings | Every drive segment whose geometry intersects the OSM "Adyar River" line, grouped into 7 physical carriageways: Maraimalai Adigal Bridge (Saidapet, ×2), Kotturpuram Bridge (×2), Thiru Vi Ka Bridge (×2), and Alandur Road / Abraham Bridge |

**Files:** `data/osm/adyar_drive.graphml`, `adyar_pois.json`, `adyar_river.json`, plus the fetch scripts.

**Synthetic or assumed:**
- Which hospitals count as "assistance points" (two rules, both reported).
- Which closures occur (scenario inputs, not observed flood data).
- Distance only: no travel time, congestion or flood depth.

## Results (rule B, 4 public hospitals; rule A in the JSON)

The same closures, ranked by objective (access lost first, then extra distance). None of these were tuned:

| Closure | Effect |
|---|---|
| Thiru Vi Ka Bridge, either carriageway | **0 m**: every zone still reaches the same nearest public hospital |
| Abraham Bridge | **0 m** |
| Kotturpuram Bridge, one carriageway | +2.8 km total; Kottur Gardens +1.18 km |
| Maraimalai Adigal Bridge (Saidapet), main carriageway | **+13.5 km total; Guindy +4.0 km** |
| All 7 crossings closed ("river flood" scenario) | +16.3 km total, worst Guindy +4.0 km; **no zone fully cut off** |

**Recovery priority with all 7 crossings closed** (reopen exactly one; evaluated on clones):

| Reopen | Distance saved |
|---|---|
| Maraimalai Adigal Bridge (main carriageway) | **13.0 km (80% of the damage)** |
| Kotturpuram Bridge (one carriageway) | 2.8 km |
| Thiru Vi Ka Bridge, Abraham Bridge, other carriageways | **0 km** |

**Contraflow** (reverse one one-way segment):
- FalkorDB generated the candidates itself: route each affected zone with `relDirection:'both'` and keep the one-way segments traversed backwards. That gave 26 candidates.
- Best: reversing 294 m of Velachery Main Road saves 362 m.
- Real, but small.

**Rule A** (37 hospitals) shows the same ordering with smaller magnitudes: Maraimalai +6.7 km, others mostly 0. The finding is robust to the assistance-point rule.

**Not found:** a single realistic closure that *disconnects* a neighbourhood. Both banks have hospitals, and the urban grid is redundant. The demo should claim cost increases and recovery priority, not cut-offs.

`Velachery` is unreachable even at baseline: its anchor sits at the bbox edge. It's excluded from claims and will be fixed by snapping or a larger bbox.

## Measured performance (8,458-segment network)

| Operation | n | Median | p95 | Min | Max |
|---|---|---|---|---|---|
| `GRAPH.COPY` (full network) | 69 | 21.6 ms | 28.7 ms | 20.9 ms | 37.0 ms |
| Close/reverse roads in clone | 69 | 1.6 ms | 1.8 ms | 0.9 ms | 4.1 ms |
| Access for all 46 zones (46 × `SPpaths`) | 71 | 25.5 ms | 28.9 ms | 17.2 ms | 30.9 ms |
| Import (nodes + edges + indexes) | 2 | about 0.5 s | | | |

A full scenario (clone + mutate + 46 routes + delete) takes about 50 ms, so 10 candidate interventions take under 1 s.

**Caveat:** `SPpaths` with `pathCount > 1` occasionally hit the server's default 1 s `TIMEOUT` on denser random graphs. Use `pathCount: 1` and generate alternatives by closing segments in a clone.

## Working FalkorDB operations (6.0.2)

- `algo.SPpaths`: decimal weights, `relDirection` `outgoing`/`both`, and an empty result when unreachable.
- `GRAPH.COPY`: clones are isolated.
- `UNWIND … CREATE/DELETE` for mutation.
- Edge-property index on `eid`.
- `algo.BFS` (outgoing only).
- Note: `algo.SSpaths` caps the *total* paths returned, not paths per node.

## Risks

1. **Drama is lower than max-flow.** The story is "detour cost and recovery priority", not "capacity collapse"; no neighbourhood was naturally cut off.
2. **Distance is a proxy.** There's no travel time or flood depth. Any time estimate is an assumption.
3. **The agent's value must be visible.** For example, resolving "Saidapet bridge" to Maraimalai Adigal Bridge is real alias knowledge.
4. Zone anchors are snapped single junctions, a coarse stand-in for a neighbourhood.
