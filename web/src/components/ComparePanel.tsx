import type { Candidate } from '../api'
import { km, signedKm } from '../api'
import type { CompareData } from '../hooks'

interface Props {
  data: CompareData | null; err: string | null; activeId: string | null; busy: boolean
  zoneName: Map<string, string>
  onPick: (id: string) => void
  setHovered: (c: Candidate | null) => void
  onOpenRecovery: (rowId: string) => void
}

const rowTitle = (label: string) => label.replace(/ closed$/, '')
const fmt = (m: number) => (Math.abs(m) < 50 ? '0.0 km' : signedKm(m, 2))

/** One comparison: the preset closures on a single shared scale, computed live by FalkorDB for the selected rule.
 *  The most disrupted row also shows how much of its added distance the best repair recovers. */
export default function ComparePanel({ data, err, activeId, zoneName, onPick, setHovered, onOpenRecovery }: Props) {
  if (err) return <div className="panel scroll"><p className="err-inline">{err}</p></div>
  if (!data) return <div className="panel scroll empty-state"><p>Evaluating the preset closures on isolated FalkorDB graph copies…</p></div>
  const max = Math.max(1, ...data.rows.map(r => Math.abs(r.total_delta_m)))
  const rec = data.recovery
  const share = rec && data.rows.length ? rec.best.share_of_disruption : null
  const lastId = data.rows[data.rows.length - 1]?.id
  const zero = data.rows.filter(r => Math.abs(r.total_delta_m) < 50)
  return (
    <div className="panel scroll compare" data-testid="compare-panel">
      <h3>Added modeled distance by closure <small>computed live by FalkorDB · rule-dependent</small></h3>
      <div className="cmp-rows">
        {data.rows.map(r => {
          const isZero = Math.abs(r.total_delta_m) < 50
          const w = isZero ? 0 : Math.max(3, (Math.abs(r.total_delta_m) / max) * 100)
          const recovered = rec && r.id === lastId && share != null ? w * share : 0
          return (
            <button key={r.id} className={`cmp-row ${activeId === r.id ? 'on' : ''} ${isZero ? 'zero' : ''}`} onClick={() => onPick(r.id)}
              data-testid={`cmp-${r.id}`}>
              <span className="cmp-label">{rowTitle(r.label)}</span>
              <span className="cmp-val">{fmt(r.total_delta_m)}</span>
              <span className="cmp-bar" aria-hidden>
                {isZero ? <i style={{ width: '2px' }} /> : <>
                  {recovered > 0 && <i className="rec" style={{ width: `${recovered}%` }} />}
                  <i style={{ width: `${w - recovered}%` }} /></>}
              </span>
              <span className="cmp-sub">{isZero ? 'no neighbourhood\'s shortest route changes'
                : `${r.affected_zones} neighbourhood${r.affected_zones === 1 ? '' : 's'} farther${r.largest ? ` · worst ${zoneName.get(r.largest.zone)} ${signedKm(r.largest.delta_m, 2)}` : ''}${r.cut_off ? ` · ${r.cut_off} cut off` : ''}`}</span>
            </button>
          )
        })}
      </div>
      {rec && (
        <div className="cmp-recovery" data-testid="cmp-recovery"
          onMouseEnter={() => setHovered(rec.best)} onMouseLeave={() => setHovered(null)}>
          {share != null && (
            <div className="keyfinding" data-testid="key-finding">
              <button className="kf rec" onClick={() => lastId && onOpenRecovery(lastId)} title={rec.best.label}>
                <b>{(share * 100).toFixed(1)}%</b><span>of the added distance is recovered by the best repair</span></button>
            </div>
          )}
          <div className="cmp-rec-line"><b>{rec.best.label}</b> · {km(rec.best.recovered_m, 2)} recovered of the added {km(rec.total_delta_m, 2)}
            <span className="sr-only"> Best repair from “{rec.from}”.</span></div>
          <button className="linklike" onClick={() => lastId && onOpenRecovery(lastId)}>Open full ranking →</button>
        </div>
      )}
      {zero.length > 0 && (
        <p className="cmp-takeaway">
          <b>{zero.map(r => rowTitle(r.label)).join(' or ')}: 0.0 km.</b> No neighbourhood's shortest route to its nearest assistance point
          uses {zero.length === 1 ? 'it' : 'them'}; a bridge's visibility on the map is not its modeled impact.
        </p>
      )}
      <details className="method">
        <summary>Method and limits</summary>
        <p className="caveat">Aggregate over {data.eligible_zones} neighbourhood anchors; baseline {km(data.baseline_total_m)}. Modeled shortest road distance on the
          OpenStreetMap graph under the selected assistance-point rule — not travel time, traffic or emergency outcomes. A road that changes nothing here may still matter for other trips.</p>
      </details>
    </div>
  )
}
