import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, emptyScenario, scenarioKey, type Candidate, type Evaluation, type Interventions, type Layers, type Meta,
  type RuleId, type Scenario, type Vulnerability } from './api'
import MapView from './components/MapView'
import ScenarioPanel from './components/ScenarioPanel'
import ImpactPanel from './components/ImpactPanel'
import ResultCard from './components/ResultCard'
import RecoveryPanel from './components/RecoveryPanel'
import EvidencePanel from './components/EvidencePanel'
import AgentPanel from './components/AgentPanel'
import AssumptionsModal from './components/AssumptionsModal'
import VulnerabilityPanel from './components/VulnerabilityPanel'
import ComparePanel from './components/ComparePanel'
import MemoryPanel from './components/MemoryPanel'
import { useCompare } from './hooks'
import CaseFileView from './components/CaseFileView'

export type Preset = { id: string; label: string; closed: string[] }
type View = 'investigate' | 'compare' | 'history'
type Sec = 'recovery' | 'vuln' | 'evidence'

function parseHash(): { view: View; drawer: boolean; sec: Sec | null; caseId: string | null } {
  const out = { view: 'investigate' as View, drawer: false, sec: null as Sec | null, caseId: null as string | null }
  try {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean)
    const ci = parts.indexOf('case')
    if (ci >= 0 && parts[ci + 1]) { out.caseId = decodeURIComponent(parts[ci + 1]); parts.splice(ci) }
    const [a, b] = parts
    if (a === 'compare') out.view = 'compare'
    else if (a === 'history' || a === 'memory') out.view = 'history'
    else if (a === 'analysis' || a === 'recovery' || a === 'sensitivity' || a === 'evidence') {
      out.drawer = true
      const k = a === 'analysis' ? b : a
      out.sec = k === 'recovery' ? 'recovery' : k === 'sensitivity' || k === 'vuln' ? 'vuln' : k === 'evidence' ? 'evidence' : null
    }
  } catch { /* no location (tests, SSR) */ }
  return out
}

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [layers, setLayers] = useState<Layers | null>(null)
  const [history, setHistory] = useState<Scenario[]>([emptyScenario('B')])
  const scenario = history[history.length - 1]
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null)
  const [ivs, setIvs] = useState<Interventions | null>(null)
  const [busy, setBusy] = useState({ eval: false, iv: false })
  const [error, setError] = useState<string | null>(null)
  const [selectedZone, setSelectedZone] = useState<string | null>(null)
  const [hovered, setHovered] = useState<Candidate | null>(null)
  const [preview, setPreview] = useState<Scenario | null>(null)
  const [closeMode, setCloseMode] = useState(false)
  // Three top-level views. Investigate is the map plus a floating investigation card and a compact result card; the
  // analysis drawer, Compare and History are single right-hand panels, so the map is never squeezed by two panels at once.
  const init = parseHash()
  const [view, setView] = useState<View>(init.view)
  const [drawer, setDrawer] = useState(init.drawer)
  const [secChoice, setSecChoice] = useState<Sec | 'none' | null>(init.sec)
  const [leftMode, setLeftMode] = useState<'agent' | 'scenario'>('agent')
  const [leftMin, setLeftMin] = useState(init.drawer)
  const [presetsOpen, setPresetsOpen] = useState(false)
  const [compact, setCompact] = useState(() => typeof window !== 'undefined' && window.innerWidth < 1180)
  const [legendOpen, setLegendOpen] = useState(false)
  const [caseId, setCaseId] = useState<string | null>(init.caseId)
  const isDisrupted = scenario.closed_carriageways.length + scenario.closed_segments.length > 0
    || scenario.constraints.some(c => c.kind === 'reserve_emergency')
  // Until the user picks a section, the repair ranking is open once something is disrupted.
  const activeSec: Sec | null = secChoice === 'none' ? null : secChoice ?? (isDisrupted ? 'recovery' : null)
  const rightVisible = view !== 'investigate' || drawer
  const [vuln, setVuln] = useState<Vulnerability | null>(null)
  const [vulnBusy, setVulnBusy] = useState(false)
  const [vulnErr, setVulnErr] = useState<string | null>(null)
  const [highlight, setHighlight] = useState<{ eids: string[]; zones: string[]; labels?: { id: string; text: string }[] } | null>(null)
  const [showAssumptions, setShowAssumptions] = useState(false)
  const seq = useRef(0)

  useEffect(() => {
    const onResize = () => setCompact(window.innerWidth < 1180)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // Deep links: #compare, #history, #analysis[/recovery|/sensitivity|/evidence], #case/<id> (legacy tab names still work).
  useEffect(() => {
    const h = view === 'compare' ? 'compare' : view === 'history' ? 'history'
      : drawer ? `analysis${activeSec ? '/' + (activeSec === 'vuln' ? 'sensitivity' : activeSec) : ''}` : ''
    const full = caseId ? `${h ? h + '/' : ''}case/${caseId}` : h
    try { if (location.hash.replace(/^#\/?/, '') !== full) window.history.replaceState(null, '', full ? `#${full}` : location.pathname + location.search) } catch { /* sandboxed */ }
  }, [view, drawer, activeSec, caseId])

  useEffect(() => {
    Promise.all([api.meta(), api.layers()]).then(([m, l]) => { setMeta(m); setLayers(l) })
      .catch(e => setError(`Cannot reach the Contraflow API: ${e}`))
  }, [])

  // Monotonic revision of user-visible scenario state. Every user edit bumps it; asynchronous results captured
  // at an older revision are discarded when they arrive (B3).
  const revision = useRef(0)
  const [applying, setApplying] = useState<number | null>(null)
  const commit = useCallback((fn: (h: Scenario[]) => Scenario[]) => {
    revision.current += 1
    setApplying(null)                       // any pending candidate application is superseded
    setHistory(fn)
  }, [])
  const push = useCallback((next: Scenario) => commit(h => [...h, next]), [commit])
  const undo = () => commit(h => (h.length > 1 ? h.slice(0, -1) : h))
  const reset = () => commit(h => [...h, emptyScenario(scenario.rule)])

  // Every scenario change is re-evaluated by FalkorDB; ranking follows when anything is disrupted.
  // Only the response for the current scenario is ever displayed; older requests are aborted.
  useEffect(() => {
    const my = ++seq.current
    const ctrl = new AbortController()
    const fail = (e: unknown) => {
      if (my !== seq.current || ctrl.signal.aborted) return
      setError(e instanceof Error ? e.message : String(e))
      // A scenario the server rejects (e.g. a constraint violation) is rolled back, never kept on screen.
      if (e instanceof ApiError && e.status === 422) commit(h => (h.length > 1 ? h.slice(0, -1) : h))
    }
    setIvs(null)
    setBusy({ eval: true, iv: false })
    api.evaluate(scenario, ctrl.signal).then(r => {
      if (my !== seq.current) return
      setEvaluation(r); setError(null)
      // adopt the server's canonical representation so every later comparison uses the same scenario identity
      if (scenarioKey(r.scenario) !== scenarioKey(scenario)) setHistory(h => [...h.slice(0, -1), r.scenario])
    })
      .catch(fail).finally(() => my === seq.current && setBusy(b => ({ ...b, eval: false })))
    const disrupted = scenario.closed_carriageways.length + scenario.closed_segments.length > 0
      || scenario.constraints.some(c => c.kind === 'reserve_emergency')
    if (!disrupted) return () => ctrl.abort()
    setBusy(b => ({ ...b, iv: true }))
    const t = setTimeout(() => {
      api.interventions(scenario, ctrl.signal).then(r => { if (my === seq.current) setIvs(r) })
        .catch(fail).finally(() => my === seq.current && setBusy(b => ({ ...b, iv: false })))
    }, 150)
    return () => { clearTimeout(t); ctrl.abort() }
  }, [scenario, commit])

  // Vulnerability analysis runs only while its tab is open, for the current scenario; older responses are discarded.
  useEffect(() => {
    if (view !== 'investigate' || !drawer || activeSec !== 'vuln') return
    const ctrl = new AbortController()
    const key = scenarioKey(scenario)
    setVulnBusy(true); setVulnErr(null)
    setVuln(v => (v && scenarioKey(v.scenario) === key ? v : null))
    const t = setTimeout(() => {
      api.vulnerability(scenario, ctrl.signal).then(v => { if (!ctrl.signal.aborted) setVuln(v) })
        .catch(e => { if (!ctrl.signal.aborted) setVulnErr(e instanceof Error ? e.message : String(e)) })
        .finally(() => { if (!ctrl.signal.aborted) setVulnBusy(false) })
    }, 250)
    return () => { clearTimeout(t); ctrl.abort() }
  }, [view, drawer, activeSec, scenario])

  const zoneName = useMemo(() => new Map((meta?.zones ?? []).map(z => [z.id, z.name])), [meta])
  const cwLabel = useMemo(() => {
    const m = new Map<string, string>()
    meta?.crossings.forEach(x => x.carriageways.forEach(c => m.set(c.id, `${x.name} · ${c.direction}`)))
    return m
  }, [meta])

  const presets: Preset[] = useMemo(() => {
    if (!meta) return []
    const bridge = (name: string) => meta.crossings.find(x => x.name.startsWith(name))?.carriageways.map(c => c.id) ?? []
    return [
      { id: 'baseline', label: 'Baseline', closed: [] },
      { id: 'tvk', label: 'Thiru Vi Ka closed', closed: bridge('Thiru Vi Ka') },
      { id: 'mab', label: 'Maraimalai Adigal closed', closed: bridge('Maraimalai Adigal') },
      { id: 'all', label: 'All river crossings closed', closed: meta.crossings.flatMap(x => x.carriageways.map(c => c.id)) },
    ]
  }, [meta])

  const { data: compare, err: compareErr } = useCompare(presets, scenario.rule)
  const deltaLabel = (id: string) => {
    const row = compare?.rows.find(x => x.id === id)
    return row ? (Math.abs(row.total_delta_m) < 50 ? '0.0 km' : `+${(row.total_delta_m / 1000).toFixed(1)} km`) : ''
  }

  const applyPreset = (p: Preset) => push({ ...scenario, closed_carriageways: p.closed, closed_segments: [], reversed_segments: [] })
  const setRule = (rule: RuleId) => push({ ...scenario, rule })
  const toggleCarriageway = (id: string) => push({
    ...scenario, closed_carriageways: scenario.closed_carriageways.includes(id)
      ? scenario.closed_carriageways.filter(c => c !== id) : [...scenario.closed_carriageways, id],
  })
  const onRoadClick = (eid: string, carriageway: string) => {
    if (carriageway) return toggleCarriageway(carriageway)
    if (scenario.reversed_segments.includes(eid))
      return push({ ...scenario, reversed_segments: scenario.reversed_segments.filter(e => e !== eid) })
    push({ ...scenario, closed_segments: scenario.closed_segments.includes(eid)
      ? scenario.closed_segments.filter(e => e !== eid) : [...scenario.closed_segments, eid] })
  }
  // Candidates are applied by the server, which recomputes the operation from the current scenario (stale -> 409).
  // The response is used only if no user edit happened while it was in flight.
  const applyCandidate = (c: Candidate) => {
    const myRev = revision.current
    setApplying(myRev)
    api.applyCandidate(scenario, c)
      .then(r => {
        if (revision.current !== myRev) return              // superseded by a newer user choice: discard
        push(r.scenario)
      })
      .catch(e => { if (revision.current === myRev) setError(e instanceof Error ? e.message : String(e)) })
      .finally(() => setApplying(a => (a === myRev ? null : a)))
  }

  if (error && !meta) return <div className="fatal">{error}<br />Start FalkorDB and the API (see README).</div>
  if (!meta || !layers) return <div className="loading">Loading Adyar road network…</div>

  const activePreset = presets.find(p => p.closed.length === scenario.closed_carriageways.length
    && p.closed.every(c => scenario.closed_carriageways.includes(c))
    && scenario.closed_segments.length === 0 && scenario.reversed_segments.length === 0)
  const go = (v: View) => { setView(v); setPresetsOpen(false); if (v === 'investigate') setDrawer(false); if (v !== 'investigate') setLeftMin(false) }
  const openDrawer = (sec?: Sec) => {
    setView('investigate'); setDrawer(true); setLeftMin(true)
    if (sec) setSecChoice(sec)
  }
  const closeRight = () => { if (view === 'investigate') { setDrawer(false); setLeftMin(false) } else go('investigate') }
  const expandLeft = () => { setLeftMin(false); if (compact) setDrawer(false) }
  const pickSec = (k: Sec) => setSecChoice(activeSec === k ? 'none' : k)
  const dismissLeft = view !== 'investigate'
  const leftHidden = dismissLeft || leftMin
  const rightTitle = view === 'compare' ? 'Compare closures' : view === 'history' ? 'History · graph memory' : 'Analysis'

  return (
    <div className={`app ${compact ? 'compact' : ''} ${rightVisible ? '' : 'r-off'}`}>
      <header className="top">
        <div className="brand">
          <div className="logo" aria-hidden><span /><span /></div>
          <div><h1>Contraflow</h1><p>{meta.subtitle}</p></div>
        </div>
        <nav className="seg viewnav" aria-label="View">
          {([['investigate', 'Investigate'], ['compare', 'Compare'], ['history', 'History']] as const).map(([k, label]) => (
            <button key={k} className={view === k ? 'on' : ''} aria-current={view === k ? 'page' : undefined} onClick={() => go(k)}>{label}</button>
          ))}
        </nav>
        <div className="top-right">
          <div className="seg" role="group" aria-label="Assistance-point rule">
            {(['B', 'A'] as RuleId[]).map(r => (
              <button key={r} className={scenario.rule === r ? 'on' : ''} onClick={() => setRule(r)}
                title={`${meta.rules[r].definition} (${meta.help_points.filter(h => h.rules.includes(r)).length} assistance points)`}>
                {meta.rules[r].label}
              </button>
            ))}
          </div>
          <button className="ghost" onClick={() => setShowAssumptions(true)} title="How it works, data and limits">About</button>
          <span className="sandbox">Tabletop · not operational</span>
        </div>
      </header>

      <main className="center">
        <MapView meta={meta} layers={layers} evaluation={evaluation} rule={scenario.rule} selectedZone={selectedZone}
          hovered={hovered} closeMode={closeMode} reversed={scenario.reversed_segments}
          preview={preview} current={scenario} highlight={highlight}
          onRoadClick={onRoadClick} onZoneClick={z => setSelectedZone(s => (s === z ? null : z))} />

        <aside className={`left ${leftHidden ? 'hidden' : ''}`} aria-label="Investigation">
          <div className="left-head">
            {leftMode === 'agent'
              ? <b>Investigate a road disruption</b>
              : <button className="linklike" onClick={() => setLeftMode('agent')}>← Back to investigation</button>}
            <span className="left-tools">
              {leftMode === 'agent' && <button className="ghost" onClick={() => setLeftMode('scenario')} title="Close roads, set constraints and contraflow by hand">Edit scenario</button>}
              <button className="ghost" onClick={() => setLeftMin(true)} aria-label="Minimize investigation panel" title="Minimize">–</button>
            </span>
          </div>
          <div className={leftMode === 'scenario' ? 'tabpane' : 'tabpane hidden'}>
            <ScenarioPanel meta={meta} scenario={scenario} evaluation={evaluation} closeMode={closeMode}
                setCloseMode={setCloseMode} toggleCarriageway={toggleCarriageway} push={push} undo={undo} reset={reset}
                canUndo={history.length > 1} layers={layers} cwLabel={cwLabel} />
          </div>
          {/* kept mounted so the conversation survives view and panel switches */}
          <div className={leftMode === 'agent' ? 'tabpane' : 'tabpane hidden'}>
            <AgentPanel scenario={scenario} push={push} cwLabel={cwLabel} zoneName={zoneName} setPreview={setPreview}
              meta={meta} setHighlight={setHighlight} onOpenCase={setCaseId} />
          </div>
        </aside>
        {view === 'investigate' && leftMin && (
          <button className="left-pill" onClick={expandLeft} aria-label="Open investigation panel">Investigate a road disruption</button>
        )}

        {((view === 'investigate' && !drawer) || (view === 'compare' && isDisrupted)) && (
          <ResultCard evaluation={evaluation} ivs={ivs} ivBusy={busy.iv} scenario={scenario} zoneName={zoneName} cwLabel={cwLabel}
            onOpen={() => openDrawer()} />
        )}

        <div className="presets-wrap">
          <button className="scen-btn" aria-expanded={presetsOpen} aria-haspopup="true" onClick={() => setPresetsOpen(o => !o)}>
            Scenario · {activePreset?.label ?? 'Custom'} <span aria-hidden>{presetsOpen ? '▾' : '▴'}</span></button>
          {presetsOpen && (
            <nav className="presets" aria-label="Scenario presets">
              {presets.map(p => (
                <button key={p.id} className={activePreset?.id === p.id ? 'on' : ''} data-delta={p.closed.length ? deltaLabel(p.id) : ''}
                  onClick={() => { applyPreset(p); setPresetsOpen(false) }}>{p.label}</button>
              ))}
            </nav>
          )}
        </div>
        {closeMode && <div className="mode-banner">Click any road to close or reopen it · river crossings toggle a whole carriageway
          <button onClick={() => setCloseMode(false)}>Done</button></div>}
        <MapLegend open={legendOpen} setOpen={setLegendOpen} />
        {busy.eval && <div className="busy">FalkorDB recomputing…</div>}
        {applying !== null && !busy.eval && <div className="busy" data-testid="applying">Applying candidate on the server…</div>}
        {error && <div className="err">{error}</div>}
        {compact && rightVisible && <div className="scrim" onClick={closeRight} />}
      </main>

      {rightVisible && (
        <aside className="right" aria-label={rightTitle}>
          <div className="right-head">
            <b>{rightTitle}</b>
            <button className="ghost" onClick={closeRight} aria-label="Close panel">Close</button>
          </div>
          <div className="right-scroll">
            {view === 'compare' ? (
              <ComparePanel data={compare} err={compareErr} busy={false} zoneName={zoneName}
                activeId={activePreset?.id ?? null} setHovered={setHovered}
                onPick={id => { const p = presets.find(x => x.id === id); if (p) applyPreset(p) }}
                onOpenRecovery={id => { const p = presets.find(x => x.id === id); if (p) applyPreset(p); openDrawer('recovery') }} />
            ) : view === 'history' ? (
              <MemoryPanel scenario={scenario} cwLabel={cwLabel} refreshKey={1}
                ruleLabel={r => meta.rules[r as RuleId]?.label ?? `rule ${r}`} onOpenCase={setCaseId} />
            ) : (
              <>
                <ImpactPanel meta={meta} evaluation={evaluation} zoneName={zoneName} cwLabel={cwLabel}
                  selectedZone={selectedZone} setSelectedZone={setSelectedZone} />
                <Accordion id="recovery" title="Recovery ranking" hint="best repair first" open={activeSec === 'recovery'} onToggle={() => pickSec('recovery')}>
                  <RecoveryPanel ivs={ivs} busy={busy.iv} zoneName={zoneName} cwLabel={cwLabel} setHovered={setHovered}
                    apply={applyCandidate} scenario={scenario} />
                </Accordion>
                <Accordion id="vuln" title="Road sensitivity" hint="advanced · test assumptions on a road" open={activeSec === 'vuln'} onToggle={() => pickSec('vuln')}>
                  <VulnerabilityPanel vuln={vuln} busy={vulnBusy} error={vulnErr} meta={meta} zoneName={zoneName}
                    scenario={scenario} push={push} setHighlight={setHighlight} />
                </Accordion>
                <Accordion id="evidence" title="Evidence and provenance" hint="FalkorDB operations behind every number" open={activeSec === 'evidence'} onToggle={() => pickSec('evidence')}>
                  <EvidencePanel evaluation={evaluation} ivs={ivs} vuln={vuln} />
                </Accordion>
                <button className="linklike jump" onClick={() => go('compare')}>Compare preset closures →</button>
              </>
            )}
          </div>
        </aside>
      )}
      {caseId && <CaseFileView id={caseId} scenario={scenario} onClose={() => setCaseId(null)} />}
      {showAssumptions && <AssumptionsModal meta={meta} onClose={() => setShowAssumptions(false)} />}
    </div>
  )
}

function Accordion({ id, title, hint, open, onToggle, children }: { id: string; title: string; hint: string; open: boolean; onToggle: () => void; children: React.ReactNode }) {
  return (
    <section className={`acc ${open ? 'open' : ''}`} data-section={id}>
      <button className="acc-h" aria-expanded={open} onClick={onToggle}><span>{title}</span><small>{hint}</small><i aria-hidden>{open ? '▾' : '▸'}</i></button>
      {open && <div className="acc-body">{children}</div>}
    </section>
  )
}

function MapLegend({ open, setOpen }: { open: boolean; setOpen: (b: boolean) => void }) {
  return (
    <div className={`legend ${open ? 'open' : ''}`}>
      <button className="legend-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>Legend {open ? '▾' : '▸'}</button>
      {open && <div className="legend-grid">
        <div><i className="sw crossing" />River crossing</div>
        <div><i className="sw closed" />Closed</div>
        <div><i className="sw base" />Normal route</div>
        <div><i className="sw cur" />Detour route</div>
        <div><i className="sw contra" />Contraflow</div>
        <div><i className="sw hl" />Inspected road / zones</div>
        <div><i className="dot help" />Assistance point</div>
        <div><i className="dot z0 dim" />Unaffected area</div>
        <div className="ramp"><i className="dot z0" /><span>Farther by</span><i className="grad" /><span>more</span></div>
      </div>}
    </div>
  )
}
