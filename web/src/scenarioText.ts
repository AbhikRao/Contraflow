import type { Scenario } from './api'

/** Plain-language identity of a scenario: closures, constraints and the assistance-point rule. */
export function describeScenario(s: Scenario, cwLabel: Map<string, string>, ruleLabel?: string) {
  const closed = s.closed_carriageways.length + s.closed_segments.length
  const parts: string[] = []
  if (closed === 0) parts.push('no closures')
  else if (s.closed_carriageways.length)
    parts.push(`${[...new Set(s.closed_carriageways.map(c => (cwLabel.get(c) ?? c).split(' · ')[0]))].join(', ')} closed`
      + (s.closed_segments.length ? ` + ${s.closed_segments.length} road segment(s)` : ''))
  else parts.push(`${s.closed_segments.length} road segment(s) closed`)
  if (s.constraints.length) parts.push(`${s.constraints.length} planning constraint(s)`)
  return ruleLabel ? parts.join(', ') + ` · ${ruleLabel}` : `${parts.join(', ')} · rule ${s.rule}`
}
