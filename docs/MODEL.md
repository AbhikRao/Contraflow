# Graph model and Cypher

Contraflow keeps one **base graph** named `contraflow` in FalkorDB 6.0.2. Every what-if runs on a disposable copy
(`GRAPH.COPY contraflow cf_sc_<id>`) that is deleted when the evaluation finishes. The base road graph is never
mutated; the only writes to it are `Report` evidence nodes from the planning agent.

## Nodes and relationships

```
(:J {id, x, y})                                         junction (OSM node id), 3,483
(:J)-[:ROAD {eid, len, name, highway, oneway, lanes,     directed drivable segment, 8,458
             crossing, carriageway}]->(:J)               (two-way road = two arcs; OSM one-way respected)

(:J {id:'__HUB_A__'|'__HUB_B__', virtual:true})          one virtual hub per assistance-point rule
(:J)-[:ROAD {eid:'hub:<rule>:<node>', len:0,             assistance-point junction -> hub (zero length)
             virtual:true}]->(hub)

(:Zone {id, name, lat, lon, snap_m})-[:ANCHORED_AT]->(:J)        45 neighbourhood anchors
(:Help {id, name, lat, lon, rules})-[:ANCHORED_AT]->(:J)         40 hospitals (rules A and/or B)

(:Crossing {id, name, roads, lat, lon})                           4 river bridges
  -[:HAS_CARRIAGEWAY]->(:Carriageway {id, direction, eids, oneway, lanes})   7 directed carriageways
(:Carriageway)-[:FROM]->(:J), (:Carriageway)-[:TO]->(:J)
(:Zone)-[:NEAR {m}]->(:Crossing)                                  straight-line proximity (≤ 3 km)

(:Report {id, text, ts, status, source})-[:REFERS_TO {method}]->(:Crossing|:Carriageway|:Zone)
(:Meta {fingerprint, attribution})
```

Indexes: range indexes on `J.id`, `ROAD.eid`, `Zone.id`, `Crossing.id`, `Carriageway.id`, `Report.id`.
Full-text indexes on `Crossing(name, roads)`, `Zone(name)` and `ROAD(name)`.

### Modeling decisions

| Decision | Reason |
|---|---|
| Virtual hub per rule with 0-length edges | One shortest-path query per zone gives the distance to the **nearest** assistance point of that rule. |
| Zone anchors snapped inside the largest strongly connected component; anchors > 600 m from it are excluded | Prevents artifacts from dead-end or out-of-extract snapping. Velachery is excluded: its place point is 1.9 km outside the extract. |
| River crossings = segments whose geometry intersects the OSM Adyar River line | Objective, not hand-picked. |
| Carriageways = connected crossing segments; bridges = carriageways sharing the "… Bridge" name | Distinguishes closing a whole bridge from closing one direction. |
| Contraflow = delete the one-way arc and add the reverse arc | Respects the physical lane budget; it does not invent capacity. |

## Constraint semantics (enforced in `Engine.validate` for every scenario)

| Constraint | Meaning | Rejected |
|---|---|---|
| `reserve_emergency` | carriageway removed from public routing | reversing any of its segments; it is never offered as a public reopening |
| `keep_direction` | direction must not change | reversing any of its segments |
| `no_reopen` | carriageway must stay closed | any scenario in which any of its segments is open (carriageway or segment reopen) |

Violations return HTTP 422 with `{"code": "constraint_violation", "kind", "target", "segment", "action"}`.

## The metric

For rule *r* and scenario *S*: `access(z) = shortest directed road distance from anchor(z) to HUB_r`.
Aggregate detour = Σ over zones reachable in **both** baseline and *S* of `access_S(z) − access_baseline(z)`.
Zones that lose access are reported separately ("cut off") and never folded into the sum. Interventions are classified relative to the current scenario as **loses_access** (some zone newly cut off),
**restores_access**, **shorter**, **longer**, **mixed** (equal gains and losses) or **no_change**, and ranked
lexicographically: fewest zones newly cut off, then most zones restored, then most net distance recovered
(Σ over zones connected before and after). A disconnection is never hidden inside the distance sum.

## Essential Cypher

**1. Access for every neighbourhood in one query** (`engine.py: ACCESS_QUERY`)

```cypher
MATCH (z:Zone)-[:ANCHORED_AT]->(a:J), (h:J {id: $hub})
CALL algo.SPpaths({sourceNode: a, targetNode: h, relTypes: ['ROAD'], weightProp: 'len'})
YIELD path, pathWeight
WITH z, pathWeight, relationships(path) AS rs
RETURN z.id, pathWeight,
       [r IN rs WHERE NOT coalesce(r.virtual, false) | r.eid],
       startNode(rs[size(rs) - 1]).id          // the assistance point actually reached
```

A zone missing from the result has no directed route (cut off). The returned edge ids drive the route drawing and
the "which zones use this carriageway" explanations.

**2. Scenario isolation**

```
GRAPH.COPY contraflow cf_sc_<uuid>
... mutate + query the copy ...
GRAPH.DELETE cf_sc_<uuid>
```

**3. Closures and contraflow in the copy**

```cypher
UNWIND $ids AS i MATCH ()-[e:ROAD {eid: i}]->() DELETE e
```

```cypher
UNWIND $rows AS r MATCH (a:J {id: r.u}), (b:J {id: r.v})
CREATE (a)-[:ROAD {eid: r.eid, len: r.len, name: r.name, oneway: true, contraflow: true}]->(b)
```

**4. Contraflow candidate generation** (`engine.py: CANDIDATE_QUERY`). For each zone hurt by the scenario, route while
ignoring direction; one-way arcs the path traverses backwards are candidates for reversal.

```cypher
MATCH (z:Zone {id: $zone})-[:ANCHORED_AT]->(a:J), (h:J {id: $hub})
CALL algo.SPpaths({sourceNode: a, targetNode: h, relTypes: ['ROAD'], weightProp: 'len', relDirection: 'both'})
YIELD path
RETURN [n IN nodes(path) | n.id], [r IN relationships(path) | [startNode(r).id, r.eid]]
```

**5. Field-report location resolution** (`evidence.py`)

```cypher
CALL db.idx.fulltext.queryNodes('Crossing', $q) YIELD node, score
RETURN node.id, node.name, node.roads, score ORDER BY score DESC
```

```cypher
CALL db.idx.fulltext.queryNodes('Zone', $q) YIELD node, score RETURN node.id, node.name, score ORDER BY score DESC
```

```cypher
UNWIND $zs AS zid MATCH (z:Zone {id: zid})-[n:NEAR]->(x:Crossing)
RETURN z.id, z.name, x.id, x.name, n.m ORDER BY n.m
```

```cypher
CALL db.idx.fulltext.queryRelationships('ROAD', $q) YIELD relationship, score
RETURN relationship.name, relationship.eid, score ORDER BY score DESC LIMIT 400
```

A crossing is resolved by proximity only when the report says "bridge", the nearest crossing is within 1.5 km, and it
is at least 1.6× and 500 m closer than the next one. Otherwise the result is **ambiguous** and the agent must ask.

**6. Evidence**

```cypher
CREATE (:Report {id: $id, text: $text, ts: $ts, status: $status, source: 'user-entered via planning agent (unverified)'})
```

```cypher
MATCH (r:Report {id: $rid}), (x:Crossing {id: $tid}) CREATE (r)-[:REFERS_TO {method: $m}]->(x)
```

**7. Crossing context for the agent**

```cypher
MATCH (x:Crossing {id: $id})-[:HAS_CARRIAGEWAY]->(c:Carriageway)
OPTIONAL MATCH (z:Zone)-[n:NEAR]->(x) WITH x, c, z, n ORDER BY n.m
RETURN c.id, c.direction, c.oneway, c.lanes, collect(DISTINCT [z.name, n.m])[0..4]
```

## What is computed outside FalkorDB

The Python engine coordinates queries, takes differences between scenario results, sums per-zone distances, orders
candidates, and formats explanations. It never computes routes at runtime. NetworkX is used in two places only:
offline preprocessing (`model.py` finds the largest strongly connected component to place zone anchors) and the test
suites as an independent reference. Contraflow candidates are a bounded heuristic (up to 24 one-way segments found by
direction-ignoring routes), not an exhaustive search over all reversals or combinations.
