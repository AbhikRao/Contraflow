"""Deterministic planner: rule-based orchestration (NO language model) driving the same typed tools.

It acts on a parsed Intent (see intent.py), so the requested action — close, reopen, constraint — is preserved
through clarification and can never become its opposite. Every sentence is filled from tool results.
"""

from __future__ import annotations

import re

from . import briefing
from .intent import CLOSE, RESERVE, Intent  # noqa: F401  (re-exported for runtime nudges)
from .tools import ToolBox

THIS = re.compile(r"\b(this|that|the same) (crossing|bridge)\b|\bit\b", re.I)


def km(x: float) -> str:
    return f"{x:.1f} km" if abs(x) >= 0.05 else f"{x * 1000:.0f} m"


def plan(intent: Intent, tb: ToolBox, history_focus: str | None, confirmed: str | None = None) -> tuple[str, dict | None]:
    a = intent.action

    if a == "negated":
        r = tb.call("search_location", {"text": intent.text})
        where = r["resolved"]["name"] if r.get("status") == "resolved" else "the named location"
        verb = {"close": "close", "reopen": "reopen", "reserve_emergency": "reserve for emergency vehicles"}
        state = {"close": "blocked", "reopen": "reopened", "reserve_emergency": "reserved for emergency vehicles"}
        if intent.mood == "imperative":
            return (f"Understood: do NOT {verb.get(intent.operation, intent.operation)} {where}. I have not proposed any "
                    "change, and nothing contradicting this instruction will be proposed in this turn."), None
        if intent.operation == "close":
            return (f"The report says {where} is NOT blocked, so I have not proposed any closure. If it was closed in this "
                    "scenario and has reopened, say so explicitly (e.g. “… has reopened”)."), None
        return (f"The report says {where} is NOT {state.get(intent.operation, intent.operation)}; "
                "no change proposed."), None

    if a == "constraint" and not intent.may_mutate:
        r = tb.call("search_location", {"text": intent.text})
        where = r["resolved"]["name"] if r.get("status") == "resolved" else "that crossing"
        op = intent.operation.replace("_", " ")
        return (f"That reads as a description, not an instruction, so I have not proposed a {op} constraint for "
                f"{where}. To propose one, give it as an instruction (for example: “Reserve {where} for emergency "
                "vehicles”)."), None

    if a in ("close", "reopen", "constraint", "why"):
        target, clar, res = _target(intent, tb, history_focus, confirmed)
        if clar:
            verb = {"close": "closed", "reopen": "reopened", "constraint": "constrained", "why": "explained"}[a]
            return f"I won't change anything until the location is confirmed: {clar['question']} (to be {verb})", clar
        if res and res.get("kind") == "road_section" and a == "close":
            return _section_flow(tb, intent, res), None
        if not target:
            reason = (res or {}).get("reason") or "no modeled crossing matched"
            return (f"I could not match this to a modeled river crossing: {reason}. Modeled crossings: "
                    + ", ".join(x["name"] for x in tb.net.crossings) + "."), None
        method = "your confirmation" if confirmed else (res or {}).get("method", "")
        return {"close": _closure_flow, "reopen": _reopen_flow, "constraint": _constraint_flow,
                "why": _why_flow}[a](tb, intent, target, method), None

    if a == "investigate":
        from .runtime import complete_investigation
        if confirmed:
            tb.authorize_context(confirmed)
        complete_investigation(tb, intent, history_focus)
        r = next((st["result"] for st in tb.steps if st.get("tool") == "investigate_disruption"), {})
        crossings = [c for c in r.get("candidates", []) if c.get("kind") == "crossing"]
        if r.get("needs_confirmation") and crossings:
            clar = {"question": f"{r.get('reason')} Which crossing should I investigate?",
                    "options": [{"label": c["name"] + (f" ({c['distance_m']} m)" if c.get("distance_m") else ""),
                                 "value": {"crossing_id": c["id"]}} for c in crossings],
                    "candidates": [c["id"] for c in crossings]}
            return "I won't guess the location for this investigation: " + clar["question"], clar
        return briefing.compose(tb.steps, tb.work.key())[0], None

    if a == "vulnerability":
        tb.call("find_critical_roads", {"top_links": 20})
        return briefing.compose(tb.steps, tb.work.key())[0], None

    if a == "recall":
        from .runtime import complete_investigation
        complete_investigation(tb, intent, history_focus)
        return briefing.compose(tb.steps, tb.work.key())[0], None

    if a == "rank":
        return _ranking_sentence(tb.call("rank_interventions", {"top_n": 4}), full=True), None

    if a == "status":
        s = tb.call("inspect_scenario", {})
        return (f"Current scenario (rule {s['rule']}): {len(s['closed'])} closure(s); modeled access is "
                f"{km(s['added_km_vs_baseline'])} above baseline across {s['zones_affected']} zone(s)"
                + (f"; cut off: {', '.join(s['zones_cut_off'])}" if s["zones_cut_off"] else "")
                + (f", worst {s['largest']['zone']} (+{km(s['largest']['added_km'])})." if s["largest"] else ".")), None

    return ("I can act on field reports and planning questions, for example: “Saidapet bridge is blocked”, "
            "“Kotturpuram bridge has reopened”, “What should we reopen first?”, “Keep Maraimalai Adigal available "
            "for inbound emergency access”, or “Why doesn't closing Thiru Vi Ka matter?”"), None


def _target(intent: Intent, tb: ToolBox, focus: str | None, confirmed: str | None):
    """Returns (crossing_id | None, clarification | None, resolution info)."""
    if confirmed:
        return confirmed, None, {"method": "your confirmation"}
    r = tb.call("search_location", {"text": intent.text})
    if r.get("status") == "resolved":
        res = r["resolved"]
        return (res["id"] if res["kind"] == "crossing" else None), None, {**res, "reason": r.get("reason")}
    crossings = [c for c in r.get("candidates", []) if c["kind"] == "crossing"]
    # A newly named (even ambiguous) location always outranks conversation focus; a pronoun cannot bypass ambiguity.
    if focus and THIS.search(intent.text) and r.get("status") == "not_found" and tb.authorize_context(focus):
        return focus, None, {"method": "the crossing under discussion"}
    if r.get("status") == "ambiguous" and crossings:
        return None, {"question": f"{r['reason']} Which crossing do you mean?",
                      "options": [{"label": c["name"] + (f" ({c['distance_m']} m)" if c.get("distance_m") else ""),
                                   "value": {"crossing_id": c["id"]}} for c in crossings],
                      "candidates": [c["id"] for c in crossings]}, r
    return None, None, r


def _closure_flow(tb: ToolBox, intent: Intent, xid: str, method: str) -> str:
    info = tb.call("inspect_crossing", {"crossing_id": xid})
    d = intent.direction or "all"
    p = tb.call("propose_closure", {"report_text": intent.text, "crossing_id": xid, "direction": d})
    if "error" in p:
        return f"I could not propose the closure: {p['error']}"
    dirs = [c["direction"] for c in info["carriageways"] if d == "all" or c["direction"] == d]
    text = (f"I read the report as {info['crossing']}" + (f" (via {method.replace('_', ' ')})" if method else "")
            + (f", {', '.join(dirs)} only" if intent.direction else ", all directions — the report gives no direction")
            + f". PROPOSED (not applied): close it; report {p['report_id']} recorded as evidence. Modeled impact: "
            f"+{km(p['added_km'])} across {p['zones_affected']} zone(s)")
    if p["largest"]:
        text += f"; largest increase {p['largest'][0]['zone']} (+{km(p['largest'][0]['added_km'])})"
    if p["zones_cut_off"]:
        text += f"; CUT OFF: {', '.join(p['zones_cut_off'])}"
    text += "."
    if p["added_km"] > 0 or p["zones_cut_off"]:
        text += " If applied: " + _ranking_sentence(tb.call("rank_interventions", {"top_n": 3}))
    else:
        text += (" No anchor's shortest route to its nearest assistance point used this crossing, so this metric "
                 "is unchanged (that does not mean the bridge is unimportant).")
    return text + " Apply the proposal to update the scenario."


def _reopen_flow(tb: ToolBox, intent: Intent, xid: str, method: str) -> str:
    p = tb.call("propose_reopen", {"crossing_id": xid, "direction": intent.direction or "all"})
    if "error" in p:
        return f"No reopening proposed: {p['error']}."
    text = (f"PROPOSED (not applied): {p['proposal']}" + (f" (via {method})" if method else "")
            + f". Modeled distance recovered: {km(p['recovered_km'])}; the plan would then be "
            f"{km(p['vs_baseline_km'])} above baseline.")
    rk = tb.call("rank_interventions", {"top_n": 3})
    if rk.get("ranking"):
        text += " If applied: " + _ranking_sentence(rk)
    return text + " Apply it to update the scenario."


def _constraint_flow(tb: ToolBox, intent: Intent, xid: str, method: str) -> str:
    c = tb.call("set_planning_constraint", {"kind": intent.constraint_kind, "crossing_id": xid,
                                            "direction": intent.direction or "all"})
    if "error" in c:
        return f"I could not set that constraint: {c['error']}"
    rk = tb.call("rank_interventions", {"top_n": 3})
    return (f"PROPOSED (not applied) constraint — {intent.constraint_kind.replace('_', ' ')} on {c['applies_to']}: "
            f"{c['meaning']}" + ("" if intent.direction else " No direction was given, so it covers every carriageway.")
            + " With this constraint: " + _ranking_sentence(rk) + " Apply it to update the scenario.")


def _why_flow(tb: ToolBox, intent: Intent, xid: str, method: str) -> str:
    info = tb.call("inspect_crossing", {"crossing_id": xid})
    users = sorted({z for c in info["carriageways"] for z in c["baseline_zone_routes_using"]})
    already = all(c["status"] == "closed" for c in info["carriageways"])
    if already:
        return (f"{info['crossing']} is already closed in the current scenario, so closing it again changes nothing. "
                f"In the baseline {len(users)} anchor route(s) used it" + (f" ({', '.join(users)})" if users else "")
                + "; see the recovery ranking for the value of reopening it.")
    ev = tb.call("evaluate_closure", {"crossing_id": xid, "direction": "all"})
    if not users and ev["added_km"] <= 0.0005 and not ev["zones_cut_off"]:
        return (f"No neighbourhood anchor's shortest directed route to its nearest assistance point (rule {info['rule']}) "
                f"crosses {info['crossing']} in the baseline, so closing it (on top of the current scenario) changes "
                f"this metric by {km(ev['added_km'])}. That does not make the bridge unimportant: other destinations, "
                "directions and capacity are not measured here.")
    return (f"{len(users)} anchor route(s) use {info['crossing']} in the baseline"
            + (f" ({', '.join(users)})" if users else "") + f". Closing it on top of the current scenario adds "
            f"{km(ev['added_km'])} across {ev['zones_affected']} zone(s)"
            + (f"; largest {ev['largest'][0]['zone']} (+{km(ev['largest'][0]['added_km'])})" if ev["largest"] else "")
            + (f"; CUT OFF: {', '.join(ev['zones_cut_off'])}" if ev["zones_cut_off"] else "") + ".")


def _section_flow(tb: ToolBox, intent: Intent, res: dict) -> str:
    p = tb.call("propose_closure", {"report_text": intent.text, "segment_ids": res["segment_ids"]})
    if "error" in p:
        return f"I could not propose the closure: {p['error']}"
    text = (f"I read the report as {res['detail']}. PROPOSED (not applied) closure: modeled impact "
            f"+{km(p['added_km'])} across {p['zones_affected']} zone(s)"
            + (f"; CUT OFF: {', '.join(p['zones_cut_off'])}" if p["zones_cut_off"] else "") + ".")
    if p["added_km"] > 0 or p["zones_cut_off"]:
        text += " If applied: " + _ranking_sentence(tb.call("rank_interventions", {"top_n": 3}))
    return text + " Apply the proposal to update the scenario."


def _ranking_sentence(rk: dict, full: bool = False) -> str:
    return briefing.ranking_sentence(rk, full)
