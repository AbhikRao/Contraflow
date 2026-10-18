import { useEffect, useRef, useState } from 'react'
import { api, scenarioKey, type AgentReply, type AgentStatus, type AgentStep, type Meta, type Scenario } from '../api'
import { Investigation, LiveProgress, Pipeline, ProvTag } from './AgentParts'
import { TOOL_LABEL, stagesFrom } from './agentModel'
import { describeScenario } from '../scenarioText'

interface Props {
  scenario: Scenario
  push: (s: Scenario) => void
  cwLabel: Map<string, string>
  zoneName: Map<string, string>
  setPreview: (s: Scenario | null) => void
  meta: Meta
  setHighlight: (h: { eids: string[]; zones: string[]; labels?: { id: string; text: string }[] } | null) => void
  onUse?: () => void
  onOpenCase?: (id: string) => void
}
type HL = Props['setHighlight']

type Turn =
  | { role: 'user'; content: string }
  | { role: 'agent'; reply: AgentReply; applied: Set<string>; rejected: Set<string>; asked: string; requestKey: string; requestScenario: Scenario }

const SUGGESTIONS = [
  { group: 'Investigate an uncertain report', items: [
    'Saidapet bridge may be flooded. Which neighbourhoods are affected, keeping Kotturpuram reserved for ambulances?',
    'What if Kotturpuram bridge were closed?',
    'Guindy bridge might be under water',
  ] },
  { group: 'Act on a report or constraint', items: [
    'Saidapet bridge is blocked.',
    'Keep Maraimalai Adigal available for inbound emergency access.',
    'What should we reopen first?',
  ] },
  { group: 'Ask the graph', items: [
    "Why doesn't closing Thiru Vi Ka matter?",
    'Which roads matter most for hospital access?',
    'Does our earlier finding about Maraimalai Adigal still hold?',
  ] },
]
const QUICK = ['What if Kotturpuram bridge were closed?', 'Guindy bridge might be under water']
export default function AgentPanel({ scenario, push, setPreview, meta, setHighlight, onUse, cwLabel, onOpenCase }: Props) {
  const [turns, setTurns] = useState<Turn[]>([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<'auto' | 'llm' | 'deterministic'>('auto')
  const [modeOpen, setModeOpen] = useState(false)
  const [showMore, setShowMore] = useState(false)
  const [status, setStatus] = useState<AgentStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [live, setLive] = useState<AgentStep[]>([])
  const end = useRef<HTMLDivElement>(null)
  const lastCard = useRef<HTMLDivElement>(null)

  useEffect(() => { api.agentStatus().then(setStatus).catch(() => setStatus(null)) }, [])
  // a finished answer is read from its top (the story pipeline); only waiting/progress follows the bottom
  useEffect(() => {
    const last = turns[turns.length - 1]
    if (last?.role === 'agent') lastCard.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    else if (last) end.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [turns])
  useEffect(() => { if (busy) end.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [busy, live.length])

  const history = () => turns.map(t => t.role === 'user' ? { role: 'user', content: t.content }
    : { role: 'assistant', content: t.reply.message, focus: t.reply.focus })
  const currentKey = scenarioKey(scenario)

  const send = async (msg: string, confirm?: Record<string, unknown>) => {
    if (!msg.trim() || busy) return
    setErr(null)
    if (!confirm) setTurns(t => [...t, { role: 'user', content: msg }])
    onUse?.()
    setBusy(true); setText(''); setLive([])
    const requestKey = scenarioKey(scenario)
    try {
      // tool steps stream in as the server executes them (real progress, not a scripted list)
      const reply = await api.agentStream(msg, scenario, history(), mode, confirm, st => setLive(l => [...l, st]))
      setTurns(t => [...t, { role: 'agent', reply, applied: new Set(), rejected: new Set(), asked: msg, requestKey, requestScenario: scenario }])
    } catch (e) { setErr(String(e)) } finally { setBusy(false); setLive([]) }
  }

  const apply = (ti: number, pid: string) => {
    const t = turns[ti]
    if (t.role !== 'agent') return
    const idx = t.reply.proposals.findIndex(x => x.id === pid)
    const p = t.reply.proposals[idx]
    if (!p || !proposalFresh(t, idx, currentKey)) return      // stale proposals are never applied
    push(p.scenario)
    setPreview(null)
    if (p.memory_id) api.decide(p.memory_id, 'applied').catch(() => undefined)   // human decision recorded in memory
    setTurns(ts => ts.map((x, i) => i === ti && x.role === 'agent' ? { ...x, applied: new Set([...x.applied, pid]) } : x))
  }
  const reject = (ti: number, pid: string) => {
    const t = turns[ti]
    if (t.role !== 'agent') return
    const p = t.reply.proposals.find(x => x.id === pid)
    if (p?.memory_id) api.decide(p.memory_id, 'rejected').catch(() => undefined)
    setPreview(null)
    setTurns(ts => ts.map((x, i) => i === ti && x.role === 'agent' ? { ...x, rejected: new Set([...x.rejected, pid]) } : x))
  }

  const form = (
    <form className="agent-input" onSubmit={e => { e.preventDefault(); send(text) }}>
      <textarea value={text} onChange={e => setText(e.target.value)} placeholder="Describe a closure, report or question…"
        aria-label="Investigation input" rows={2} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(text) } }} />
      <button type="submit" disabled={busy || !text.trim()} aria-label="Run investigation">Investigate</button>
    </form>
  )
  const fresh = turns.length === 0
  const modeTitle = (m: typeof mode) => m === 'auto' ? 'Local LLM when available, otherwise rule-based planner' : m === 'llm' ? 'Local LLM chooses the graph tools' : 'Rule-based planner, no language model'
  return (
    <div className={`agent ${fresh ? 'fresh' : ''}`}>
      {fresh && form}
      {fresh && (
        <div className="agent-intro">
          <div className="suggest chips-row">
            {QUICK.map(x => <button key={x} onClick={() => send(x)}>{x}</button>)}
            <button className="more-btn" aria-expanded={showMore} onClick={() => setShowMore(o => !o)}>{showMore ? 'Fewer examples' : 'More examples'}</button>
          </div>
          {showMore && SUGGESTIONS.map(g => <div key={g.group} className="suggest-group"><h5>{g.group}</h5>
            <div className="suggest">{g.items.map(x => <button key={x} onClick={() => send(x)}>{x}</button>)}</div></div>)}
        </div>
      )}
      {!fresh && (
        <div className="agent-log scroll">
          {turns.map((t, i) => t.role === 'user'
            ? <div key={i} className="u-msg">
                {['investigate', 'close', 'reopen', 'negated'].includes((turns[i + 1] as { reply?: AgentReply } | undefined)?.reply?.intent?.action ?? '') &&
                  <ProvTag kind="report" />}{t.content}</div>
            : <div key={i} ref={i === turns.length - 1 ? lastCard : undefined}><AgentTurn t={t} onApply={pid => apply(i, pid)} onReject={pid => reject(i, pid)} onPreview={setPreview}
                currentKey={currentKey} onChoose={v => send(t.asked, { ...v, pending: t.reply.clarification?.pending })}
                onRetry={() => send(t.asked)} onSend={send} busy={busy} meta={meta} setHighlight={setHighlight} cwLabel={cwLabel} onOpenCase={onOpenCase} /></div>)}
          {busy && <LiveProgress steps={live} llm={mode !== 'deterministic' && !!status?.llm_available} model={status?.model} />}
          {err && <div className="err-inline">{err}</div>}
          <div ref={end} />
        </div>
      )}
      {fresh && busy && <div className="agent-log"><LiveProgress steps={live} llm={mode !== 'deterministic' && !!status?.llm_available} model={status?.model} /></div>}
      {fresh && err && <div className="err-inline" style={{ margin: '0 14px' }}>{err}</div>}
      {!fresh && form}
      <div className="agent-foot">
        <button className={`llm-status ${status?.llm_available ? 'ok' : 'off'}`} aria-expanded={modeOpen} onClick={() => setModeOpen(o => !o)}
          title={status?.reason ?? status?.base_url ?? 'Planner status'}>
          {status?.llm_available ? `${status.model} · local` : 'Rule-based planner'}
        </button>
        {modeOpen && (
          <div className="seg small" role="group" aria-label="Agent mode">
            {(['auto', 'llm', 'deterministic'] as const).map(m => (
              <button key={m} className={mode === m ? 'on' : ''} onClick={() => { setMode(m); setModeOpen(false) }} title={modeTitle(m)}>
                {m === 'auto' ? 'Auto' : m === 'llm' ? 'LLM' : 'Rules'}</button>
            ))}
          </div>
        )}
        <span className="foot-note">Reports are unverified · you approve every change</span>
      </div>
    </div>
  )
}

/** A proposal may be applied only onto the scenario it was computed for (or the preceding proposal's result). */
function proposalFresh(t: Extract<Turn, { role: 'agent' }>, idx: number, currentKey: string) {
  const base = idx === 0 ? t.requestKey : scenarioKey(t.reply.proposals[idx - 1].scenario)
  return currentKey === base
}

function AgentTurn({ t, onApply, onReject, onChoose, busy, onPreview, currentKey, onRetry, onSend, meta, setHighlight, cwLabel, onOpenCase }: {
  t: Extract<Turn, { role: 'agent' }>; onApply: (pid: string) => void; onReject: (pid: string) => void
  onPreview: (s: Scenario | null) => void; currentKey: string; onRetry: () => void; onSend: (m: string) => void
  onChoose: (v: Record<string, unknown>) => void; busy: boolean; meta: Meta; setHighlight: HL; cwLabel: Map<string, string>; onOpenCase?: (id: string) => void
}) {
  const r = t.reply
  const g = r.grounding
  const queries = r.steps.reduce((n, s) => n + (s.queries ?? 0), 0)
  return (
    <div className="a-card">
      <div className="a-meta">
        <span className={`mode ${r.mode.kind}`}>{r.mode.label}</span>
        <span>{(r.elapsed_ms / 1000).toFixed(1)} s · {r.steps.filter(s => s.kind === 'tool').length} tool calls · {queries} FalkorDB ops</span>
      </div>
      <div className={`origin ${t.requestKey !== currentKey ? 'stale' : ''}`} data-testid="card-origin">
        Computed for: <b>{describeScenario(t.requestScenario, cwLabel, meta.rules[t.requestScenario.rule].label)}</b>
        {t.requestKey !== currentKey && <span className="badge warn" data-testid="card-stale">Scenario changed since — historical result; ask again to recompute</span>}
      </div>
      <CardPipeline t={t} />
      {r.investigation_id && onOpenCase && <button className="case-btn" data-testid="open-case" onClick={() => onOpenCase(r.investigation_id!)}>Open case file — evidence from graph memory</button>}
      <div className="blk agent">
        <ProvTag kind={r.briefing_source === 'model' ? 'model' : r.briefing_source === 'deterministic' ? 'system' : 'agent'} />
        <Briefing text={r.message} />
      </div>
      <Investigation steps={r.steps} meta={meta} setHighlight={setHighlight} />
      {r.briefing_source === 'model' ? (
        <div className="ground warn" data-source="model">Model-written text: no graph tool produced this answer. Unverified.</div>
      ) : r.briefing_source === 'deterministic' ? (
        <div className={`ground ${g.ungrounded.length ? 'warn' : 'sys'}`} data-source="deterministic" title={g.method}>
          System-generated text (rule-based planner / runtime){g.numbers > 0
            ? ` · ${g.grounded}/${g.numbers} figures appear in graph tool outputs (numeric check, not semantic verification)`
            : ' · no graph figures quoted'}
        </div>
      ) : (
        <div className={`ground ${g.ungrounded.length ? 'warn' : 'ok'}`} data-source="tools" title={g.method}>
          {g.ungrounded.length
            ? `⚠ ${g.ungrounded.length} number(s) not found in tool output: ${g.ungrounded.join(', ')}`
            : `Briefing composed from graph tool results · ${g.grounded}/${g.numbers} numbers appear in tool outputs (numeric check, not semantic verification)`}
        </div>
      )}
      {r.clarification && t.requestKey !== currentKey && (
        <p className="stale">The scenario changed after this question; ask again to act on the current scenario.</p>)}
      {r.clarification && t.requestKey === currentKey && (
        <div className="clar">
          <b>{r.clarification.question}</b>
          <div>{r.clarification.options.map(o => (
            <button key={o.label} disabled={busy} onClick={() => onChoose(o.value)}>{o.label}</button>))}</div>
        </div>
      )}
      {(r.suggested_actions?.length ?? 0) > 0 && (
        <div className="next" data-testid="suggested-actions">
          <span>Nothing was changed. To act, send an explicit instruction (it becomes a proposal you still have to apply):</span>
          {r.suggested_actions!.map(a => <button key={a.message} disabled={busy} onClick={() => onSend(a.message)}>{a.label}</button>)}
        </div>
      )}
      {r.proposals.length > 0 && (
        <div className="props">
          {r.proposals.map((p, idx) => {
            const fresh = proposalFresh(t, idx, currentKey)
            return (
              <div key={p.id} className={`prop ${!fresh && !t.applied.has(p.id) ? 'is-stale' : ''}`}
                onMouseEnter={() => fresh && !t.applied.has(p.id) && onPreview(p.scenario)} onMouseLeave={() => onPreview(null)}>
                <ProvTag kind={t.applied.has(p.id) ? 'applied' : t.rejected.has(p.id) ? 'rejected' : 'proposed'}
                  label={!fresh && !t.applied.has(p.id) && !t.rejected.has(p.id) ? 'Proposal · out of date' : undefined} />
                <span className={`pk ${p.kind}`}>{p.kind}</span><span className="pl">{p.label}
                  {!fresh && !t.applied.has(p.id) && <em> — out of date: the scenario changed after this proposal</em>}</span>
                {t.applied.has(p.id) ? <span className="applied">Applied ✓</span>
                  : t.rejected.has(p.id) ? <span className="rejected">Rejected</span>
                  : fresh ? <><button onClick={() => onApply(p.id)}>Apply</button>
                      <button className="ghost" onClick={() => onReject(p.id)}>Reject</button></>
                    : <button className="retry" disabled={busy} onClick={onRetry}>Ask again</button>}
              </div>
            )
          })}
          <p className="hint">Hover a proposal to preview it on the map.{r.proposals.length > 1 ? ' Each proposal includes the ones before it.' : ''}</p>
        </div>
      )}
      <details className="trace">
        <summary>Tool trace ({r.steps.length} steps)</summary>
        <ol>
          {r.steps.map((s, i) => s.kind === 'thought'
            ? <li key={i} className="thought">{s.text}</li>
            : <li key={i} className={s.kind === 'error' ? 'terr' : ''}>
                <div className="tl"><code>{s.tool}</code><span>{TOOL_LABEL[s.tool ?? ''] ?? ''}</span>
                  <em>{s.ms} ms · {s.queries} ops</em></div>
                {s.args && Object.keys(s.args).length > 0 && <pre>{JSON.stringify(s.args)}</pre>}
                <div className="ts">{s.result_summary}</div>
              </li>)}
        </ol>
      </details>
      {r.model_note && (
        <details className="trace">
          <summary>Model's own wording — unverified, not used as the briefing{r.model_note.claims_applied ? ' ⚠' : ''}</summary>
          {r.model_note.claims_applied && <p className="stale">This wording implies a change was applied. Nothing is applied
            until you click Apply.</p>}
          <p className="mnote">{r.model_note.text}</p>
        </details>
      )}
    </div>
  )
}

/** The agent's briefing stays complete in the DOM; long ones are clamped to a few lines with an explicit expander. */
function Briefing({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const long = text.length > 320
  return (
    <>
      <p className={`a-text ${long && !open ? 'clamped' : ''}`}>{text}</p>
      {long && <button className="linklike" onClick={() => setOpen(o => !o)}>{open ? 'Show less' : 'Show full briefing'}</button>}
    </>
  )
}

/** The story of this reply. Each stage is marked done only if a tool for it actually completed. */
function CardPipeline({ t }: { t: Extract<Turn, { role: 'agent' }> }) {
  const r = t.reply
  const action = r.intent?.action ?? ''
  const done = (tool: string) => r.steps.some(s => s.kind === 'tool' && s.tool === tool)
  if (action === 'vulnerability' || action === 'recall') {
    const v = action === 'vulnerability'
    const st = (ok: boolean, label: string, key: string, detail?: string) => ({ key, label, state: ok ? 'done' as const : 'pending' as const, detail })
    const res = r.steps.find(s => s.tool === (v ? 'find_critical_roads' : 'recall_memory'))?.result as { evaluated?: number; findings?: unknown[] } | undefined
    return <Pipeline stages={v
      ? [st(true, 'Question', 'q'), st(done('find_critical_roads'), 'Scan', 's', res?.evaluated ? `${res.evaluated} candidates` : undefined),
        st(done('find_critical_roads'), 'Ranked', 'k', 'on graph copies'), st(true, 'Read the caveat', 'c', 'bounded, modeled')]
      : [st(true, 'Question', 'q'), st(done('recall_memory'), 'Recall', 'r', res?.findings ? `${res.findings.length} finding(s)` : undefined),
        st(done('recall_memory'), 'Re-evaluate now', 'e', 'current scenario'), st(true, 'Status', 's', 'applies / changed / stale')]} />
  }
  if (!['investigate', 'close', 'reopen'].includes(action)) return null
  return <Pipeline stages={stagesFrom(r.steps, { action, proposals: r.proposals.length, suggested: r.suggested_actions?.length ?? 0,
    applied: r.proposals.filter(p => t.applied.has(p.id)).length, rejected: r.proposals.filter(p => t.rejected.has(p.id)).length, final: true })} />
}
