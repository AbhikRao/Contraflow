// Typed client for the Contraflow API. Every number shown in the UI comes from these responses.

export type RuleId = 'A' | 'B'
export type ConstraintKind = 'reserve_emergency' | 'keep_direction' | 'no_reopen'

export interface Constraint { kind: ConstraintKind; target: string }

export interface Scenario {
  rule: RuleId
  closed_carriageways: string[]
  closed_segments: string[]
  reversed_segments: string[]
  constraints: Constraint[]
}

export const emptyScenario = (rule: RuleId = 'B'): Scenario => ({
  rule, closed_carriageways: [], closed_segments: [], reversed_segments: [], constraints: [],
})

export interface Carriageway { id: string; direction: string; eids: string[]; oneway: boolean; lanes: string[]; len: number }
export interface Crossing { id: string; name: string; roads: string[]; lat: number; lon: number; carriageways: Carriageway[] }
export interface Zone { id: string; name: string; lat: number; lon: number; snap_m: number; node: string }
export interface HelpPoint { id: string; name: string; lat: number; lon: number; node: string; snap_m: number; rules: string[] }

export interface Meta {
  name: string; subtitle: string; objective: string
  rules: Record<RuleId, { id: RuleId; label: string; definition: string }>
  constraint_kinds: Record<ConstraintKind, string>
  crossings: Crossing[]; zones: Zone[]; excluded_zones: (Zone & { reason: string })[]
  help_points: HelpPoint[]
  network: { junctions: number; segments: number; oneway_segments: number }
  source: Record<string, unknown> & { attribution: string; fetched_utc: string; bbox_w_s_e_n: number[] }
}

export interface LedgerEntry { graph: string; op: string; cypher: string; params: Record<string, unknown>; ms: number; rows: number }
export interface LedgerSummary {
  queries: number; total_ms: number
  by_op: Record<string, { count: number; ms: number; example: string }>
  entries: LedgerEntry[]
}

export interface ZoneRow {
  zone: string; ref: number | null; cur: number | null; delta?: number
  status: 'same' | 'changed' | 'cut_off' | 'restored'; help: string[]; help_node: string | null
}

export interface Evaluation {
  scenario: Scenario; scenario_key: string
  rule: { id: RuleId; label: string; definition: string }
  metrics: {
    eligible_zones: number; total_delta_m: number; affected_zones: number; cut_off: string[]
    largest: { zone: string; delta_m: number } | null
  }
  baseline_total_m: number
  zones: ZoneRow[]
  routes: Record<string, { baseline: string[]; current: string[] | null }>
  closed_eids: string[]
  closure_explanations: { carriageway: string; baseline_routes_using: string[]; note: string }[]
  elapsed_ms: number
  ledger: LedgerSummary
}

export interface Candidate {
  id: string; type: 'reopen_carriageway' | 'reopen_bridge' | 'reopen_segment' | 'contraflow'
  label: string; carriageways: string[]; segments: string[]
  effect: 'loses_access' | 'restores_access' | 'shorter' | 'longer' | 'mixed' | 'no_change'
  recovered_m: number; share_of_disruption: number | null
  zones_lost: string[]; zones_restored: string[]; zones_improved: { zone: string; saved_m: number }[]
  zones_worsened: { zone: string; added_m: number }[]
  after_total_delta_m: number; after_cut_off: string[]; routes_using_it: string[]; why: string
  base_key: string; result_scenario: Scenario; result_key: string
  excluded_by?: { code: string; kind?: ConstraintKind; target?: string; meaning: string; message?: string }
}

export interface Interventions {
  scenario: Scenario
  current: { total_delta_m: number; affected_zones: number; cut_off: string[] }
  ranking: Candidate[]; excluded: Candidate[]; objective: string
  elapsed_ms: number; ledger: LedgerSummary
}

export interface AgentStep {
  kind: 'tool' | 'thought' | 'error'
  tool?: string; args?: Record<string, unknown>; result_summary?: string
  result?: unknown; ms?: number; queries?: number; text?: string
}
export interface Proposal {
  id: string; kind: 'close' | 'reopen' | 'constraint' | 'contraflow' | 'apply_intervention'
  label: string; scenario: Scenario; report_id?: string; state: 'proposed'; scenario_key: string; base_key: string
  memory_id?: string
}
export interface AgentReply {
  mode: { kind: 'llm' | 'deterministic'; label: string; model?: string }
  message: string
  steps: AgentStep[]
  proposals: Proposal[]
  clarification?: { question: string; options: { label: string; value: Record<string, unknown> }[];
    candidates?: string[]; pending?: Record<string, unknown> } | null
  grounding: { numbers: number; grounded: number; ungrounded: string[]; method?: string }
  model_note?: { text: string; claims_applied: boolean; label: string; grounding: AgentReply['grounding'] } | null
  base_key?: string; focus?: string; stale?: boolean; briefing_source?: 'tools' | 'model' | 'deterministic'
  intent?: { action: string; constraint_kind?: string | null; direction?: string | null; negated_of?: string | null }
  elapsed_ms: number
  working_scenario: Scenario
  suggested_actions?: { label: string; message: string }[]
  investigation_id?: string | null
  hypothesis?: string[]
}

export interface VulnResult {
  id: string; kind: 'bridge' | 'carriageway' | 'road_link'; label: string; carriageways: string[]; segments: string[]
  highway: string; prominent: boolean; zones_cut_off: string[]; added_m: number; zones_longer: number
  worst: [string, number] | null; routes_using_now: string[]
  category: 'disconnects' | 'major_detour' | 'detour' | 'redundant'; flags: string[]; assistance_points?: string[]
}
export interface Vulnerability {
  scenario: Scenario; scenario_key: string; current_added_m: number
  candidate_set: { bridges_and_carriageways: number; busiest_links: number; prominent_unused_links: number;
    evaluated: number; exhaustive: false }
  criteria: string; results: VulnResult[]; elapsed_ms: number; ledger: LedgerSummary
}
export interface MemoryOverview {
  stats: { investigations: number; reports: number; findings: number; proposals: number; decisions: number }
  investigations: { id: string; ts: string; question: string; scenario_key: string; mode: string; findings: number;
    proposals: [string, string, string][] }[]
  status: Record<string, unknown>
}
export interface MemoryFinding {
  kind: 'disruption_impact' | 'recovery_ranking' | 'vulnerability'; then: string; assumptions: string[]
  same_scenario_as_now: boolean; now?: string | null; still_applies?: string | null
  then_added_km?: number | null; now_added_km?: number | null; hypothesis_label?: string | null
}
export interface MemoryReview {
  scenario_key: string; stats: MemoryOverview['stats']
  investigations: { investigation_id: string; when: string; report: string; places: string[]; scenario_then: Scenario
    findings: MemoryFinding[]; proposals: { label: string; state: string }[] }[]
}
export interface CaseFile {
  investigation_id: string; recorded_at: string; mode: string
  agent: { answered_by?: string | null; briefing_source?: string | null; elapsed_ms?: number | null }
  report: { text: string; status: string; provenance: string[] }
  resolved_entities: { name: string; kind?: string; resolution?: string; method?: string }[]
  scenario_then: { description: string; scenario_key: string; road_graph_fingerprint: string }
  commander_constraints: { kind: string; clause?: string; direction: string }[]
  assumptions: string[]
  impact: { hypothesis?: string; added_km?: number; zones_affected?: number; zones_cut_off?: string[]
    affected: { zone: string; delta_m: number | null; status: string }[] } | null
  alternatives: { action: string; effect: string; recovered_km: number }[]
  comparison: string | null; recommendation: string | null; recommendation_basis: string | null
  agent_steps: { kind: string; tool: string; summary: string; ms?: number; queries?: number }[]
  graph_operations: Record<string, number>; graph_operation_count?: number; briefing?: string
  human_decision: string[]
  current_status: { evaluated_against: string; scenario_key: string; rechecks: { finding: string; then: string; now?: string; still_applies?: string }[] }
  assembled_by: { graph: string; cypher: string }
  markdown: string
}
export interface RobustBest { id: string; label: string; effect: string; recovered_km: number; share_of_disruption: number | null
  zones_restored: string[]; zones_improved: number; routes_using_it: number; after_total_delta_km: number }
export interface Robustness {
  available: boolean; reason?: string; investigation_id?: string; tested_on?: string; scenario_key?: string
  evaluated_against_key?: string; stored_recommendation?: string | null; stored_matches_fresh?: boolean
  original?: RobustBest | null; current?: { total_delta_m: number; affected_zones: number; cut_off: string[] }
  summary?: string; method?: string; elapsed_ms?: number
  graph_operations?: { count: number; total_ms: number; by_op: Record<string, number> }
  variations?: { id: string; kind: string; changed: string; status?: string; best?: RobustBest | null; original_still_best?: boolean
    same_crossing?: boolean; reason?: string; rejected?: { code: string; message: string }
    current?: { total_delta_m: number; affected_zones: number; cut_off: string[] }
    zones_deciding?: { zone: string; name?: string; new_saves_m: number; original_saves_m: number }[] }[]
}
export type StreamEvent =
  | { type: 'step'; step: AgentStep }
  | { type: 'reply'; reply: AgentReply }
  | { type: 'error'; detail: { code?: string; message?: string } }

export interface AgentStatus { llm_available: boolean; model?: string; base_url?: string; reason?: string }

export class ApiError extends Error {
  status: number; code?: string
  constructor(message: string, status: number, code?: string) { super(message); this.status = status; this.code = code }
}

async function post<T>(url: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
  if (!r.ok) {
    const d = (await r.json().catch(() => ({ detail: r.statusText }))).detail
    throw new ApiError(typeof d === 'string' ? d : d?.message ?? r.statusText, r.status, typeof d === 'string' ? undefined : d?.code)
  }
  return r.json()
}

export const api = {
  meta: () => fetch('/api/meta').then(r => r.json() as Promise<Meta>),
  layers: () => fetch('/api/layers').then(r => r.json() as Promise<Layers>),
  evaluate: (s: Scenario, signal?: AbortSignal) => post<Evaluation>('/api/evaluate', s, signal),
  interventions: (s: Scenario, signal?: AbortSignal) =>
    post<Interventions>('/api/interventions', { scenario: s, include_contraflow: true }, signal),
  // Only the candidate id and the scenario key it was ranked for are sent; the server recomputes the result.
  applyCandidate: (current: Scenario, candidate: Candidate, signal?: AbortSignal) =>
    post<{ scenario: Scenario; scenario_key: string; candidate: Candidate }>('/api/apply_candidate',
      { current, candidate_id: candidate.id, base_key: candidate.base_key }, signal),
  agent: (message: string, scenario: Scenario, history: unknown[], mode?: string, confirm?: unknown) =>
    post<AgentReply>('/api/agent', { message, scenario, history, mode, confirm }),
  /** Same agent run as `agent`, but tool steps are delivered as server-sent events while they execute. */
  agentStream: async (message: string, scenario: Scenario, history: unknown[], mode: string | undefined,
    confirm: unknown, onStep: (s: AgentStep) => void): Promise<AgentReply> => {
    const r = await fetch('/api/agent/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, scenario, history, mode, confirm }) })
    if (!r.ok || !r.body) {
      const d = (await r.json().catch(() => ({ detail: r.statusText }))).detail
      throw new ApiError(typeof d === 'string' ? d : d?.message ?? r.statusText, r.status, typeof d === 'string' ? undefined : d?.code)
    }
    const reader = r.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      let i
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2)
        if (!chunk.startsWith('data: ')) continue
        const ev = JSON.parse(chunk.slice(6)) as StreamEvent
        if (ev.type === 'step') onStep(ev.step)
        else if (ev.type === 'reply') return ev.reply
        else throw new ApiError(ev.detail.message ?? 'agent error', 500, ev.detail.code)
      }
    }
    throw new ApiError('agent stream ended without a reply', 500)
  },
  vulnerability: (s: Scenario, signal?: AbortSignal) => post<Vulnerability>('/api/vulnerability', { scenario: s, top_links: 20 }, signal),
  caseFile: (id: string, scenario: Scenario) => post<CaseFile>(`/api/case/${encodeURIComponent(id)}`, { scenario }),
  caseRobustness: (id: string, scenario: Scenario) => post<Robustness>(`/api/case/${encodeURIComponent(id)}/robustness`, { scenario }),
  memoryReview: (scenario: Scenario, signal?: AbortSignal) => post<MemoryReview>('/api/memory/review', { scenario }, signal),
  memory: () => fetch('/api/memory').then(r => r.json() as Promise<MemoryOverview>),
  decide: (proposal_id: string, state: 'applied' | 'rejected') => post<{ ok: boolean }>('/api/memory/decision', { proposal_id, state }),
  agentStatus: () => fetch('/api/agent/status').then(r => r.json() as Promise<AgentStatus>),
}

export interface Layers {
  roads: GeoJSON.FeatureCollection<GeoJSON.LineString, {
    eid: string; name: string; highway: string; oneway: boolean; len: number; crossing: string; carriageway: string
  }>
  river: GeoJSON.FeatureCollection
  zones: GeoJSON.FeatureCollection<GeoJSON.Point>
  help: GeoJSON.FeatureCollection<GeoJSON.Point, { id: string; name: string; rules: string; node: string }>
  crossings: GeoJSON.FeatureCollection<GeoJSON.Point, { id: string; name: string }>
}

export const km = (m: number | null | undefined, digits = 1) =>
  m == null ? '—' : `${(m / 1000).toFixed(digits)} km`
export const signedKm = (m: number, digits = 1) => `${m >= 0 ? '+' : '−'}${(Math.abs(m) / 1000).toFixed(digits)} km`

/** Order-insensitive identity of a scenario, used to detect stale proposals. */
export const scenarioKey = (s: Scenario) => JSON.stringify({
  rule: s.rule, closed_carriageways: [...s.closed_carriageways].sort(), closed_segments: [...s.closed_segments].sort(),
  reversed_segments: [...s.reversed_segments].sort(),
  constraints: s.constraints.map(c => `${c.kind}|${c.target}`).sort(),
})
