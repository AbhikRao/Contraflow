import { useEffect, useState } from 'react'
import { api, type CaseFile, type Robustness, type Scenario } from '../api'

/** Evidence-backed case file, laid out as an analytical document:
 *  report → scenario & assumptions → measured impact → recommendation (+ assumption test) → human decision → evidence.
 *  Every field comes from the memory graph or a fresh re-check, none from a model. */
export default function CaseFileView({ id, scenario, onClose }: { id: string; scenario: Scenario; onClose: () => void }) {
  const [c, setC] = useState<CaseFile | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [showCypher, setShowCypher] = useState(false)
  const [rob, setRob] = useState<Robustness | null>(null)
  const [robBusy, setRobBusy] = useState(false)
  const [robErr, setRobErr] = useState<string | null>(null)
  const [secs, setSecs] = useState(0)
  useEffect(() => { api.caseFile(id, scenario).then(setC).catch(e => setErr(String(e))) }, [id, scenario])
  useEffect(() => { setRob(null); setRobErr(null) }, [id, scenario])   // a result is only shown for the scenario it was computed on
  useEffect(() => {
    if (!robBusy) return
    const t0 = Date.now(); setSecs(0)
    const t = setInterval(() => setSecs(Math.floor((Date.now() - t0) / 1000)), 500)
    return () => clearInterval(t)
  }, [robBusy])
  const testAssumptions = () => {
    setRobBusy(true); setRobErr(null)
    api.caseRobustness(id, scenario).then(setRob).catch(e => setRobErr(String(e))).finally(() => setRobBusy(false))
  }
  const download = () => {
    if (!c) return
    const url = URL.createObjectURL(new Blob([c.markdown], { type: 'text/markdown' }))
    const a = document.createElement('a'); a.href = url; a.download = `contraflow-case-${c.investigation_id}.md`; a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  const tone = (s?: string) => !s ? 'old' : s === 'yes' || s.startsWith('yes') ? 'ok' : s === 'changed' ? 'warn' : 'old'
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal case" role="dialog" aria-label="Case file" data-testid="case-file" onClick={e => e.stopPropagation()}>
        <div className="case-head">
          <div><div className="kicker">Case file · from FalkorDB memory</div><h2>{id}</h2></div>
          <div className="case-actions">
            <button className="ghost" onClick={download} disabled={!c}>Download .md</button>
            <button className="ghost" onClick={onClose} aria-label="Close">Close</button>
          </div>
        </div>
        {err && <p className="err-inline" style={{ padding: '0 28px' }}>{err}</p>}
        {!c && !err && <p className="hint" style={{ padding: '0 28px' }}>Traversing the memory graph and re-checking findings…</p>}
        {c && <div className="case-body scroll">
          <section className="case-section" style={{ marginTop: 14 }}>
            <div className="blk report"><span className="ptag report">Report · {c.report.status}</span>
              <h2 className="case-title">{c.report.text}</h2>
              <p className="case-lede">{c.recorded_at.replace('T', ' ')} · {c.agent.answered_by ?? c.mode} · a hypothesis evaluated on a graph copy; nothing was applied by this investigation.</p></div>
            <div className="case-grid" style={{ marginTop: 14 }}>
              <div className="blk"><span className="mem-k">Resolved on the graph</span>
                {c.resolved_entities.length ? c.resolved_entities.map(e => <div key={e.name}><span className="place">{e.name}</span> <small>{e.kind} · {e.resolution} · {e.method}</small></div>)
                  : <small>no place resolved</small>}</div>
              <div className="blk"><span className="mem-k">Scenario at the time</span><div>{c.scenario_then.description}</div>
                <small className="mono">scenario {c.scenario_then.scenario_key} · road graph {c.scenario_then.road_graph_fingerprint}</small></div>
            </div>
          </section>

          {(c.commander_constraints.length > 0 || c.assumptions.length > 0) && <section className="case-section">
            <h3>Constraints &amp; assumptions <small>analysis only</small></h3>
            <div className="blk assume">
              {c.commander_constraints.map((k, i) => <div key={i} className="blk-text"><b>{k.kind.replace('_', ' ')}</b> — “{k.clause}” ({k.direction})</div>)}
              <ul>{c.assumptions.map(a => <li key={a}>{a}</li>)}</ul></div>
          </section>}

          {c.impact && <section className="case-section">
            <h3>Measured impact <small>graph copy</small></h3>
            <div className="case-big">+{c.impact.added_km} km <small>across {c.impact.zones_affected} neighbourhood(s); cut off: {c.impact.zones_cut_off?.length ? c.impact.zones_cut_off.join(', ') : 'none'}</small></div>
            <div className="zchips" style={{ margin: 0 }}>{c.impact.affected.map(a => <span key={a.zone} className={a.status === 'cut_off' ? 'lost' : 'worse'}>{a.zone} {a.delta_m == null ? 'cut off' : `+${(a.delta_m / 1000).toFixed(2)} km`}</span>)}</div>
          </section>}

          {c.alternatives.length > 0 && <section className="case-section">
            <h3>Recommendation <small>each alternative evaluated on its own graph copy</small></h3>
            <div className="case-rec"><b>{c.recommendation}</b>
              <small>{c.recommendation_basis}</small></div>
            <div style={{ marginTop: 10 }}>
              {c.alternatives.map(a => <div key={a.action} className={`opt ${a.action === c.recommendation ? 'win' : ''}`}>
                <span className="opt-name">{a.action}{a.action === c.recommendation ? ' — selected' : ''}</span>
                <span className="opt-val">{a.recovered_km} km</span></div>)}
              {c.comparison && <small className="hint">{c.comparison}</small>}
            </div>
            {!rob && !robBusy && <button className="rob-btn" onClick={testAssumptions} data-testid="test-assumptions">Test assumptions</button>}
            {robBusy && <div className="rob-wait" data-testid="rob-loading"><span className="dots"><i /><i /><i /></span><b>{secs}s</b>
              <span>Re-ranking on FalkorDB graph copies — typically 3–9 s.</span></div>}
            {robErr && <p className="err-inline">{robErr}</p>}
            {rob && <RobustnessView r={rob} onRerun={testAssumptions} busy={robBusy} />}
          </section>}

          <section className="case-section">
            <div className="case-grid">
              <div className="blk"><h3>Human decision</h3><ul>{c.human_decision.map(d => <li key={d}>{d}</li>)}</ul></div>
              <div className="blk"><h3>Status now</h3><div>Re-evaluated against: {c.current_status.evaluated_against}</div>
                {c.current_status.rechecks.map((r, i) => <div key={i} className="mem-now"><span className={`badge ${tone(r.still_applies)}`}>{r.still_applies ?? 'n/a'}</span> {r.finding.replace('_', ' ')}{r.now ? ` — ${r.now}` : ''}</div>)}</div>
            </div>
          </section>

          <section className="case-section case-evidence">
            <h3>Evidence</h3>
            {c.agent_steps.length > 0 && <details open><summary>What the agent executed ({c.agent.briefing_source ?? 'n/a'} briefing{c.agent.elapsed_ms ? `, ${(c.agent.elapsed_ms / 1000).toFixed(1)} s` : ''})</summary>
              <ol className="case-steps">{c.agent_steps.map((s, i) => <li key={i} className={s.kind === 'error' ? 'terr' : ''}>
                <code>{s.tool}</code>{s.kind === 'error' ? ' (rejected)' : ''} — {s.summary}{s.ms != null && <em> {s.ms} ms · {s.queries ?? 0} ops</em>}</li>)}</ol>
              <small>{c.graph_operation_count} FalkorDB operations: {Object.entries(c.graph_operations).map(([k, v]) => `${k} ×${v}`).join(', ')}</small></details>}
            <div style={{ marginTop: 10 }}><button className="linklike" onClick={() => setShowCypher(v => !v)}>{showCypher ? 'Hide' : 'Show'} the Cypher traversal that assembled this file</button></div>
            {showCypher && <pre className="case-cypher" data-testid="case-cypher">{c.assembled_by.cypher}</pre>}
          </section>
          <p className="case-foot">Graph <code>{c.assembled_by.graph}</code> · modeled shortest road distances on the OpenStreetMap graph; a tabletop planning sandbox, not a
            prediction of flooding, travel time, evacuation or emergency safety.</p>
        </div>}
      </div>
    </div>
  )
}

const robTone = (s?: string) => !s ? 'old' : s === 'remains best' || s.startsWith('same crossing') ? 'ok' : s === 'no repair needed' ? 'old' : 'warn'
const robLabel = (s?: string) => !s ? 'not evaluated' : s === 'no repair needed' ? 'no repair needed' : s === 'remains best' ? 'still best'
  : s === 'changed: no repair helps' ? 'no repair helps' : s.startsWith('changed') ? 'winner changes' : s === 'original excluded' ? 'excluded' : s === 'original not applicable' ? 'not applicable'
    : s.startsWith('same crossing') ? 'same crossing' : s

/** Deterministic re-ranking under single-assumption variations; every row is a fresh FalkorDB evaluation.
 *  The count is "k of n variations tested", never a probability or a safety statement. */
function RobustnessView({ r, onRerun, busy }: { r: Robustness; onRerun: () => void; busy: boolean }) {
  if (!r.available) return <div className="rob" data-testid="robustness"><p className="rob-caveat">{r.reason}</p></div>
  const vs = r.variations ?? []
  const held = vs.filter(v => v.original_still_best).length
  const tested = vs.filter(v => !v.rejected).length
  return (
    <div className="rob" data-testid="robustness">
      <div className="rob-head">
        <b data-testid="rob-summary">Recommendation unchanged in {held} of {tested} variations tested</b>
        <small>{r.original ? <>Testing <b>{r.original.label}</b> ({r.original.recovered_km} km) on: {r.tested_on}</> : r.tested_on}
          {r.stored_recommendation && !r.stored_matches_fresh && <span className="rob-stale" data-testid="rob-stale"> · the stored recommendation was “{r.stored_recommendation}”; this is a fresh ranking</span>}</small>
      </div>
      <ul className="rob-list">
        {vs.map(v => (
          <li key={v.id}><details className={`rob-item ${robTone(v.status)}`} data-testid={`rob-${v.kind}`}>
            <summary><span className="rob-dot" />
              <span className="rob-what">{v.changed}
                <small>{v.rejected ? `not evaluated: ${v.rejected.message}` : v.best ? `Best: ${v.best.label} · ${v.best.recovered_km} km`
                  : 'No evaluated repair improves access'}{v.current ? ` · disruption +${(v.current.total_delta_m / 1000).toFixed(2)} km` : ''}</small></span>
              <span className="rob-res">{robLabel(v.status)}</span></summary>
            <div className="rob-more">{v.reason ?? v.rejected?.message}
              {v.zones_deciding?.length ? <small>Deciding neighbourhoods (new winner vs original, km saved): {v.zones_deciding.map(z => `${z.name ?? z.zone} ${(z.new_saves_m / 1000).toFixed(2)} vs ${(z.original_saves_m / 1000).toFixed(2)}`).join('; ')}.</small> : null}</div>
          </details></li>
        ))}
      </ul>
      <p className="rob-caveat">Only these {vs.length} model variations were tested, each changing one assumption and re-ranked from scratch on its own graph copies. {r.method?.includes('not a probability') ? 'It is a deterministic comparison, not a probability, a confidence level or a safety conclusion.' : ''}
        {r.graph_operations ? ` ${r.graph_operations.count} FalkorDB operations (${r.graph_operations.by_op.graph_copy ?? 0} graph copies), ${((r.elapsed_ms ?? 0) / 1000).toFixed(1)} s.` : ''}
        {' '}<button className="linklike" onClick={onRerun} disabled={busy}>Re-run</button></p>
    </div>
  )
}
