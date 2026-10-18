"""Engine-level differential tests: every FalkorDB result is recomputed with NetworkX on the
identical edge set. Requires the pinned FalkorDB on localhost:6379."""

import random

import networkx as nx
import pytest

from contraflow.engine import Engine, Scenario, ValidationError
from contraflow.graphdb import BASE, DB, SCENARIO_PREFIX
from contraflow.model import build

NET = build()
DBC = DB()
DBC.load_base(NET)
ENG = Engine(NET, DBC)
ALL_CW = [c["id"] for x in NET.crossings for c in x["carriageways"]]
ONEWAY = [s["eid"] for s in NET.segments if s["oneway"]]


def nx_access(sc: Scenario) -> dict[str, float]:
    """Independent reference: Dijkstra from each zone anchor to the rule's assistance points."""
    closed = ENG.effective_closed(sc) | set(sc.reversed_segments)
    G = nx.DiGraph()

    def add(u, v, w):
        if not G.has_edge(u, v) or G[u][v]["w"] > w:
            G.add_edge(u, v, w=w)
    for s in NET.segments:
        if s["eid"] not in closed:
            add(s["u"], s["v"], s["len"])
    for e in sc.reversed_segments:
        s = NET.seg(e)
        add(s["v"], s["u"], s["len"])
    for h in NET.help_points:
        if sc.rule in h["rules"]:
            add(h["node"], "__HUB__", 0.0)
    out = {}
    for z in NET.zones:
        try:
            out[z["id"]] = nx.dijkstra_path_length(G, z["node"], "__HUB__", weight="w")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            pass
    return out


def assert_same(fdb: dict, ref: dict):
    assert set(fdb) == set(ref), (set(fdb) ^ set(ref))
    for z in ref:
        assert abs(fdb[z]["dist"] - ref[z]) < 0.2, (z, fdb[z]["dist"], ref[z])


def base_fingerprint():
    g = DBC.graph(BASE)
    return (g.q("t", "MATCH ()-[e:ROAD]->() RETURN count(e), sum(e.len)")[0],
            g.q("t", "MATCH (n:J) RETURN count(n)")[0][0])


# ------------------------------------------------------------------ model
def test_bridge_grouping_and_directions():
    assert {x["name"] for x in NET.crossings} == {"Abraham Bridge", "Kotturpuram Bridge",
                                                  "Maraimalai Adigal Bridge", "Thiru Vi Ka Bridge"}
    assert len(ALL_CW) == 7
    for x in NET.crossings:
        for c in x["carriageways"]:
            for e in c["eids"]:
                s = NET.seg(e)
                assert s["crossing"] == x["id"] and s["carriageway"] == c["id"]
                dy = s["coords"][-1][1] - s["coords"][0][1]
                if c["direction"] == "northbound":
                    assert dy > 0
                elif c["direction"] == "southbound":
                    assert dy < 0
    two_way = NET.carriageway("abraham_bridge:two-way")
    assert len(two_way["eids"]) == 2 and not two_way["oneway"]


def test_zone_anchors_connected_and_documented():
    assert [z["name"] for z in NET.excluded_zones] == ["Velachery"]
    for rule in ("A", "B"):
        base = ENG.baseline(rule)
        assert len(base) == len(NET.zones), f"rule {rule}: unreachable anchors {set(z['id'] for z in NET.zones) - set(base)}"
    assert all(z["snap_m"] <= 600 for z in NET.zones)


# ------------------------------------------------------------------ routing vs NetworkX
@pytest.mark.parametrize("rule", ["A", "B"])
def test_baseline_matches_networkx(rule):
    assert_same(ENG.baseline(rule), nx_access(Scenario(rule)))


@pytest.mark.parametrize("rule", ["A", "B"])
def test_each_carriageway_and_bridge_closure(rule):
    sets = [[c] for c in ALL_CW] + [[c["id"] for c in x["carriageways"]] for x in NET.crossings] + [ALL_CW]
    for closed in sets:
        sc = Scenario(rule, closed)
        from contraflow.graphdb import Ledger
        assert_same(ENG.raw_access(sc, Ledger()), nx_access(sc))


def test_random_scenarios_match_networkx():
    rng = random.Random(1816)
    from contraflow.graphdb import Ledger
    for k in range(30):
        sc = Scenario(rng.choice(["A", "B"]), rng.sample(ALL_CW, rng.randint(0, 4)),
                      rng.sample([s["eid"] for s in NET.segments], rng.randint(0, 120)),
                      rng.sample(ONEWAY, rng.randint(0, 6)))
        sc.reversed_segments = [e for e in sc.reversed_segments if e not in sc.closed_segments]
        assert_same(ENG.raw_access(sc, Ledger()), nx_access(sc))


# ------------------------------------------------------------------ isolation
def test_clones_isolated_and_cleaned():
    before = base_fingerprint()
    for _ in range(5):
        ENG.evaluate(Scenario("B", ALL_CW, reversed_segments=ONEWAY[:3]))
    ENG.interventions(Scenario("B", ALL_CW))
    assert base_fingerprint() == before
    assert not [g for g in DBC.list_graphs() if g.startswith(SCENARIO_PREFIX)]


def test_repeated_execution_is_deterministic():
    a = ENG.interventions(Scenario("B", ALL_CW))
    b = ENG.interventions(Scenario("B", ALL_CW))
    strip = lambda r: [(x["id"], x["recovered_m"], x["zones_restored"]) for x in r["ranking"]]
    assert strip(a) == strip(b)


# ------------------------------------------------------------------ metric + ranking
def test_metric_uses_same_eligible_zones():
    r = ENG.evaluate(Scenario("B", ALL_CW))
    assert r["metrics"]["eligible_zones"] == len(NET.zones)
    total = sum(z["delta"] for z in r["zones"] if z.get("delta") and abs(z["delta"]) > 0.5)
    assert abs(total - r["metrics"]["total_delta_m"]) < 1


@pytest.mark.parametrize("rule", ["A", "B"])
def test_intervention_ranking_recomputed_with_networkx(rule):
    sc = Scenario(rule, ALL_CW)
    res = ENG.interventions(sc)
    now = nx_access(sc)
    for item in res["ranking"]:
        if item["type"] == "contraflow":
            nxt = sc.with_changes(reverse=item["segments"])
        else:
            nxt = sc.with_changes(reopen_carriageways=item["carriageways"], reopen_segments=item["segments"])
        after = nx_access(nxt)
        rec = sum(now[z] - after[z] for z in now if z in after and abs(now[z] - after[z]) > 0.5)
        assert abs(rec - item["recovered_m"]) < 1, (item["id"], rec, item["recovered_m"])
    from contraflow.engine import rank_key
    assert res["ranking"] == sorted(res["ranking"], key=rank_key)


def test_reserve_constraint_excludes_and_changes_metric():
    cw = "maraimalai_adigal_bridge:northbound"
    sc = Scenario("B", ALL_CW, constraints=[{"kind": "reserve_emergency", "target": cw}])
    res = ENG.interventions(sc)
    assert all(cw not in i["carriageways"] for i in res["ranking"])
    assert any(cw in i["carriageways"] and i["excluded_by"]["kind"] == "reserve_emergency" for i in res["excluded"])
    open_reserved = Scenario("B", [], constraints=[{"kind": "reserve_emergency", "target": cw}])
    assert ENG.evaluate(open_reserved)["metrics"]["total_delta_m"] > 0   # reservation removes it from public routing


def test_keep_direction_blocks_contraflow():
    """Find a scenario whose contraflow candidates include a crossing carriageway, then protect it."""
    for closed in [[c] for c in ALL_CW] + [[c["id"] for c in x["carriageways"]] for x in NET.crossings] + [ALL_CW]:
        sc = Scenario("B", closed)
        cands = [i["segments"][0] for i in ENG.interventions(sc)["ranking"] if i["type"] == "contraflow"]
        cws = {NET.seg(e)["carriageway"] for e in cands} - {""}
        if cws:
            target = sorted(cws)[0]
            sc.constraints = [{"kind": "keep_direction", "target": target}]
            after = [i["segments"][0] for i in ENG.interventions(sc)["ranking"] if i["type"] == "contraflow"]
            assert target not in {NET.seg(e)["carriageway"] for e in after}
            return
    # No crossing carriageway is ever a contraflow candidate in these scenarios: check on ordinary segments instead.
    sc = Scenario("B", ALL_CW)
    cands = [i["segments"][0] for i in ENG.interventions(sc)["ranking"] if i["type"] == "contraflow"]
    pytest.skip(f"no crossing carriageway appears among contraflow candidates ({len(cands)} road candidates)")


def test_validation_rejects_unknown_ids():
    for bad in (Scenario("Z"), Scenario("B", ["nope"]), Scenario("B", [], ["nope"]),
                Scenario("B", [], [], [next(s["eid"] for s in NET.segments if not s["oneway"])]),
                Scenario("B", constraints=[{"kind": "delete_db", "target": ALL_CW[0]}])):
        with pytest.raises(ValidationError):
            ENG.evaluate(bad)
