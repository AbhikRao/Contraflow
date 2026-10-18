import { useEffect, useState } from 'react'
import { signedKm, type AgentReply, type AgentStep, type Meta } from '../api'
import { TOOL_LABEL, resOf, stagesFrom, type Res, type Stage } from './agentModel'

export type Prov = 'report' | 'graph' | 'assume' | 'agent' | 'system' | 'model' | 'proposed' | 'applied' | 'rejected'
const PROV: Record<Prov, string> = {
  report: 'Field report · unverified', graph: 'Graph-derived · FalkorDB', assume: 'Assumption · analysis only',
  agent: 'Agent briefing · from graph tool results', system: 'System-generated text', model: 'Model-written · unverified',
  proposed: 'Proposed · awaiting your decision', applied: 'Applied by you', rejected: 'Rejected by you',
}
export const ProvTag = ({ kind, label }: { kind: Prov; label?: string }) =>
  <span className={`ptag ${kind}`} data-prov={kind}>{label ?? PROV[kind]}</span>

const GLYPH = { done: '✓', pending: '', attention: '!', na: '–' } as const
export function Pipeline({ stages, className = '' }: { stages: Stage[]; className?: string }) {
  return (
    <ol className={`pipeline ${className}`} data-testid="pipeline">
      {stages.map((s, i) => (
        <li key={s.key} className={`st-${s.state}`} data-stage={s.key} data-state={s.state}>
          <span className="pdot">{GLYPH[s.state] || i + 1}</span>
          <span className="plabel">{s.label}</span>
          {s.detail && <span className="pdetail" title={s.detail}>{s.detail}</span>}
        </li>
      ))}
    </ol>
  )
}

/** While the agent runs: only operations that have completed are listed; the wait itself is stated, not animated into fake steps. */
export function LiveProgress({ steps, llm, model }: { steps: AgentStep[]; llm: boolean; model?: string }) {
  const [t0] = useState(() => Date.now())
  const [now, setNow] = useState(Date.now())
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 500); return () => clearInterval(id) }, [])
  const secs = Math.floor((now - t0) / 1000)
  const done = steps.filter(s => s.kind === 'tool').length   // rejected calls are not completed operations
  return (
    <div className="a-busy" data-testid="agent-busy">
      <div className="busy-head"><span className="dots"><i /><i /><i /></span>
        <b>{secs}s</b>
        <span>{llm ? `Waiting on ${model ?? 'the local model'} and the graph — local models typically take 15–30 s.`
          : 'Running graph tools (rule-based planner, no language model)…'}</span></div>
      <Pipeline stages={stagesFrom(steps, { proposals: 0, suggested: 0, applied: 0, rejected: 0, final: false })} className="compact" />
      <div className="busy-note">{done === 0 ? 'No graph operation has completed yet.' : `${done} graph operation${done === 1 ? '' : 's'} completed so far — only completed ones are listed:`}</div>
      {steps.length > 0 && <ol className="live" data-testid="live-steps">
        {steps.map((s, k) => <li key={k} className={s.kind === 'error' ? 'terr' : ''}>
          {s.kind === 'thought' ? <span>{s.text}</span>
            : s.kind === 'error' ? <><b>✕ Rejected: {TOOL_LABEL[s.tool ?? ''] ?? s.tool}</b> <span>{s.result_summary}</span></>
            : <><b>✓ {TOOL_LABEL[s.tool ?? ''] ?? s.tool}</b> <span>{s.result_summary}</span><em> {s.ms} ms</em></>}</li>)}
      </ol>}
    </div>
  )
}

/** Compact, graph-derived evidence blocks for an investigation reply. */
export function Investigation({ steps, meta, setHighlight }: { steps: AgentStep[]; meta: Meta; setHighlight: (h: { eids: string[]; zones: string[]; labels?: { id: string; text: string }[] } | null) => void }) {
  const inv = resOf(steps, 'investigate_disruption').find(r => Array.isArray(r.affected))
  const cmp = resOf(steps, 'compare_options').find(r => Array.isArray(r.options))
  const rec = resOf(steps, 'recall_memory').find(r => Array.isArray(r.findings))
  const assumed = resOf(steps, 'assume_constraint').filter(r => r.assumed)
  if (!inv && !cmp && !rec) return null
  const cwEids = new Map(meta.crossings.flatMap(x => x.carriageways.map(c => [c.id, c.eids] as const)))
  const closes = inv ? [...(inv.closes?.closed_carriageways ?? []).flatMap((c: string) => cwEids.get(c) ?? []),
    ...(inv.closes?.closed_segments ?? [])] : []
  const valid: Res[] = cmp ? cmp.options.filter((o: Res) => !o.error) : []
  const winner = valid.find(o => o.action === cmp?.winner)
  const maxRec = Math.max(0.01, ...valid.map(o => o.recovered_km))
  return (
    <div className="inv">
      {assumed.length > 0 && (
        <div className="blk assume" data-testid="assumptions">
          <ProvTag kind="assume" />
          {assumed.map((a, i) => <span key={i} className="blk-text">{a.assumed}</span>)}
          <small>used for this analysis only; it is not applied and not proposed</small>
        </div>
      )}
      {inv && inv.affected.length > 0 && (
        <div className="blk graph affected" data-testid="affected-zones"
          onMouseEnter={() => setHighlight({ eids: closes, zones: inv.affected.map((a: Res) => a.id),
            labels: inv.affected.slice(0, 6).map((a: Res) => ({ id: a.id, text: a.status === 'cut_off' ? 'cut off' : signedKm(a.delta_m, 2) })) })}
          onMouseLeave={() => setHighlight(null)}>
          <ProvTag kind="graph" label="Graph-derived · hypothesis on a graph copy" />
          <b>Neighbourhoods affected</b> <small>hover to show on the map</small>
          <div className="zchips">
            {inv.affected.slice(0, 10).map((a: Res) => (
              <span key={a.id} className={a.status === 'cut_off' ? 'lost' : a.status === 'shorter' ? '' : 'worse'}
                title={a.status === 'cut_off' ? `used ${a.route_used.join(', ') || 'no river crossing'}`
                  : `was via ${a.route_used.join(', ') || 'no river crossing'}; now via ${a.reroutes_via.join(', ') || 'no river crossing'} to ${a.nearest_help_now}`}>
                {a.zone} {a.status === 'cut_off' ? 'cut off' : signedKm(a.delta_m, 2)}</span>))}
          </div>
          {inv.affected[0]?.reroutes_via && (
            <p className="route-note">e.g. <b>{inv.affected[0].zone}</b>: was {inv.affected[0].route_used.length ? `via ${inv.affected[0].route_used.join(', ')}` : 'without a river crossing'},
              now {inv.affected[0].reroutes_via.length ? `via ${inv.affected[0].reroutes_via.join(', ')}` : 'without crossing the river'} to {inv.affected[0].nearest_help_now}.</p>)}
          {(inv.conflicting_reports?.length ?? 0) > 0 && <p className="stale">Conflicting earlier report(s): {inv.conflicting_reports.map((c: Res) => `“${c.text}”`).join('; ')}</p>}
        </div>
      )}
      {cmp && valid.length >= 2 && (
        <div className="blk graph compare" data-testid="comparison">
          <ProvTag kind="graph" label="Graph-derived · alternatives on graph copies" />
          <b>Alternatives compared</b> <small>({cmp.evaluated_for})</small>
          {valid.map(o => (
            <div key={o.candidate_id} className={`opt ${o.action === cmp.winner ? 'win' : ''}`}>
              <span className="opt-name">{o.action}{o.action === cmp.winner ? ' ★' : ''}</span>
              <span className="opt-bar"><i style={{ width: `${Math.max(2, (o.recovered_km / maxRec) * 100)}%` }} /></span>
              <span className="opt-val">{o.recovered_km} km</span>
            </div>))}
          <p className="why">Why <b>{cmp.winner}</b> wins: {valid.some(o => o.zones_cut_off_by_it.length > 0) ? 'the other option cuts off neighbourhoods; ' : ''}it recovers {winner?.recovered_km} km vs{' '}
            {valid.filter(o => o.action !== cmp.winner).map(o => `${o.recovered_km} km`).join(' / ')} of the modeled added distance.
            {cmp.per_zone_differences.length > 0 && ` ${cmp.per_zone_differences.length} neighbourhood(s) get different routes:`}</p>
          {cmp.per_zone_differences.length > 0 && <ul className="zdiff">
            {cmp.per_zone_differences.slice(0, 3).map((d: Res) => <li key={d.zone}><b>{d.zone}</b>: {d.option_a_km} km
              {d.option_a_route_crossings.length ? ` via ${d.option_a_route_crossings.join(', ')}` : ''} vs {d.option_b_km} km
              {d.option_b_route_crossings.length ? ` via ${d.option_b_route_crossings.join(', ')}` : ''}</li>)}
          </ul>}
        </div>
      )}
      {rec && rec.findings.length > 0 && (
        <div className="blk recall" data-testid="recall">
          <ProvTag kind="graph" label="Recalled from memory · re-evaluated now" />
          <ul>{rec.findings.map((f: Res) => <li key={f.finding_id}><span className={`st ${String(f.still_applies ?? '').split(' ')[0]}`}>
            {f.still_applies ?? '—'}</span> {f.then}{f.now ? ` → ${f.now}` : ''} <small>({f.when})</small></li>)}</ul>
        </div>
      )}
    </div>
  )
}

export type { AgentReply }
