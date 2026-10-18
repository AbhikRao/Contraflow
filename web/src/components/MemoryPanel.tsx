import { useEffect, useState } from 'react'
import { api, type MemoryFinding, type MemoryReview, type Scenario } from '../api'
import { describeScenario } from '../scenarioText'

interface Props { scenario: Scenario; cwLabel: Map<string, string>; refreshKey: number; ruleLabel: (r: string) => string; onOpenCase?: (id: string) => void }

function status(f: MemoryFinding): { tone: 'ok' | 'warn' | 'old'; head: string; text: string } {
  const st = f.still_applies ?? ''
  if (st === 'yes') return { tone: 'ok', head: 'Still applies', text: `Re-evaluated now on your current scenario: same modeled result${f.now_added_km != null ? ` (+${f.now_added_km} km)` : ''}.` }
  if (st.startsWith('yes (same')) return { tone: 'ok', head: 'Same scenario', text: 'Computed for exactly the scenario on screen now.' }
  if (st === 'changed') return { tone: 'warn', head: 'Changed', text: `Re-evaluated now: +${f.now_added_km} km, was +${f.then_added_km} km. The earlier number no longer holds.` }
  if (st.startsWith('superseded')) return { tone: 'old', head: 'Superseded', text: 'The hypothesised closure is now part of the scenario, so this finding has been acted on.' }
  if (st.startsWith('stale')) return { tone: 'old', head: 'Stale — historical', text: 'Computed for a different scenario. Do not rely on it; re-rank before using.' }
  return { tone: 'old', head: 'Not re-checked', text: 'Historical record.' }
}

/** Persistent investigation memory, re-evaluated against the scenario on screen. */
export default function MemoryPanel({ scenario, cwLabel, refreshKey, ruleLabel, onOpenCase }: Props) {
  const [m, setM] = useState<MemoryReview | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    const ctrl = new AbortController()
    setErr(null)
    api.memoryReview(scenario, ctrl.signal).then(r => { if (!ctrl.signal.aborted) setM(r) })
      .catch(e => { if (!ctrl.signal.aborted) setErr(String(e)) })
    return () => ctrl.abort()
  }, [scenario, refreshKey])
  if (err) return <div className="panel scroll"><p className="err-inline">{err}</p></div>
  if (!m) return <div className="panel scroll empty-state"><p>Re-evaluating stored findings against the current scenario…</p></div>
  return (
    <div className="panel scroll memory" data-testid="memory-view">
      <h3>Graph memory <small>stored in FalkorDB · re-checked now</small></h3>
      <p className="hint">Re-checked against <b>{describeScenario(scenario, cwLabel, ruleLabel(scenario.rule))}</b>; nothing is shown as current until it has been.</p>
      <details className="method"><summary>Labels used here</summary>
        <div className="prov-legend">
          <span className="ptag report">Field report · unverified</span><span className="ptag graph">Graph-derived finding</span>
          <span className="ptag assume">Assumption</span><span className="ptag human">Your decision</span>
        </div>
        <p className="hint">Past investigations are kept as a graph of reports, places, findings and your decisions in FalkorDB, so they survive restarts.</p>
      </details>
      {m.investigations.length === 0 && <p className="empty-state">No investigations yet. Ask the agent about a reported or hypothetical closure.</p>}
      {m.investigations.map(inv => (
        <article key={inv.investigation_id} className="mem-card">
          <div className="mem-when">{inv.when.replace('T', ' ')}{onOpenCase && <button className="linklike" style={{ float: 'right' }} onClick={() => onOpenCase(inv.investigation_id)}>Case file</button>}</div>
          <div className="mem-block"><span className="ptag report">Field report · unverified</span>
            <q>{inv.report}</q></div>
          {inv.places.length > 0 && <div className="mem-block"><span className="mem-k">Graph locations</span>
            {inv.places.map(p => <span key={p} className="place">{p}</span>)}</div>}
          <div className="mem-block" data-testid="mem-origin"><span className="mem-k">Computed for</span>
            <span className="mem-scn">{describeScenario(inv.scenario_then, cwLabel, ruleLabel(inv.scenario_then.rule))}</span></div>
          {inv.findings.map((f, i) => {
            const s = status(f)
            return (
              <div key={i} className={`mem-finding ${s.tone}`}>
                <div className="mem-then"><span className="ptag graph">Graph-derived · then</span> {f.then}</div>
                {f.assumptions.length > 0 && <div className="mem-assume"><span className="ptag assume">Assumed</span> {f.assumptions.join('; ')}</div>}
                <div className="mem-now"><span className={`badge ${s.tone}`}>{s.head}</span> {s.text}</div>
              </div>
            )
          })}
          {inv.proposals.length > 0 && <div className="mem-block">
            {inv.proposals.map((p, i) => <div key={i} className={`mem-prop st-${p.state}`}>
              <span className="ptag human">Your decision</span> {p.label} — <b>{p.state === 'applied' ? 'applied by you' : p.state === 'rejected' ? 'rejected by you' : 'proposed, undecided'}</b></div>)}
          </div>}
        </article>
      ))}
      <p className="hint">{m.stats.investigations} investigation(s) · {m.stats.reports} report(s) · {m.stats.findings} finding(s) · {m.stats.decisions} decision(s) stored.</p>
    </div>
  )
}
