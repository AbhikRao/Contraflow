import { useEffect, useState } from 'react'
import { api, type Candidate, type Evaluation, type RuleId, type Scenario } from './api'

export interface CompareRow {
  id: string; label: string; scenario: Scenario
  total_delta_m: number; affected_zones: number; cut_off: number; largest: { zone: string; delta_m: number } | null
}
export interface CompareData {
  rule: RuleId; baseline_total_m: number; eligible_zones: number
  rows: CompareRow[]
  /** best recovery from the last preset (all crossings closed), as ranked by the server */
  recovery: { from: string; best: Candidate; total_delta_m: number } | null
}

/** Evaluates the preset scenarios on FalkorDB for the selected rule. Nothing here is a constant: every figure shown
 *  in the comparison comes from /api/evaluate and /api/interventions. */
export function useCompare(presets: { id: string; label: string; closed: string[] }[], rule: RuleId) {
  const [data, setData] = useState<CompareData | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const sig = presets.map(p => `${p.id}:${p.closed.length}`).join('|')
  useEffect(() => {
    if (!presets.length) return
    const ctrl = new AbortController()
    setData(null); setErr(null)
    const sc = (closed: string[]): Scenario => ({ rule, closed_carriageways: closed, closed_segments: [], reversed_segments: [], constraints: [] })
    const run = async () => {
      const disrupted = presets.filter(p => p.closed.length > 0)
      const evals: Evaluation[] = []
      for (const p of disrupted) evals.push(await api.evaluate(sc(p.closed), ctrl.signal))
      const last = disrupted[disrupted.length - 1]
      const iv = await api.interventions(sc(last.closed), ctrl.signal)
      const best = iv.ranking.find(c => c.effect === 'restores_access' || c.effect === 'shorter')
      return {
        rule, baseline_total_m: evals[0].baseline_total_m, eligible_zones: evals[0].metrics.eligible_zones,
        rows: disrupted.map((p, i) => ({ id: p.id, label: p.label, scenario: sc(p.closed), total_delta_m: evals[i].metrics.total_delta_m,
          affected_zones: evals[i].metrics.affected_zones, cut_off: evals[i].metrics.cut_off.length, largest: evals[i].metrics.largest })),
        recovery: best ? { from: last.label, best, total_delta_m: iv.current.total_delta_m } : null,
      } as CompareData
    }
    run().then(d => { if (!ctrl.signal.aborted) setData(d) })
      .catch(e => { if (!ctrl.signal.aborted) setErr(e instanceof Error ? e.message : String(e)) })
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig, rule])
  return { data, err }
}
