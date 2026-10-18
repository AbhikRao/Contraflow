"""Evidence-backed incident case files.

A case file is assembled from the investigation-memory graph by one multi-hop traversal (Memory.case_file) plus a fresh
re-evaluation of every stored finding against the scenario on screen. Nothing in it is written by a language model:
every field is a stored tool result, a stored provenance record, or a re-computation."""
from __future__ import annotations

from .agent.intent import parse_intent
from .agent.tools import ToolBox
from .engine import Scenario


def _scenario_text(net, sc: dict) -> str:
    cws = sc.get("closed_carriageways") or []
    names = []
    for c in cws:
        try:
            cw = net.carriageway(c)
            names.append(f"{cw['crossing_name']} {cw['direction']}")
        except Exception:  # noqa: BLE001
            names.append(c)
    parts = [", ".join(names) + " closed"] if names else ["no closures"]
    if sc.get("closed_segments"):
        parts.append(f"{len(sc['closed_segments'])} road segment(s) closed")
    if sc.get("constraints"):
        parts.append("; ".join(f"{k['kind'].replace('_', ' ')} on {k['target']}" for k in sc["constraints"]))
    return ", ".join(parts) + f" · rule {sc.get('rule', '?')}"


def _cw_name(net, cid: str) -> str:
    try:
        cw = net.carriageway(cid)
        return f"{cw['crossing_name']} {cw['direction']}"
    except Exception:  # noqa: BLE001
        return cid


def build_case(state: dict, iid: str, current: Scenario) -> dict | None:
    mem, net = state["memory"], state["net"]
    cf = mem.case_file(iid)
    if cf is None:
        return None
    tb = ToolBox(state, current, None, user_msg="", intent=parse_intent("earlier finding"))
    rechecks = []
    for f in cf["findings"]:
        if f["kind"] in ("disruption_impact", "recovery_ranking"):
            rechecks.append({"finding": f["kind"], "then": f["summary"],
                             **tb.recheck_finding({"kind": f["kind"], "hypothesis": f["hypothesis"], "metrics": f["metrics"],
                                                   "scenario_key": f["scenario_key"]})})
    impact = next((f for f in cf["findings"] if f["kind"] == "disruption_impact"), None)
    ranking = next((f for f in cf["findings"] if f["kind"] == "recovery_ranking"), None)
    vuln = next((f for f in cf["findings"] if f["kind"] == "vulnerability"), None)
    trace = cf["trace"] or {}
    steps = [s for s in trace.get("steps", []) if s.get("kind") in ("tool", "error")]
    compare = next((s for s in steps if s.get("tool") == "compare_options" and s.get("kind") == "tool"), None)
    entities = []
    for r in cf["reports"]:
        if r.get("place") and r["place"] not in [e["name"] for e in entities]:
            entities.append({"name": r["place"], "kind": r.get("place_kind"), "resolution": r.get("resolution"),
                             "method": r.get("method")})
    assumptions = sorted({a for f in cf["findings"] for a in f["assumptions"]})
    top = (ranking or {}).get("metrics", {}).get("top", [])
    helpful = [t for t in top if t.get("effect") in ("restores_access", "shorter")]
    decided = [p for p in cf["proposals"] if p.get("decision")]
    case = {
        "investigation_id": iid, "recorded_at": cf["ts"], "mode": cf["mode"],
        "agent": {"answered_by": (trace.get("mode") or {}).get("label"), "briefing_source": trace.get("briefing_source"),
                  "elapsed_ms": trace.get("elapsed_ms")},
        "report": {"text": cf["report_text"], "status": "reported, unverified",
                   "provenance": sorted({r["provenance"] for r in cf["reports"] if r.get("provenance")}) or ["reported_unverified"]},
        "resolved_entities": entities,
        "scenario_then": {"description": _scenario_text(net, cf["scenario"]), "scenario_key": cf["scenario_key"],
                          "road_graph_fingerprint": (cf["model_fingerprint"] or "")[:16]},
        # the typed constraint parsed from the report (its own clause and direction), not re-derived from text
        "commander_constraints": ([{"kind": cf["intent"]["aux_constraint"], "clause": cf["intent"].get("aux_text"),
                                    "direction": cf["intent"].get("aux_direction") or "all directions"}]
                                  if cf["intent"].get("aux_constraint") else []),
        "assumptions": assumptions,
        "impact": None if not impact else {
            "hypothesis": impact["hypothesis"].get("label"), "added_km": impact["metrics"].get("added_km"),
            "zones_affected": impact["metrics"].get("zones_affected"), "zones_cut_off": impact["metrics"].get("zones_cut_off"),
            "affected": sorted(impact["affects"], key=lambda a: -(a["delta_m"] or 0))},
        "alternatives": top,
        "comparison": compare.get("summary") if compare else None,
        "recommendation": (helpful[0]["action"] if helpful else ("no evaluated repair improves access" if ranking else None)),
        "recommendation_basis": ("objective: never cut off a zone, then restore access, then recover the most modeled distance; "
                                 "evaluated on graph copies under the hypothesis and assumptions above") if ranking else None,
        "sensitivity": vuln["metrics"] if vuln else None,
        "agent_steps": steps,
        "graph_operations": trace.get("graph_operations", {}),
        "graph_operation_count": trace.get("graph_operation_count"),
        "briefing": trace.get("briefing"),
        "human_decision": ([f"{p['label']}: {'applied by the user' if p['state'] == 'applied' else 'rejected by the user'}"
                            for p in decided] or [f"{p['label']}: proposed, undecided" for p in cf["proposals"]]
                           or ["no change was proposed by this investigation; nothing was applied"]),
        "current_status": {"evaluated_against": _scenario_text(net, current.to_dict()),
                           "scenario_key": current.key(), "rechecks": rechecks},
        "assembled_by": cf["assembled_by"],
    }
    case["markdown"] = to_markdown(case)
    return case


def case_robustness(state: dict, iid: str, current: Scenario) -> dict | None:
    """'Test assumptions' for a case file: the investigation's stored hypothesis is applied to the scenario on screen and
    its recommendation is re-ranked under a bounded set of single-assumption variations (Engine.robustness). The stored
    recommendation is only compared with the fresh one, never reused."""
    cf = state["memory"].case_file(iid)
    if cf is None:
        return None
    ranking = next((f for f in cf["findings"] if f["kind"] == "recovery_ranking"), None)
    if ranking is None:
        return {"available": False, "reason": "this investigation did not rank any repair, so there is no recommendation "
                                              "to test"}
    tb = ToolBox(state, current, None, user_msg="", intent=parse_intent("earlier finding"))
    hyp = ranking.get("hypothesis") or {}
    sc = tb._apply_delta(current, hyp)
    out = state["engine"].robustness(sc)
    stored = next((t["action"] for t in ranking["metrics"].get("top", []) if t.get("effect") in ("restores_access", "shorter")),
                  None)
    fresh = (out["original"] or {}).get("label")
    ledger = out.pop("ledger")
    return {"available": True, "investigation_id": iid, **out,
            "tested_on": _scenario_text(state["net"], {**sc.to_dict(), "constraints": [
                {**k, "target": _cw_name(state["net"], k["target"])} for k in sc.constraints]}),
            "evaluated_against_key": current.key(),
            "stored_recommendation": stored,
            "stored_matches_fresh": stored == fresh,
            "graph_operations": {"count": ledger["queries"], "total_ms": ledger["total_ms"],
                                 "by_op": {k: v["count"] for k, v in ledger["by_op"].items()}}}


def to_markdown(c: dict) -> str:
    L = [f"# Case file {c['investigation_id']}", "",
         f"Recorded {c['recorded_at']} · answered by {c['agent']['answered_by'] or c['mode']} · "
         f"briefing source: {c['agent']['briefing_source'] or 'n/a'}", "",
         "## Report", f"> {c['report']['text']}", "", f"Status: **{c['report']['status']}** ({', '.join(c['report']['provenance'])})", ""]
    if c["resolved_entities"]:
        L += ["## Resolved graph entities"] + [f"- {e['name']} ({e['kind']}; {e['resolution']}; {e['method']})"
                                               for e in c["resolved_entities"]] + [""]
    L += ["## Scenario at the time", f"- {c['scenario_then']['description']}",
          f"- scenario key `{c['scenario_then']['scenario_key']}` · road-graph fingerprint `{c['scenario_then']['road_graph_fingerprint']}`", ""]
    if c["commander_constraints"]:
        L += ["## Commander constraint (from the report)"] + [f"- {k['kind'].replace('_', ' ')} — “{k['clause']}” ({k['direction']})"
                                                              for k in c["commander_constraints"]] + [""]
    if c["assumptions"]:
        L += ["## Assumptions (analysis only, never applied)"] + [f"- {a}" for a in c["assumptions"]] + [""]
    if c["impact"]:
        i = c["impact"]
        L += ["## Modeled impact (graph copy)", f"- Hypothesis: {i['hypothesis']} closed",
              f"- +{i['added_km']} km modeled distance across {i['zones_affected']} neighbourhood(s); cut off: {i['zones_cut_off'] or 'none'}"]
        L += [f"  - {a['zone']}: {a['status']} {'' if a['delta_m'] is None else f'+{a['delta_m'] / 1000:.2f} km'}" for a in i["affected"]] + [""]
    if c["alternatives"]:
        L += ["## Alternatives evaluated"] + [f"- {a['action']} — {a['effect'].replace('_', ' ')}, recovers {a['recovered_km']} km"
                                              for a in c["alternatives"]]
        if c["comparison"]:
            L.append(f"- Comparison: {c['comparison']}")
        L += ["", f"## Recommendation\n**{c['recommendation']}** — {c['recommendation_basis']}", ""]
    if c["agent_steps"]:
        L += ["## What the agent executed"] + [f"{n}. `{s['tool']}`{' (rejected)' if s['kind'] == 'error' else ''} — {s['summary']}"
                                               f"{'' if s.get('ms') is None else f' ({s['ms']} ms, {s.get('queries') or 0} graph ops)'}"
                                               for n, s in enumerate(c["agent_steps"], 1)]
        L += [f"\nGraph operations ({c['graph_operation_count']}): " + ", ".join(f"{k} ×{v}" for k, v in c["graph_operations"].items()), ""]
    L += ["## Human decision"] + [f"- {d}" for d in c["human_decision"]] + [""]
    L += ["## Status now", f"Re-evaluated against: {c['current_status']['evaluated_against']}"]
    L += [f"- {r['finding']}: **{r.get('still_applies', 'n/a')}** — then: {r['then']}" + (f"; now: {r['now']}" if r.get("now") else "")
          for r in c["current_status"]["rechecks"]] + [""]
    L += ["## Provenance", f"Assembled from FalkorDB graph `{c['assembled_by']['graph']}` by one traversal:", "",
          "```cypher", c["assembled_by"]["cypher"], "```",
          "", "_Modeled shortest-path distances on the OpenStreetMap road graph; not travel times, capacity or safety advice._"]
    return "\n".join(L)
