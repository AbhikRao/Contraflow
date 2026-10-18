"""Graph-grounded briefing: composes the agent's primary answer strictly from this turn's tool results.

Whatever planner chose the tools (LLM or deterministic), the user-facing briefing is generated here,
so it can only restate what FalkorDB-backed tools returned.
"""

from __future__ import annotations


def km(x: float) -> str:
    return f"{x:.1f} km" if abs(x) >= 0.05 else f"{x * 1000:.0f} m"


def ranking_sentence(rk: dict, full: bool = False) -> str:
    if "error" in rk:
        return f"Ranking failed: {rk['error']}"
    good = [r for r in rk["ranking"] if r.get("effect") in ("restores_access", "shorter")]
    harm = rk.get("harmful", [])
    tail = ""
    if harm:
        cuts = [h for h in harm if h["effect"] == "loses_access"]
        tail = (f" {len(harm)} candidate(s) would make access worse and are not recommended"
                + (f" ({len(cuts)} would cut off zones: " + "; ".join(f"{h['action']} → {', '.join(h['cuts_off'])}"
                                                               for h in cuts[:2]) + ")" if cuts else "") + ".")
    if rk.get("excluded_by_constraints"):
        tail += " Excluded by constraints: " + ", ".join(e["action"] for e in rk["excluded_by_constraints"]) + "."
    if not good:
        return f"No eligible intervention improves access (current added distance {km(rk['current_added_km'])})." + tail
    b = good[0]
    gain = []
    if b["zones_restored"]:
        gain.append(f"restoring road access for {', '.join(b['zones_restored'])}")
    if b["recovered_km"] > 0:
        gain.append(f"recovering {km(b['recovered_km'])}" + (
            f" ({b['share_of_current_disruption_pct']}% of the current {km(rk['current_added_km'])} added)"
            if b["share_of_current_disruption_pct"] is not None else ""))
    s = f"Best modeled recovery of {rk['candidates_evaluated']} candidates: {b['action']}, {' and '.join(gain)}."
    if full and len(good) > 1:
        s += " Next: " + "; ".join(f"{r['action']} ({km(r['recovered_km'])})" for r in good[1:4]) + "."
    if full and rk["no_effect"]:
        s += f" {len(rk['no_effect'])} candidate(s) have no effect on this metric."
    return s + tail


def _impact(p: dict) -> str:
    s = f"modeled impact +{km(p['added_km'])} across {p['zones_affected']} zone(s)"
    if p.get("largest"):
        s += f", largest {p['largest'][0]['zone']} (+{km(p['largest'][0]['added_km'])})"
    if p.get("zones_cut_off"):
        s += f"; cut off: {', '.join(p['zones_cut_off'])}"
    return s


def compose(steps: list[dict], work_key: str | None = None) -> tuple[str, bool]:
    """Returns (briefing, needs_user_choice)."""
    out, ask = [], False
    ranked = False
    for st in steps:
        if st.get("kind") != "tool":
            continue
        t, r = st["tool"], st["result"]
        if t == "investigate_disruption" and r.get("status") in ("ambiguous", "not_found"):
            out.append(unresolved_sentence(r))       # investigations never change the scenario; say what is unclear
            ask = ask or any(c.get("kind") == "crossing" for c in r.get("candidates", []))
            continue
        if r.get("needs_confirmation") or (t == "search_location" and r.get("status") in ("ambiguous", "not_found")):
            if r.get("status") == "not_found":
                out.append(f"I could not match this to the modeled network: {r.get('reason')}")
            else:
                out.append(f"I won't change the scenario until the location is confirmed: {r.get('reason')}")
                ask = True
            continue
        if t == "assess_field_report" and "proposal" in r:
            dirn = (" The report states no direction, so every carriageway is included." if r.get("direction") == "all"
                    else f" Direction: {r['direction']} only." if r.get("direction") else "")
            out.append(f"Resolved the report to {r['resolved']}: {r.get('how')}.{dirn} "
                       f"Proposed: {r['proposal']} — {_impact(r)}. Report {r['report_id']} recorded as evidence.")
            if r["added_km"] <= 0.0005 and not r.get("zones_cut_off"):
                out.append("No anchor's shortest route to its nearest assistance point used it, so this metric is "
                           "unchanged; that does not mean the road is unimportant.")
        elif t == "propose_closure" and not any(s.get("tool") == "assess_field_report" for s in steps):
            out.append(f"Proposed: {r['proposal']} — {_impact(r)}. Report {r['report_id']} recorded.")
        elif t == "propose_reopen":
            out.append(f"Proposed: {r['proposal']}, recovering {km(r['recovered_km'])}; the plan would then be "
                       f"{km(r['vs_baseline_km'])} above baseline.")
        elif t == "set_planning_constraint" and "constraint" in r:
            out.append(f"Proposed constraint — {r['constraint'].replace('_', ' ')} on {r['applies_to']}: {r['meaning']}")
        elif t == "explain_crossing" and "crossing" in r:
            ev = r["closing_it"]
            if not r["baseline_zone_routes_using"]:
                out.append(f"No neighbourhood anchor's shortest directed route to its nearest assistance point crosses "
                           f"{r['crossing']} in the baseline, so closing it changes this metric by {km(ev['added_km'])}. "
                           "That does not make it unimportant: other destinations, directions and capacity are not "
                           "measured here.")
            else:
                out.append(f"{len(r['baseline_zone_routes_using'])} anchor route(s) use {r['crossing']} in the baseline "
                           f"({', '.join(r['baseline_zone_routes_using'])}); closing it: {_impact(ev)}.")
        elif t == "evaluate_closure" and "added_km" in r and not any(s.get("tool") == "explain_crossing" for s in steps):
            out.append(f"Closing {r['closure']} (hypothetical): {_impact(r)}.")
        elif t == "investigate_disruption" and "affected" in r:
            out.append(investigation_sentence(r))
        elif t == "assume_constraint" and r.get("code") == "conflicting_scope":
            out.append(f"Constraint not assumed — {r['error']}.")
        elif t == "assume_constraint" and r.get("assumed"):
            out.append(f"Assumed for this analysis only (not proposed): {r['assumed']}.")
        elif t == "rank_recovery" and "ranking" in r:
            cons = ", ".join(r.get("hypothesis") or []) or "current scenario"
            out.append(f"Under the hypothesis ({cons}): " + ranking_sentence(r, full=True))
        elif t == "compare_options" and r.get("winner"):
            out.append(compare_sentence(r))
        elif t == "trace_route" and r.get("zone"):
            out.append(route_sentence(r))
        elif t == "find_critical_roads" and "most_critical" in r:
            out.append(vulnerability_sentence(r))
        elif t == "recall_memory" and "findings" in r:
            out.append(recall_sentence(r))
        elif t == "rank_interventions" and "ranking" in r and not ask:
            # The recommendation must come from the LATEST ranking evaluated for the final working scenario;
            # earlier rankings are reported only as labelled historical comparisons.
            ranks = [x for x in steps if x.get("kind") == "tool" and x.get("tool") == "rank_interventions"
                     and "ranking" in (x.get("result") or {})]
            current = [x for x in ranks if work_key is None or x["result"].get("scenario_key") == work_key]
            latest = current[-1] if current else None
            if st is latest:
                proposed = r.get("evaluated_for") == "proposed scenario"
                out.append(("If the proposal is applied (constraints: " + (", ".join(r.get("constraints") or []) or "none")
                            + "): " if proposed else "Current recommendation: ") + ranking_sentence(r, full=True))
            elif not ranked:
                good = [c for c in r["ranking"] if c.get("effect") in ("restores_access", "shorter")]
                if good:
                    out.append(f"Earlier ranking (historical, before the proposed change): best was {good[0]['action']} "
                               f"({km(good[0]['recovered_km'])}). It is superseded by the ranking below.")
            ranked = True
        elif t == "inspect_scenario" and "added_km_vs_baseline" in r:
            out.append(f"Current scenario (rule {r['rule']}): {len(r['closed'])} closure(s); modeled access is "
                       f"{km(r['added_km_vs_baseline'])} above baseline across {r['zones_affected']} zone(s).")
        elif r.get("code") == "ambiguous_location" and not ask:
            out.append("I won't change the scenario until the location is confirmed: the place named in the request "
                       "matches more than one crossing.")
            ask = True
        elif "error" in r and t in ("propose_closure", "set_planning_constraint", "propose_reopen"):
            out.append(f"Could not complete {t.replace('_', ' ')}: {r['error']}")
    if any(st.get("tool") == "investigate_disruption" and "affected" in (st.get("result") or {}) for st in steps):
        out.append("Nothing has been changed: this is an investigation on graph copies. Use a suggested next step to "
                   "turn it into a proposal for review.")
    if any(st.get("tool", "").startswith(("propose", "assess", "set_")) and "error" not in st.get("result", {})
           and not st["result"].get("needs_confirmation") for st in steps if st.get("kind") == "tool"):
        out.append("PROPOSED, not applied: nothing changes until you click Apply.")
    return " ".join(out), ask


def _via(crossings: list[str]) -> str:
    return "via " + ", ".join(crossings) if crossings else "without crossing the river"


def unresolved_sentence(r: dict) -> str:
    if r.get("status") == "not_found":
        return ("I could not match that location to any crossing, road or neighbourhood in the modeled Adyar corridor, "
                "so nothing was evaluated and no figures are given.")
    names = sorted({c["name"] for c in r.get("candidates", [])})[:4]
    reason = (r.get("reason") or "several places match").strip()
    hint = "" if "name a nearby" in reason.lower() else (
        " Name the road together with a nearby neighbourhood (for example “… near Guindy”) or select the section on "
        "the map.")
    return (f"The location is ambiguous, so nothing was evaluated: {reason.rstrip('.')}."
            + (f" Matches include: {', '.join(names)}." if names and len(names) > 1 else "") + hint)


def investigation_sentence(r: dict) -> str:
    s = f"Investigation (hypothesis, not applied): {r['resolved']}" + (f" — {r['how']}" if r.get("how") else "")
    if r.get("fully_already_closed"):
        s += (". It is already closed in the current scenario, so this hypothesis adds nothing; the analysis below is "
              "for the current scenario")
    elif r["zones_affected"] == 0 and not r.get("zones_cut_off"):
        s += (". If closed: no modeled change — every neighbourhood keeps an equally short route to its nearest "
              "assistance point (no current route depends on it, or an equal-length alternative exists)")
    else:
        s += f". If closed: +{km(r['added_km'])} modeled distance across {r['zones_affected']} zone(s)"
    if r.get("zones_cut_off"):
        s += f"; CUT OFF: {', '.join(r['zones_cut_off'])}"
    longer = [a for a in r["affected"] if a["status"] == "longer"][:3]
    if longer:
        s += ". Most affected: " + "; ".join(
            f"{a['zone']} +{km(a['delta_m'] / 1000)} (its route used {', '.join(a['route_used']) or 'no river crossing'}; "
            f"now {_via(a['reroutes_via'])} to {a.get('nearest_help_now', '?')})" for a in longer)
    if r.get("conflicting_reports"):
        s += (f". Note: {len(r['conflicting_reports'])} earlier report(s) about this place say otherwise "
              f"(e.g. “{r['conflicting_reports'][0]['text'][:80]}”) — the evidence conflicts")
    elif r.get("earlier_reports_here"):
        s += f". {len(r['earlier_reports_here'])} earlier report(s) mention this place"
    return s + "."


def compare_sentence(r: dict) -> str:
    opts = [o for o in r["options"] if "error" not in o]
    s = f"Why {r['winner']} is preferred: " + " vs ".join(f"{o['action']} recovers {km(o['recovered_km'])}" for o in opts[:2])
    if r.get("per_zone_differences"):
        d = r["per_zone_differences"][0]
        fa = f"{d['option_a_km']} km" if isinstance(d["option_a_km"], float) else "cut off"
        fb = f"{d['option_b_km']} km" if isinstance(d["option_b_km"], float) else "cut off"
        s += (f". Largest difference: {d['zone']} — {fa} {_via(d['option_a_route_crossings'])} with the first option, "
              f"{fb} {_via(d['option_b_route_crossings'])} with the second")
    return s + "."


def route_sentence(r: dict) -> str:
    parts = []
    for k in ("current", "hypothesis"):
        v = r.get(k)
        if isinstance(v, dict):
            parts.append(f"{k}: cut off" if v.get("status") == "cut off" else
                         f"{k}: {v['km']} km {_via(v['crossings'])} to {v['nearest_help']}")
    return f"Route of {r['zone']} — " + "; ".join(parts) + "."


def recall_sentence(r: dict) -> str:
    if not r["findings"]:
        return "No earlier findings were found in investigation memory for this request."
    lines = []
    for f in r["findings"][:3]:
        line = f"Earlier finding ({f['when']}): {f['then']}"
        if f.get("now"):
            line += f" Now: {f['now']}"
        line += f" — still applies: {f.get('still_applies', 'unknown')}."
        lines.append(line)
    return " ".join(lines)


def vulnerability_sentence(r: dict) -> str:
    def desc(x):
        d = f"{x['road']} ("
        d += f"cuts off {', '.join(x['zones_cut_off'])}; " if x["zones_cut_off"] else ""
        d += f"+{km(x['added_km'])} across {x['zones_longer']} zone(s)"
        if "access_to_assistance_point" in x["flags"]:
            d += "; it is the access link to an assistance point"
        return d + ")"
    s = (f"Vulnerability analysis: {r['evaluated']} candidate failures, each on its own graph copy (bounded set, not "
         f"exhaustive). Highest modeled access sensitivity: " + "; ".join(desc(x) for x in r["most_critical"][:3]) + ".")
    if r["less_obvious_but_high_impact"]:
        s += " Less obvious but high impact: " + "; ".join(x["road"] for x in r["less_obvious_but_high_impact"][:3]) + "."
    if r["prominent_but_low_impact"]:
        s += (" Prominent but no change on this metric: " + "; ".join(x["road"] for x in r["prominent_but_low_impact"][:4])
              + " (alternatives of equal length exist or no neighbourhood route uses them).")
    return s
