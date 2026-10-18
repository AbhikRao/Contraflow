import { type AgentStep } from '../api'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Res = Record<string, any>
export const resOf = (steps: AgentStep[], tool: string) =>
  steps.filter(s => s.kind === 'tool' && s.tool === tool).map(s => s.result as Res).filter(Boolean)

export const TOOL_LABEL: Record<string, string> = {
  assess_field_report: 'Assess field report', search_location: 'Resolve location', inspect_crossing: 'Inspect crossing',
  inspect_scenario: 'Inspect scenario', evaluate_closure: 'Evaluate closure (graph copy)', propose_closure: 'Propose closure',
  propose_reopen: 'Propose reopening', set_planning_constraint: 'Set constraint', rank_interventions: 'Rank interventions',
  get_evidence: 'Retrieve evidence', explain_crossing: 'Explain crossing',
  investigate_disruption: 'Investigate disruption (graph copy)', assume_constraint: 'Assume constraint (analysis only)',
  rank_recovery: 'Rank recovery under hypothesis', compare_options: 'Compare alternatives', trace_route: 'Trace route',
  recall_memory: 'Recall memory', find_critical_roads: 'Access-sensitivity scan',
}

/** Stage = a step of the story. Its state comes only from tools that actually completed in this reply. */
export interface Stage { key: string; label: string; state: 'done' | 'pending' | 'attention' | 'na'; detail?: string }

export function stagesFrom(steps: AgentStep[], opts: { action?: string; proposals: number; suggested: number
  applied: number; rejected: number; final: boolean }): Stage[] {
  const has = (tool: string) => steps.some(s => s.kind === 'tool' && s.tool === tool)
  const inv = resOf(steps, 'investigate_disruption')[0]
  const assess = resOf(steps, 'assess_field_report')[0]
  const rank = resOf(steps, 'rank_recovery')[0] ?? resOf(steps, 'rank_interventions').slice(-1)[0]
  const cmp = resOf(steps, 'compare_options')[0]
  const located = inv?.resolved ?? assess?.resolved ?? resOf(steps, 'search_location').find(r => r.status === 'resolved')?.resolved?.name
  const ambiguous = [inv, assess, ...resOf(steps, 'search_location')].some(r => r?.needs_confirmation)
  const proposed = resOf(steps, 'propose_closure').slice(-1)[0]
  const addedKm = inv?.added_km ?? assess?.added_km ?? proposed?.added_km
  const zones = inv?.zones_affected ?? assess?.zones_affected ?? proposed?.zones_affected
  const impactDone = addedKm != null
  const options = rank?.ranking?.length ?? rank?.candidates_evaluated
  // only a candidate that actually improves access can be the recommendation
  const helpful = (rank?.ranking ?? []).filter((c: Res) => c.effect === 'restores_access' || c.effect === 'shorter')
  const winner = cmp?.winner ?? helpful[0]?.action
  const noChange = impactDoneOf(addedKm) && Math.abs(addedKm) < 0.005
  const decided = opts.applied + opts.rejected
  const decision: Stage = !opts.final ? { key: 'decision', label: 'Human decision', state: 'pending' }
    : decided > 0 ? { key: 'decision', label: 'Human decision', state: 'done', detail: opts.applied ? 'applied by you' : 'rejected by you' }
    : opts.proposals + opts.suggested > 0 ? { key: 'decision', label: 'Human decision', state: 'attention', detail: 'awaiting you' }
    : { key: 'decision', label: 'Human decision', state: 'na', detail: 'no change requested' }
  const out: Stage[] = [
    { key: 'report', label: 'Report', state: steps.some(s => s.kind === 'tool') || opts.final ? 'done' : 'pending', detail: 'unverified' },
    { key: 'investigation', label: 'Investigation', state: ambiguous ? 'attention' : located ? 'done' : 'pending',
      detail: ambiguous ? 'location needs your choice' : located },
    { key: 'impact', label: 'Impact', state: impactDone ? 'done' : 'pending',
      detail: impactDone ? `${Math.abs(addedKm) < 0.005 ? 'no change' : `+${addedKm} km`}${zones != null ? ` · ${zones} zone${zones === 1 ? '' : 's'}` : ''}` : undefined },
    rank || cmp ? { key: 'alternatives', label: 'Alternatives', state: 'done', detail: options ? `${options} evaluated` : undefined }
      : noChange && opts.final ? { key: 'alternatives', label: 'Alternatives', state: 'na', detail: 'not needed: no modeled change' }
      : { key: 'alternatives', label: 'Alternatives', state: 'pending' },
    winner ? { key: 'recommendation', label: 'Recommendation', state: 'done', detail: winner }
      : rank && opts.final ? { key: 'recommendation', label: 'Recommendation', state: 'done', detail: 'no repair improves access' }
      : noChange && opts.final ? { key: 'recommendation', label: 'Recommendation', state: 'na', detail: 'nothing to repair' }
      : { key: 'recommendation', label: 'Recommendation', state: 'pending' },
    decision,
  ]
  return out.map(s => (s.key === 'impact' && !impactDone && has('rank_interventions') ? { ...s, state: 'na' as const, detail: 'see ranking' } : s))
}


function impactDoneOf(v: unknown): v is number { return typeof v === 'number' }
