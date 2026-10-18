import type { Meta } from '../api'

export default function AssumptionsModal({ meta, onClose }: { meta: Meta; onClose: () => void }) {
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" role="dialog" aria-label="Assumptions and limitations" onClick={e => e.stopPropagation()}>
        <button className="x" onClick={onClose} aria-label="Close">×</button>
        <h2>How Contraflow works</h2>
        <p className="lede">Contraflow is a tabletop planning sandbox. It is not an emergency-response or evacuation system,
          and it does not issue operational advice.</p>
        <h3>The question</h3>
        <p>When a bridge closes, how much farther must each of {meta.zones.length} neighbourhoods travel to reach the nearest assistance
          point, and which repair helps most? Distances are modeled shortest road distances, not travel times.</p>
        <h3>The workflow</h3>
        <p>Report (unverified) → Investigation → Impact → Alternatives → Recommendation → <b>Human decision</b>.
          The planning agent calls FalkorDB graph tools; every figure comes from a tool, not from the model.
          It proposes changes. It never applies one: nothing changes until you approve it.</p>
        <p>Open <b>Compare</b> for the preset closures side by side, <b>History</b> for graph memory and case files,
          and <b>View analysis</b> on the result card for the full zone list, repair ranking, road sensitivity and provenance.</p>
        <h2 style={{ marginTop: 22 }}>What this model is, and is not</h2>
        <h3>Objective</h3>
        <p>{meta.objective}</p>
        <h3>Real data</h3>
        <ul>
          <li>Road network: {meta.network.junctions.toLocaleString()} junctions, {meta.network.segments.toLocaleString()} directed
            segments ({meta.network.oneway_segments} one-way), OpenStreetMap drive network fetched {String(meta.source.fetched_utc)} for
            bbox {meta.source.bbox_w_s_e_n.join(', ')}.</li>
          <li>River crossings: drive segments whose geometry intersects the OSM “Adyar River” line, grouped into
            {' '}{meta.crossings.length} bridges / {meta.crossings.reduce((n, x) => n + x.carriageways.length, 0)} directed carriageways.</li>
          <li>Neighbourhoods: {meta.zones.length} named OSM places, each snapped to its nearest junction in the largest strongly
            connected road component. Excluded: {meta.excluded_zones.map(z => `${z.name} (${z.reason})`).join('; ')}</li>
          <li>Assistance points: OSM hospitals selected by one of two declared rules —
            <b> {meta.rules.B.label}</b>: {meta.rules.B.definition} <b>{meta.rules.A.label}</b>: {meta.rules.A.definition}</li>
        </ul>
        <h3>Assumptions</h3>
        <ul>
          <li>Closures, reopenings, reservations and contraflow are scenario inputs chosen by the user — not observed events.</li>
          <li>Field reports typed into the agent are user-entered (synthetic in the demo), not verified sources.</li>
          <li>Distances are shortest directed road distances along OSM geometry. One-way tags are respected.</li>
          <li>Contraflow reverses a one-way segment (it does not add lanes or capacity).</li>
        </ul>
        <h3>Not modeled</h3>
        <ul>
          <li>Travel time, congestion, flood depth, road capacity, vehicle type, or hospital capacity and services.</li>
          <li>People affected or served; evacuation timing or safety.</li>
          <li>A zero change for a crossing means only that no anchor’s shortest route to its nearest assistance point uses it — it
            may still be vital for other trips, directions or destinations.</li>
        </ul>
        <p className="attrib">{meta.source.attribution}</p>
      </div>
    </div>
  )
}
