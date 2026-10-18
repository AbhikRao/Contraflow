import { useState } from 'react'
import type { Evaluation, Interventions, LedgerSummary, Vulnerability } from '../api'

export default function EvidencePanel({ evaluation, ivs, vuln }: {
  evaluation: Evaluation | null; ivs: Interventions | null; vuln: Vulnerability | null
}) {
  const [which, setWhich] = useState<'eval' | 'iv' | 'vuln'>('eval')
  const led: LedgerSummary | undefined = which === 'eval' ? evaluation?.ledger : which === 'iv' ? ivs?.ledger : vuln?.ledger
  return (
    <div className="panel scroll evidence">
      <div className="seg small">
        <button className={which === 'eval' ? 'on' : ''} onClick={() => setWhich('eval')}>Scenario evaluation</button>
        <button className={which === 'iv' ? 'on' : ''} onClick={() => setWhich('iv')} disabled={!ivs}>Recovery ranking</button>
        <button className={which === 'vuln' ? 'on' : ''} onClick={() => setWhich('vuln')} disabled={!vuln}>Access sensitivity</button>
      </div>
      {!led ? <p className="hint">Nothing evaluated yet.</p> : <>
        <p className="hint">Every number on screen comes from these FalkorDB operations. Scenario changes run on
          <code> GRAPH.COPY</code> clones (<code>cf_sc_*</code>) that are deleted afterwards; the base graph is never mutated.</p>
        <div className="ops">
          {Object.entries(led.by_op).map(([op, o]) => (
            <div key={op}><code>{op}</code><span>{o.count}×</span><span>{o.ms.toFixed(1)} ms</span></div>
          ))}
          <div className="tot"><b>Total</b><span>{led.queries}</span><span>{led.total_ms.toFixed(1)} ms</span></div>
        </div>
        <h4>Query log</h4>
        <ol className="qlog">
          {led.entries.slice(0, 60).map((e, i) => (
            <li key={i}>
              <div className="qmeta"><code>{e.op}</code><span>{e.graph}</span><span>{e.ms.toFixed(1)} ms · {e.rows} rows</span></div>
              <pre>{e.cypher}</pre>
              {Object.keys(e.params).length > 0 && <pre className="params">{JSON.stringify(e.params)}</pre>}
            </li>
          ))}
        </ol>
        {led.entries.length > 60 && <p className="hint">+{led.entries.length - 60} more operations</p>}
      </>}
    </div>
  )
}
