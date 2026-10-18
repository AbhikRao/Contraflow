"""Agent loop: LLM tool-calling (when a free local/compatible model is available) or the
deterministic planner, both over the same typed tools. Every reply is number-grounded."""

from __future__ import annotations

import json
import re
import time

from ..engine import Scenario, ValidationError
from . import briefing, deterministic, llm
from .intent import Intent, parse_intent
from ..evidence import resolve_location
from .tools import SPECS, ToolBox, ToolError, specs_for

MAX_STEPS = 8

SYSTEM = """You are the planning agent inside Contraflow, a tabletop road-access sandbox for Chennai's Adyar corridor.
The road graph lives in FalkorDB; your tools query it and evaluate scenarios on isolated graph copies.

Rules:
- Use the provided tools through tool calls. Never write a tool call as text.
- Every number you write must come from a tool result in this conversation. Never estimate or invent numbers.
- Field report that a bridge/road is blocked, flooded or closed: call assess_field_report with the report text,
  then call rank_interventions.
- Commander constraint (keep/reserve a crossing for emergency vehicles, do not reverse, do not reopen): call
  set_planning_constraint with the user's wording as location, then call rank_interventions.
- "What should we reopen / recover first?": call rank_interventions.
- "Why does a crossing (not) matter?": call explain_crossing.
- If a tool returns needs_confirmation, status "ambiguous" or "not_found": do not propose anything; say why and
  ask the user to choose between the candidates, then stop.
- Proposals are reviewed by the user before anything changes, so never ask permission to propose.
  Only ask a question when the location is ambiguous or not found.
- Use direction "all" unless the user explicitly says northbound or southbound.
- You only propose changes; the user applies them. Never say a change has been applied.
- Final answer: what was resolved and why, the modeled impact, and the best recovery option.
- Investigation ("may be flooded", "what if", "find out who is affected"): call investigate_disruption with the
  place, assume_constraint if the user asked to keep a crossing, rank_recovery, then compare_options on the top two
  candidate_ids. Optionally trace_route for the most affected neighbourhood. Investigations change nothing.
- Follow-up about earlier findings: call recall_memory.
- Never mention travel time or speed. Distances are modeled shortest directed road distances to the nearest
  assistance point — not travel time,
  congestion, capacity, people served or safety. Do not give operational emergency advice.
- Reply in at most 5 short sentences. Use km values as given by tools."""


def _ctx(tb: ToolBox) -> str:
    sc = tb.work
    closed = [tb.net.carriageway(c)["crossing_name"] + " " + tb.net.carriageway(c)["direction"] for c in sc.closed_carriageways]
    return (f"Current scenario: rule {sc.rule}; closed: {', '.join(closed) or 'none'}; other closed segments: "
            f"{len(sc.closed_segments)}; constraints: {', '.join(k['kind'] + ' on ' + k['target'] for k in sc.constraints) or 'none'}. "
            "Crossing ids: " + ", ".join(f"{x['id']} ({x['name']})" for x in tb.net.crossings) + ".")


NUM = re.compile(r"(?<![\w.])[-+−]?\d[\d,]*(?:\.\d+)?")


def grounding(text: str, tb: ToolBox, user_msg: str) -> dict:
    allowed = set()
    for v in tb.numbers:
        for f in (v, v / 1000, v * 100):
            allowed.update({f"{f:.0f}", f"{f:.1f}", f"{f:.2f}", f"{round(f)}"})
    allowed.update(NUM.findall(user_msg))
    for x in tb.net.crossings:        # ids/names may contain digits
        allowed.update(NUM.findall(x["name"]))
    for r in (tb.proposals or []):
        allowed.update(NUM.findall(r.get("report_id") or ""))
    found = [n.replace(",", "").lstrip("+−-") for n in NUM.findall(text)]
    bad = [n for n in found if n not in allowed and n.rstrip("0").rstrip(".") not in allowed]
    return {"numbers": len(found), "grounded": len(found) - len(bad), "ungrounded": bad,
            "method": "numeric check only: each number appears in this turn's tool outputs. Not semantic verification "
                      "of units, subjects or meaning."}


APPLIED_CLAIM = re.compile(r"\b(is now|are now|has been (closed|reopened|reserved|applied|set)|have been (closed|reopened|"
                           r"applied)|now reserved|now closed|now reopened|i have (closed|reopened|applied|reserved))\b", re.I)
TOTAL_BUDGET_S = 90.0   # bounds the model loop only; deterministic follow-ups (typically 1-3 s) run afterwards


def _pending(intent, clar: dict, scenario: Scenario) -> dict:
    return {"intent": intent.to_dict(), "candidates": clar.get("candidates") or
            [o["value"]["crossing_id"] for o in clar.get("options", [])], "scenario_key": scenario.key()}


def _reply(tb, mode, text, clar, t0, note=None, model_text=None, extra=None, source="tools"):
    if note:
        tb.steps.insert(0, {"kind": "thought", "text": note})
    model_note = None
    if model_text:
        model_note = {"text": model_text, "grounding": grounding(model_text, tb, tb.user_msg),
                      "claims_applied": bool(APPLIED_CLAIM.search(model_text)),
                      "label": "Model's own wording — unverified; not used as the briefing"}
    if tb.inv_id and tb.mem:
        try:
            led = tb.ledger.summary()
            tb.mem.attach_trace(tb.inv_id, {
                "mode": mode, "briefing_source": source, "briefing": text,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "steps": [{"kind": s.get("kind"), "tool": s.get("tool"), "args": s.get("args"),
                           "summary": s.get("result_summary") or s.get("text"), "ms": s.get("ms"), "queries": s.get("queries")}
                          for s in tb.steps],
                "graph_operations": {op: v["count"] for op, v in led["by_op"].items()},
                "graph_operation_count": led["queries"],
                "proposals": [{"label": p["label"], "kind": p["kind"]} for p in tb.proposals],
                "clarification": (clar or {}).get("question")})
        except Exception:  # noqa: BLE001 — provenance capture must never break a reply
            pass
    return {"mode": mode, "message": text, "model_note": model_note, "steps": tb.steps, "proposals": tb.proposals,
            "clarification": clar, "grounding": grounding(text, tb, tb.user_msg), "intent": tb.intent.to_dict(),
            "briefing_source": source, "suggested_actions": suggested_actions(tb),
            "investigation_id": tb.inv_id, "hypothesis": tb.hyp_delta["label"] if tb.hypothesis else [],
            "base_key": tb.base_key, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "working_scenario": tb.work.to_dict(), "focus": tb.focus, "ledger": tb.ledger.summary(), **(extra or {})}


def suggested_actions(tb: ToolBox) -> list[dict]:
    """Investigations never change the scenario. They offer explicit follow-up commands; each one goes through the
    normal intent parsing, authorization, proposal and human Apply steps."""
    if tb.intent.action != "investigate":
        return []
    out = []
    for st in tb.steps:
        r = st.get("result") or {}
        if st.get("kind") != "tool":
            continue
        # built from the typed scopes the tools evaluated, one instruction per location, so each follow-up carries
        # exactly the location and direction that were analysed (never a direction from another clause)
        if st["tool"] == "investigate_disruption" and r.get("hypothesis") and r.get("resolved"):
            for sc in r.get("scopes") or [{"name": r["resolved"], "direction": None}]:
                where = sc["name"] + (f" {sc['direction']}" if sc.get("direction") else "")
                out.append({"label": f"Propose closing {where}", "message": f"Close {where}."})
        if st["tool"] == "assume_constraint" and r.get("assumed") and r.get("scope"):
            sc = r["scope"]
            where = sc["name"] + (f" {sc['direction']}" if sc.get("direction") else "")
            verb = {"reserve_emergency": "Reserve {} for emergency vehicles.", "keep_direction": "Do not reverse {}.",
                    "no_reopen": "Do not reopen {}."}[tb.intent.aux_constraint]
            out.append({"label": f"Propose: {r['assumed']}", "message": verb.format(where)})
    return out


def _record_negative_report(tb: ToolBox, intent: Intent) -> None:
    """'X is not blocked' is evidence too: it is stored as an unverified report (so later contradictory reports can
    be flagged), never as a scenario change. Location resolution is read-only."""
    if intent.polarity != "negative" or intent.mood == "imperative" or not tb.mem:
        return
    r = resolve_location(tb.db, tb.net, intent.text, tb.ledger)
    if r["status"] == "resolved" and r["resolved"]["kind"] == "crossing":
        x = r["resolved"]
        tb._report(intent.text, [{"kind": "crossing", "id": x["id"], "name": x["name"], "method": x.get("method", "")}])


def run_agent(state: dict, message: str, scenario: Scenario, history: list[dict], mode: str | None,
              confirm: dict | None, on_step=None) -> dict:
    t0 = time.perf_counter()
    state["engine"].validate(scenario)
    focus = next((h.get("focus") for h in reversed(history or []) if h.get("focus")), None)
    det_mode = {"kind": "deterministic", "label": "Deterministic planner · rule-based, no LLM"}

    # ---- confirmation of an earlier clarification: resume the ORIGINAL intent, never a default action
    if confirm:
        pending = confirm.get("pending")
        if not pending:
            raise ValidationError("confirmation without a pending clarification", "invalid_confirmation")
        intent = Intent.from_dict(pending["intent"])
        choice = confirm.get("crossing_id")
        if choice not in pending.get("candidates", []):
            raise ValidationError(f"{choice!r} was not one of the offered choices", "invalid_confirmation")
        tb = ToolBox(state, scenario, {"crossing_id": choice, "direction": confirm.get("direction")},
                     user_msg=intent.text, intent=intent, on_step=on_step)
        if pending.get("scenario_key") != scenario.key():
            return _reply(tb, det_mode, "The scenario has changed since I asked, so I have not acted on this "
                          "confirmation. Please restate the request for the current scenario.", None, t0,
                          extra={"stale": True}, source="deterministic")
        text, clar = deterministic.plan(intent, tb, focus, confirmed=choice)
        return _reply(tb, {"kind": "deterministic", "label": "Confirmation · deterministic orchestration (no LLM)"},
                      text, clar, t0, source="deterministic")

    intent = parse_intent(message)
    tb = ToolBox(state, scenario, None, user_msg=message, intent=intent, on_step=on_step)
    _record_negative_report(tb, intent)
    status = llm.llm_status()
    want_llm = mode == "llm" or (mode in (None, "auto") and status["llm_available"])
    note = None
    if want_llm and status["llm_available"]:
        try:
            model_text, clar = _llm_loop(tb, message, history, focus, intent)
            text, _ = briefing.compose(tb.steps, tb.work.key())
            source = "tools"
            if intent.polarity == "negative":
                neg = (f"Your message is a negative {'instruction' if intent.mood == 'imperative' else 'report'} about "
                       f"'{intent.operation.replace('_', ' ')}', so I have not proposed any such change.")
                if not text:
                    source = "deterministic"          # the reply is the runtime's own template, not a tool briefing
                text = f"{neg} {text}".strip()
            if not text:
                # No graph tool produced a briefable result: show the model's words, explicitly marked as model-written.
                text, model_text, source = model_text, None, "model"
            if clar:
                clar["pending"] = _pending(intent, clar, scenario)
            return _reply(tb, {"kind": "llm", "label": f"Live LLM · {status['model']} (local, free)",
                               "model": status["model"]}, text, clar, t0, model_text=model_text, source=source)
        except Exception as e:  # noqa: BLE001 — any LLM failure falls back, visibly
            note = f"LLM call failed ({type(e).__name__}: {str(e)[:120]}); used the deterministic planner instead."
            tb = ToolBox(state, scenario, None, user_msg=message, intent=intent, on_step=on_step)
    elif want_llm and not status["llm_available"]:
        note = f"LLM unavailable ({status['reason']}); used the deterministic planner."
    text, clar = deterministic.plan(intent, tb, focus)
    if clar:
        clar["pending"] = _pending(intent, clar, scenario)
    return _reply(tb, det_mode, text, clar, t0, note=note, source="deterministic")


TEXT_CALL = re.compile(r"^\s*`*(\w+)\((.*)\)`*\s*$", re.S)


def _textual_call(content: str) -> list[dict]:
    """Small local models sometimes print `tool(arg="x")` instead of emitting a tool call."""
    m = TEXT_CALL.match(content.strip())
    if not m or m.group(1) not in {s["name"] for s in SPECS}:
        return []
    args = {k: (v.strip('"\'') if not v.isdigit() else int(v))
            for k, v in re.findall(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|[\w.-]+)", m.group(2))}
    return [{"id": "textual", "function": {"name": m.group(1), "arguments": json.dumps(args)}}]


def _llm_loop(tb: ToolBox, message: str, history: list[dict], focus: str | None, intent) -> tuple[str, dict | None]:
    """The model chooses tools. Required state transitions are completed deterministically:
    a mutating request that ends without a proposal (and without ambiguity) is completed by the deterministic
    planner, and every proposal is followed by a re-ranking. Total wall time is bounded."""
    deadline = time.perf_counter() + TOTAL_BUDGET_S
    msgs = [{"role": "system", "content": SYSTEM}]
    for h in (history or [])[-6:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": str(h["content"])[:600]})
    ctx = _ctx(tb) + (f" Crossing under discussion: {focus}." if focus else "")
    msgs.append({"role": "user", "content": f"{ctx}\nParsed user intent: {intent.polarity} {intent.mood} "
                 f"'{intent.operation}'" + (f", direction {intent.direction} only" if intent.direction else "")
                 + (" (negative: do NOT propose this change)" if intent.polarity == "negative" else "")
                 + f"\n\nUser: {message}"})
    tools = specs_for(intent)
    allowed = {t["name"] for t in tools}
    nudged, text = False, ""
    for _ in range(MAX_STEPS):
        remaining = deadline - time.perf_counter()
        if remaining < 3:
            tb.steps.append({"kind": "thought", "text": "Time budget reached; stopped the model loop."})
            break
        out = llm.chat(msgs, tools, timeout=min(45.0, remaining))
        calls = out.get("tool_calls") or []
        if not calls:
            calls = _textual_call(out.get("content") or "")
            if calls:
                tb.steps.append({"kind": "thought", "text": "Model wrote a tool call as text; executed it as a structured call."})
        if not calls:
            text = (out.get("content") or "").strip()
            blocked = _blocked(tb)
            if not nudged and intent.action == "investigate" and not blocked and not _did(tb, "investigate_disruption"):
                nudged = True
                msgs.append({"role": "assistant", "content": text or "(no answer)"})
                msgs.append({"role": "user", "content": "Continue: call investigate_disruption for the reported place, "
                             "then rank_recovery. Nothing is applied; this is analysis only."})
                tb.steps.append({"kind": "thought", "text": "Runtime nudge: investigation incomplete; asked the model to continue."})
                continue
            if not nudged and intent.mutating and not tb.proposals and not blocked:
                nudged = True
                msgs.append({"role": "assistant", "content": text or "(no answer)"})
                msgs.append({"role": "user", "content": "Continue with the tools now: propose the requested change, "
                             "then rank interventions. The user reviews proposals before anything is applied."})
                tb.steps.append({"kind": "thought", "text": "Runtime nudge: workflow incomplete; asked the model to continue."})
                continue
            break
        msgs.append({"role": "assistant", "content": out.get("content") or "", "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if fn.get("name") in allowed:
                res = tb.call(fn.get("name", ""), args)
            else:   # refused before execution; still shown in the trace so the attempt is visible
                res = {"error": f"tool {fn.get('name')!r} is not available for this request", "code": "tool_not_offered"}
                step = {"kind": "error", "tool": fn.get("name", ""), "args": args, "result": res,
                        "result_summary": res["error"], "ms": 0, "queries": 0}
                tb.steps.append(step)
                if callable(tb.on_step):
                    tb.on_step(step)
            msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "name": fn.get("name", ""),
                         "content": json.dumps(res, default=str)[:3500]})
    clar = _clarification(tb)
    if intent.action in ("investigate", "recall") and not clar and not _blocked(tb):
        complete_investigation(tb, intent, focus)
        clar = _clarification(tb)
    if intent.action == "vulnerability" and not _did(tb, "find_critical_roads"):
        tb.steps.append({"kind": "thought", "text": "Runtime follow-up (vulnerability analysis not run by the model) — "
                                                    "deterministic orchestration"})
        tb.call("find_critical_roads", {"top_links": 20})
    # ---- deterministic completion of required state transitions
    if intent.mutating and not tb.proposals and not clar and not _blocked(tb):
        tb.steps.append({"kind": "thought", "text": "Model did not complete the requested workflow; completed by "
                         "deterministic orchestration."})
        _, clar = deterministic.plan(intent, tb, focus)
    if tb.proposals or (intent.also_rank and intent.action != "investigate"):
        key = tb.work.key()
        if not any(s.get("tool") == "rank_interventions" and (s.get("result") or {}).get("scenario_key") == key
                   for s in tb.steps):
            tb.steps.append({"kind": "thought", "text": "Runtime follow-up: re-ranked interventions for the proposed "
                             "scenario (deterministic orchestration)."})
            tb.call("rank_interventions", {"top_n": 3})
    if not text:
        text = "(the model gave no final text)"
    return text, clar


def _did(tb: ToolBox, tool: str, **match) -> bool:
    return any(s.get("tool") == tool and s.get("kind") == "tool" and
               all((s.get("result") or {}).get(k) == v for k, v in match.items()) for s in tb.steps)


def complete_investigation(tb: ToolBox, intent, focus) -> None:
    """Deterministic completion of the investigation's required steps, each labelled in the trace."""
    def follow(tool, args, why):
        tb.steps.append({"kind": "thought", "text": f"Runtime follow-up ({why}): {tool} — deterministic orchestration."})
        return tb.call(tool, args)
    if intent.action == "recall":
        if not _did(tb, "recall_memory"):
            follow("recall_memory", {}, "the request refers to earlier findings")
        return
    if not _did(tb, "investigate_disruption"):
        r = follow("investigate_disruption", {"location": intent.primary_text or intent.text}, "disruption not yet evaluated")
        if r.get("needs_confirmation") or r.get("status") in ("ambiguous", "not_found"):
            return
    if intent.aux_constraint and not _did(tb, "assume_constraint"):
        follow("assume_constraint", {"kind": intent.aux_constraint, "location": intent.aux_text or ""},
               "the user asked to keep a constraint during the analysis")
    hk = tb._hyp().key()
    if not any(s.get("tool") == "rank_recovery" and (s.get("result") or {}).get("scenario_key") == hk for s in tb.steps):
        follow("rank_recovery", {"top_n": 4}, "recovery not yet ranked for the final hypothesis")
    rk = tb.last_ranking or {}
    good = [c["candidate_id"] for c in rk.get("ranking", []) if c["effect"] in ("restores_access", "shorter")]
    if len(good) >= 2 and not _did(tb, "compare_options"):
        follow("compare_options", {"candidate_ids": good[:2]}, "explain why the best option wins")


def _blocked(tb: ToolBox) -> bool:
    """True when the workflow legitimately stopped: ambiguous / not found location or a rejected action."""
    for s in tb.steps:
        r = s.get("result") or {}
        if r.get("needs_confirmation") or r.get("status") in ("ambiguous", "not_found") or \
                r.get("code") in ("ambiguous_location", "constraint_violation", "nothing_to_reopen"):
            return True
    return False


def _clarification(tb: ToolBox) -> dict | None:
    if tb.proposals:
        return None
    if any((s.get("result") or {}).get("code") == "ambiguous_location" for s in tb.steps):
        tb._authorize_from_user()
        r = tb.user_resolution or {}
        cands = [c for c in r.get("candidates", []) if c.get("kind") == "crossing"]
        if cands:
            return {"question": f"{r.get('reason', 'Which location?')} Which crossing do you mean?",
                    "options": [{"label": c["name"] + (f" ({c['distance_m']} m)" if c.get("distance_m") else ""),
                                 "value": {"crossing_id": c["id"]}} for c in cands],
                    "candidates": [c["id"] for c in cands]}
    for s in reversed(tb.steps):
        r = s.get("result") or {}
        cands = [c for c in r.get("candidates", []) if c.get("kind") == "crossing"]
        if (r.get("status") == "ambiguous" or r.get("needs_confirmation")) and cands:
            return {"question": f"{r.get('reason', 'Which location?')} Which crossing do you mean?",
                    "options": [{"label": c["name"] + (f" ({c['distance_m']} m)" if c.get("distance_m") else ""),
                                 "value": {"crossing_id": c["id"]}} for c in cands],
                    "candidates": [c["id"] for c in cands]}
    return None
