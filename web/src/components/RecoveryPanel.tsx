import type { Candidate, Interventions, Scenario } from '../api'
import { signedKm } from '../api'

interface Props {
  ivs: Interventions | null; busy: boolean; zoneName: Map<string, string>; cwLabel: Map<string, string>
  setHovered: (c: Candidate | null) => void; apply: (c: Candidate) => void; scenario: Scenario
}

const TYPE: Record<Candidate['type'], string> = {
  reopen_bridge: 'Reopen bridge', reopen_carriageway: 'Reopen direction', reopen_segment: 'Reopen road', contraflow: 'Contraflow',
}

export default function RecoveryPanel({ ivs, busy, zoneName, setHovered, apply }: Props) {
  if (!ivs) return (
    <div className="panel scroll empty-state">
      {busy ? <p>Evaluating interventions on isolated FalkorDB graph copies…</p> : <>
        <p><b>No disruption in the current scenario.</b></p>
        <p>Close a crossing or road (or choose a preset) and Contraflow will evaluate every eligible reopening and
          contraflow candidate on its own copy of the road graph, then rank them.</p></>}
    </div>
  )
  const good = (c: Candidate) => c.effect === 'restores_access' || c.effect === 'shorter'
  const useful = ivs.ranking.filter(good)
  const harmful = ivs.ranking.filter(c => c.effect === 'loses_access' || c.effect === 'longer' || c.effect === 'mixed')
  const zero = ivs.ranking.filter(c => c.effect === 'no_change')
  const top = useful[0]
  const card = (c: Candidate, i: number) => (
    <div key={c.id} className={`cand ${good(c) && i === 0 ? 'best' : ''} ${c.effect === 'loses_access' ? 'harm' : ''}`} onMouseEnter={() => setHovered(c)} onMouseLeave={() => setHovered(null)}>
      <div className="cand-head">
        <span className="rank">{i + 1}</span>
        <div className="cand-title"><span className={`type t-${c.type}`}>{TYPE[c.type]}</span><b>{c.label}</b></div>
        <button className={c.effect === 'loses_access' ? 'danger-btn' : ''} onClick={() => {
          if (c.effect === 'loses_access' && !window.confirm(`This cuts off ${c.zones_lost.length} zone(s). Apply anyway?`)) return
          apply(c); setHovered(null)
        }}>{c.effect === 'loses_access' ? 'Apply anyway' : 'Apply'}</button>
      </div>
      <div className="cand-metrics">
        {c.zones_lost.length > 0 && <span className="lost">CUTS OFF: {c.zones_lost.map(z => zoneName.get(z)).join(', ')}</span>}
        <span className={c.recovered_m < -0.5 ? 'rec neg' : 'rec'}>{c.recovered_m > 0.5 ? `recovers ${(c.recovered_m / 1000).toFixed(2)} km`
          : c.recovered_m < -0.5 ? `adds ${(-c.recovered_m / 1000).toFixed(2)} km` : c.effect === 'no_change' ? 'no change' : 'net 0 km'}</span>
        {c.share_of_disruption != null && c.recovered_m > 0.5 &&
          <span className="share"><i style={{ width: `${Math.min(100, c.share_of_disruption * 100)}%` }} />{Math.round(c.share_of_disruption * 100)}% of current disruption</span>}
        {c.zones_restored.length > 0 && <span className="restored">restores access: {c.zones_restored.map(z => zoneName.get(z)).join(', ')}</span>}
      </div>
      {c.zones_improved.length > 0 && <div className="zchips">
        {c.zones_improved.slice(0, 6).map(z => <span key={z.zone}>{zoneName.get(z.zone)} {signedKm(-z.saved_m, 2)}</span>)}
        {c.zones_worsened.map(z => <span key={z.zone} className="worse">{zoneName.get(z.zone)} {signedKm(z.added_m, 2)}</span>)}
      </div>}
      <p className="whytext">{c.why}</p>
    </div>
  )
  return (
    <div className="panel scroll">
      <div className="rank-head">
        <div>{ivs.ranking.length} candidates evaluated on isolated graph copies in {(ivs.elapsed_ms / 1000).toFixed(2)} s · {ivs.ledger.queries} FalkorDB operations</div>
        {busy && <span className="pulse">updating</span>}
      </div>
      {top && (
        <div className="verdict">Best modeled recovery: <b>{top.label}</b>
          {top.share_of_disruption != null && <> — recovers {Math.round(top.share_of_disruption * 100)}% of the current aggregate detour</>}.</div>
      )}
      {useful.length === 0 && <p className="hint">No candidate improves access in this scenario.</p>}
      {useful.map(card)}
      {harmful.length > 0 && <details className="zero harmful" open={useful.length === 0}>
        <summary>{harmful.length} candidate{harmful.length === 1 ? '' : 's'} that would cut off zones or lengthen routes — not recommended</summary>
        {harmful.map((c, i) => card(c, useful.length + i))}
      </details>}
      {zero.length > 0 && <details className="zero">
        <summary>{zero.length} candidates with no effect on this metric — why?</summary>
        {zero.map((c, i) => card(c, useful.length + harmful.length + i))}
      </details>}
      {ivs.excluded.length > 0 && <div className="excluded">
        <h4>Excluded by planning constraints</h4>
        {ivs.excluded.map(c => <div key={c.id} className="excl"><b>{c.label}</b><span>{c.excluded_by?.kind ? `${c.excluded_by.kind.replace('_', ' ')}: ` : ''}{c.excluded_by?.meaning}</span></div>)}
      </div>}
      <p className="objective">{ivs.objective}</p>
    </div>
  )
}
