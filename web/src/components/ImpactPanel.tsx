import type { Evaluation, Meta } from '../api'
import { km, signedKm } from '../api'

interface Props {
  meta: Meta; evaluation: Evaluation | null; zoneName: Map<string, string>; cwLabel: Map<string, string>
  selectedZone: string | null; setSelectedZone: (z: string | null) => void
}

export default function ImpactPanel({ evaluation: ev, zoneName, cwLabel, selectedZone, setSelectedZone }: Props) {
  if (!ev) return <div className="impact skeleton" />
  const m = ev.metrics
  const changed = ev.zones.filter(z => z.status !== 'same').sort((a, b) => (b.delta ?? 1e9) - (a.delta ?? 1e9))
  const maxDelta = Math.max(1, ...changed.map(z => Math.abs(z.delta ?? 0)))
  const sel = ev.zones.find(z => z.zone === selectedZone)
  const disrupted = ev.scenario.closed_carriageways.length + ev.scenario.closed_segments.length > 0
    || ev.scenario.reversed_segments.length > 0 || ev.scenario.constraints.length > 0
  return (
    <div className="impact">
      <div className="kicker">Modeled access impact · {ev.rule.label}</div>
      {m.cut_off.length > 0 && (
        <div className="cutoff-alert">
          <b>{m.cut_off.length} zone{m.cut_off.length === 1 ? '' : 's'} cut off</b> — no modeled road access to any assistance
          point: {m.cut_off.map(z => zoneName.get(z)).join(', ')}. The distance figure below covers only zones that are still connected.
        </div>
      )}
      <div className="headline">
        <span className={m.total_delta_m > 0.5 ? 'num bad' : m.total_delta_m < -0.5 ? 'num good' : 'num'}>
          {disrupted ? (Math.abs(m.total_delta_m) < 50 ? '0.0 km' : signedKm(m.total_delta_m)) : km(ev.baseline_total_m)}</span>
        <span className="cap">{disrupted ? `aggregate change in modeled distance to the nearest assistance point${m.cut_off.length ? ' (connected zones only)' : ''}`
          : `aggregate modeled distance from ${m.eligible_zones} neighbourhood anchors to their nearest assistance point`}</span>
      </div>
      <div className="stats">
        <div><b>{m.affected_zones}</b><span>zones changed</span></div>
        <div><b className={m.cut_off.length ? 'bad' : ''}>{m.cut_off.length}</b><span>zones cut off</span></div>
        <div><b>{m.largest ? signedKm(m.largest.delta_m, 2) : '—'}</b><span>{m.largest ? `largest · ${zoneName.get(m.largest.zone)}` : 'largest detour'}</span></div>
      </div>
      {ev.closure_explanations.length > 0 && (() => {
        const used = ev.closure_explanations.filter(c => c.baseline_routes_using.length)
        const unused = ev.closure_explanations.filter(c => !c.baseline_routes_using.length)
        return (
          <ul className="why">
            {used.length > 0 && <li title={used.map(c => `${cwLabel.get(c.carriageway)}: ${c.baseline_routes_using.map(z => zoneName.get(z)).join(', ')}`).join('\n')}>
              Used by baseline routes: <b>{used.map(c => `${cwLabel.get(c.carriageway)} (${c.baseline_routes_using.length})`).join(' · ')}</b></li>}
            {unused.length > 0 && <li className="zero">No baseline route to the nearest assistance point used <b>{unused.map(c => cwLabel.get(c.carriageway)).join('; ')}</b>,
              so closing {unused.length === 1 ? 'it' : 'them'} cannot change this metric. That says nothing about their value for other trips.</li>}
          </ul>
        )
      })()}
      {changed.length > 0 && (
        <div className="ztable">
          {changed.slice(0, 3).map(z => (
            <button key={z.zone} className={`zrow ${selectedZone === z.zone ? 'sel' : ''}`} onClick={() => setSelectedZone(z.zone)}>
              <span className="zn">{zoneName.get(z.zone)}</span>
              <span className="zb"><i style={{ width: `${Math.min(100, (Math.abs(z.delta ?? maxDelta) / maxDelta) * 100)}%` }}
                className={z.status === 'cut_off' ? 'cut' : (z.delta ?? 0) < 0 ? 'good' : ''} /></span>
              <span className="zd">{z.status === 'cut_off' ? 'cut off' : signedKm(z.delta ?? 0, 2)}</span>
            </button>
          ))}
          {changed.length > 3 && <div className="more">+{changed.length - 3} more neighbourhoods on the map</div>}
        </div>
      )}
      {sel && (
        <div className="selzone">
          <b>{zoneName.get(sel.zone)}</b>
          {sel.status === 'cut_off' ? <span> has no modeled road access to an assistance point.</span>
            : <span> → {sel.help[0] ?? 'assistance point'}: {km(sel.ref, 2)} baseline → <b>{km(sel.cur, 2)}</b> now</span>}
        </div>
      )}
    </div>
  )
}
