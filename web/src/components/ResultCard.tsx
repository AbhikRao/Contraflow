import type { Evaluation, Interventions, Scenario } from '../api'
import { km, signedKm } from '../api'
import { describeScenario } from '../scenarioText'

interface Props {
  evaluation: Evaluation | null; ivs: Interventions | null; ivBusy: boolean; scenario: Scenario
  zoneName: Map<string, string>; cwLabel: Map<string, string>; onOpen: () => void
}

/** The default result: one modeled number, the best repair and how much of the disruption it recovers.
 *  Everything else (zone list, ranking, sensitivity, provenance) lives in the analysis drawer. */
export default function ResultCard({ evaluation: ev, ivs, ivBusy, scenario, zoneName, cwLabel, onOpen }: Props) {
  if (!ev) return <div className="result skeleton" aria-hidden />
  const m = ev.metrics
  const disrupted = scenario.closed_carriageways.length + scenario.closed_segments.length > 0
    || scenario.reversed_segments.length > 0 || scenario.constraints.length > 0
  const top = ivs?.ranking.find(c => c.effect === 'restores_access' || c.effect === 'shorter')
  const share = top?.share_of_disruption
  return (
    <section className="result impact" data-testid="result-card" aria-label="Modeled result">
      <div className="kicker">Modeled impact · {ev.rule.label}</div>
      <div className="headline">
        <span className={m.total_delta_m > 0.5 ? 'num bad' : m.total_delta_m < -0.5 ? 'num good' : 'num'}>
          {disrupted ? (Math.abs(m.total_delta_m) < 50 ? '0.0 km' : signedKm(m.total_delta_m)) : km(ev.baseline_total_m)}</span>
        <span className="cap" title={disrupted
          ? `Aggregate change in modeled shortest road distance from ${m.eligible_zones} neighbourhood anchors to their nearest assistance point${m.cut_off.length ? ' (connected zones only)' : ''}. Not travel time.`
          : `Aggregate modeled shortest road distance from ${m.eligible_zones} neighbourhood anchors to their nearest assistance point. Not travel time.`}>
          {disrupted ? 'added modeled distance' : 'modeled distance, baseline'}{disrupted && m.cut_off.length ? ' · connected zones' : ''}</span>
      </div>
      <div className="scn">{describeScenario(scenario, cwLabel, ev.rule.label)}</div>
      {m.cut_off.length > 0 && (
        <div className="cutoff-alert">
          <b>{m.cut_off.length} zone{m.cut_off.length === 1 ? '' : 's'} cut off</b> — no modeled road access to any assistance
          point: {m.cut_off.map(z => zoneName.get(z)).join(', ')}.
        </div>
      )}
      {disrupted && (
        <div className="best" data-testid="best-recovery">
          {top ? <>
            <span className="rec-pct">{share != null ? `${(share * 100).toFixed(1)}%` : '—'}</span>
            <span className="rec-txt">recovered by best repair<b>{top.label}</b></span>
          </> : <span className="rec-txt dim">{ivBusy || !ivs ? 'Evaluating repairs on graph copies…' : 'No candidate improves access.'}</span>}
        </div>
      )}
      <button className="primary-sm wide-btn" onClick={onOpen}>View analysis</button>
    </section>
  )
}
