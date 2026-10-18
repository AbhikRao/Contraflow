import { useMemo, useState } from 'react'
import type { ConstraintKind, Evaluation, Layers, Meta, Scenario } from '../api'

interface Props {
  meta: Meta; scenario: Scenario; evaluation: Evaluation | null; layers: Layers
  closeMode: boolean; setCloseMode: (b: boolean) => void
  toggleCarriageway: (id: string) => void; push: (s: Scenario) => void
  undo: () => void; reset: () => void; canUndo: boolean; cwLabel: Map<string, string>
}

const ARROW: Record<string, string> = { northbound: '↑ Northbound', southbound: '↓ Southbound', 'two-way': '↕ Two-way' }

export default function ScenarioPanel(p: Props) {
  const { meta, scenario, evaluation } = p
  const roads = useMemo(() => new Map(p.layers.roads.features.map(f => [f.properties.eid, f.properties])), [p.layers])

  // How many baseline zone routes use each carriageway (graph-derived, from FalkorDB route paths).
  const usage = useMemo(() => {
    const u = new Map<string, number>()
    if (!evaluation) return u
    Object.values(evaluation.routes).forEach(r => {
      new Set(r.baseline.map(e => roads.get(e)?.carriageway).filter(Boolean) as string[])
        .forEach(c => u.set(c, (u.get(c) ?? 0) + 1))
    })
    return u
  }, [evaluation, roads])

  const [kind, setKind] = useState<ConstraintKind>('reserve_emergency')
  const [target, setTarget] = useState(meta.crossings[0]?.carriageways[0]?.id ?? '')
  const needsClosed = kind === 'no_reopen' && !scenario.closed_carriageways.includes(target)
  const addConstraint = () => {
    if (needsClosed || scenario.constraints.some(c => c.kind === kind && c.target === target)) return
    p.push({ ...scenario, constraints: [...scenario.constraints, { kind, target }] })
  }

  return (
    <div className="panel scroll">
      <section>
        <h3>River crossings <small>{meta.crossings.length} bridges · {meta.crossings.reduce((n, x) => n + x.carriageways.length, 0)} carriageways</small></h3>
        <p className="hint">Each bridge is modeled as directed carriageways. Close a whole bridge or a single direction.</p>
        {meta.crossings.map(x => {
          const closedN = x.carriageways.filter(c => scenario.closed_carriageways.includes(c.id)).length
          const all = closedN === x.carriageways.length
          return (
            <div key={x.id} className={`xcard ${closedN ? 'is-closed' : ''}`}>
              <div className="xhead">
                <div><b>{x.name}</b><span>{x.roads.join(' · ')}</span></div>
                <button className={all ? 'danger on' : 'danger'} onClick={() => p.push({
                  ...scenario, closed_carriageways: all
                    ? scenario.closed_carriageways.filter(c => !x.carriageways.some(k => k.id === c))
                    : [...new Set([...scenario.closed_carriageways, ...x.carriageways.map(c => c.id)])],
                })}>{all ? 'Reopen bridge' : 'Close bridge'}</button>
              </div>
              {x.carriageways.map(c => {
                const closed = scenario.closed_carriageways.includes(c.id)
                const cons = scenario.constraints.filter(k => k.target === c.id)
                return (
                  <div key={c.id} className="cw">
                    <label className="switch" title={closed && cons.some(k => k.kind === 'no_reopen')
                      ? 'Locked by a “do not reopen” constraint — remove the constraint first' : undefined}>
                      <input type="checkbox" checked={!closed} disabled={closed && cons.some(k => k.kind === 'no_reopen')}
                        onChange={() => p.toggleCarriageway(c.id)} />
                      <span />
                    </label>
                    <div className="cw-body">
                      <div>{ARROW[c.direction] ?? c.direction} <em>{c.oneway ? 'one-way' : 'two-way'}{c.lanes.length ? ` · lanes ${c.lanes.join('/')}` : ''} · {Math.round(c.len)} m</em></div>
                      <div className="use">{usage.get(c.id) ?? 0} baseline zone route{usage.get(c.id) === 1 ? '' : 's'} use this direction
                        {cons.map(k => <span key={k.kind} className="tag">{k.kind.replace('_', ' ')}</span>)}</div>
                    </div>
                  </div>
                )
              })}
            </div>
          )
        })}
      </section>

      <section>
        <h3>Other roads</h3>
        <button className={`wide ${p.closeMode ? 'on' : ''}`} onClick={() => p.setCloseMode(!p.closeMode)}>
          {p.closeMode ? 'Finish selecting roads' : 'Close a road on the map'}
        </button>
        {scenario.closed_segments.length === 0 && <p className="hint">No individual road segments closed.</p>}
        <ul className="chips">
          {scenario.closed_segments.map(e => (
            <li key={e}><span>{roads.get(e)?.name || 'Unnamed road'} <em>{Math.round(roads.get(e)?.len ?? 0)} m</em></span>
              <button onClick={() => p.push({ ...scenario, closed_segments: scenario.closed_segments.filter(x => x !== e) })}>Reopen</button></li>
          ))}
        </ul>
        {scenario.reversed_segments.length > 0 && <>
          <h4>Contraflow in effect</h4>
          <ul className="chips contra">
            {scenario.reversed_segments.map(e => (
              <li key={e}><span>{roads.get(e)?.name || 'Unnamed road'} reversed <em>{Math.round(roads.get(e)?.len ?? 0)} m</em></span>
                <button onClick={() => p.push({ ...scenario, reversed_segments: scenario.reversed_segments.filter(x => x !== e) })}>Undo</button></li>
            ))}
          </ul>
        </>}
      </section>

      <section>
        <h3>Planning constraints</h3>
        <ul className="chips">
          {scenario.constraints.map(c => (
            <li key={c.kind + c.target}><span><b>{c.kind.replace('_', ' ')}</b> {p.cwLabel.get(c.target)}</span>
              <button onClick={() => p.push({ ...scenario, constraints: scenario.constraints.filter(k => k !== c) })}>Remove</button></li>
          ))}
        </ul>
        <div className="addc">
          <select value={kind} onChange={e => setKind(e.target.value as ConstraintKind)} aria-label="Constraint type">
            <option value="reserve_emergency">Reserve for emergency inbound</option>
            <option value="keep_direction">Keep direction (no contraflow)</option>
            <option value="no_reopen">Do not reopen</option>
          </select>
          <select value={target} onChange={e => setTarget(e.target.value)} aria-label="Carriageway">
            {meta.crossings.flatMap(x => x.carriageways.map(c => <option key={c.id} value={c.id}>{x.name} · {c.direction}</option>))}
          </select>
          <button onClick={addConstraint} disabled={needsClosed}>Add</button>
        </div>
        <p className="hint">{meta.constraint_kinds[kind]}{needsClosed ? ' Applies only to a carriageway that is currently closed.' : ''}</p>
      </section>

      <footer className="history">
        <button onClick={p.undo} disabled={!p.canUndo}>Undo</button>
        <button onClick={p.reset}>Reset to baseline</button>
      </footer>
    </div>
  )
}
