import { useEffect, useMemo, useRef } from 'react'
import maplibregl, { type GeoJSONSource, type Map as MLMap } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { Candidate, Evaluation, Layers, Meta, RuleId, Scenario } from '../api'
import { km, signedKm } from '../api'

// Map renders only locally served OSM geometry: no tile server, no glyph server.
const C = {
  land: '#f5f5f2', water: '#c4d9e6', waterEdge: '#a9c6d8',
  major: '#6d7078', minor: '#c9c9c3', minorLow: '#deded9',
  crossing: '#c9821a', closed: '#c0392b', contra: '#6b4fa3',
  routeBase: '#6f6a60', routeCur: '#2858d4', glow: '#2f9e6e',
}
const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }
const MAJOR = ['motorway', 'trunk', 'primary', 'motorway_link', 'trunk_link', 'primary_link']
const MID = ['secondary', 'tertiary', 'secondary_link', 'tertiary_link']

interface Props {
  meta: Meta
  layers: Layers
  evaluation: Evaluation | null
  rule: RuleId
  selectedZone: string | null
  hovered: Candidate | null
  closeMode: boolean
  reversed: string[]
  preview: Scenario | null
  current: Scenario
  /** Roads / zones the user is inspecting (vulnerability candidate, investigation result). Display only. */
  highlight: { eids: string[]; zones: string[]; labels?: { id: string; text: string }[] } | null
  onRoadClick: (eid: string, carriageway: string) => void
  onZoneClick: (zone: string) => void
}

export default function MapView(p: Props) {
  const el = useRef<HTMLDivElement>(null)
  const map = useRef<MLMap | null>(null)
  const ready = useRef(false)
  const markers = useRef<maplibregl.Marker[]>([])
  const cb = useRef(p)
  cb.current = p

  const roadById = useMemo(() => {
    const m = new Map<string, GeoJSON.Feature<GeoJSON.LineString>>()
    p.layers.roads.features.forEach(f => m.set(f.properties.eid, f))
    return m
  }, [p.layers])

  const lines = (eids: string[], props: Record<string, unknown> = {}): GeoJSON.FeatureCollection => ({
    type: 'FeatureCollection',
    features: eids.flatMap(e => {
      const rev = e.startsWith('rev:')
      const f = roadById.get(rev ? e.slice(4) : e)
      if (!f) return []
      const coords = rev ? [...f.geometry.coordinates].reverse() : f.geometry.coordinates
      return [{ type: 'Feature', geometry: { type: 'LineString', coordinates: coords }, properties: { ...f.properties, ...props } }]
    }),
  })

  // ---------------------------------------------------------------- init
  useEffect(() => {
    if (!el.current || map.current) return
    const [w, s, e, n] = p.meta.source.bbox_w_s_e_n
    const m = new maplibregl.Map({
      container: el.current,
      style: { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': C.land } }] },
      bounds: [[w - 0.002, s + 0.004], [e - 0.002, n - 0.002]], fitBoundsOptions: { padding: { top: 56, bottom: 10, left: 10, right: 10 } }, attributionControl: false, dragRotate: false,
      maxBounds: [[w - 0.04, s - 0.03], [e + 0.04, n + 0.03]],
    })
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')
    m.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left')
    m.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap contributors</a>, <a href="https://opendatacommons.org/licenses/odbl/" target="_blank" rel="noopener">ODbL</a>' }))
    map.current = m
    m.on('load', () => {
      const src = (id: string, data: GeoJSON.FeatureCollection | GeoJSON.Feature) => m.addSource(id, { type: 'geojson', data })
      src('river', p.layers.river)
      src('roads', p.layers.roads)
      ;['preview', 'hl', 'hl-pt', 'closed', 'contra', 'glow', 'route-base', 'route-cur', 'zones', 'help'].forEach(id => src(id, EMPTY))

      const w = (z10: number, z16: number) => ['interpolate', ['exponential', 1.6], ['zoom'], 11, z10, 16, z16] as unknown as number
      m.addLayer({ id: 'river-edge', type: 'line', source: 'river', paint: { 'line-color': C.waterEdge, 'line-width': w(9, 70), 'line-opacity': 0.5 }, layout: { 'line-cap': 'round', 'line-join': 'round' } })
      m.addLayer({ id: 'river', type: 'line', source: 'river', paint: { 'line-color': C.water, 'line-width': w(7, 64) }, layout: { 'line-cap': 'round', 'line-join': 'round' } })
      m.addLayer({ id: 'roads-minor', type: 'line', source: 'roads', filter: ['!', ['in', ['get', 'highway'], ['literal', [...MAJOR, ...MID]]]],
        paint: { 'line-color': ['interpolate', ['linear'], ['zoom'], 12, C.minorLow, 15, C.minor], 'line-width': w(0.5, 3) } })
      m.addLayer({ id: 'roads-mid', type: 'line', source: 'roads', filter: ['in', ['get', 'highway'], ['literal', MID]],
        paint: { 'line-color': '#a4a5a8', 'line-width': w(1.1, 6) }, layout: { 'line-cap': 'round' } })
      m.addLayer({ id: 'roads-major', type: 'line', source: 'roads', filter: ['in', ['get', 'highway'], ['literal', MAJOR]],
        paint: { 'line-color': C.major, 'line-width': w(1.8, 9) }, layout: { 'line-cap': 'round' } })
      m.addLayer({ id: 'crossings', type: 'line', source: 'roads', filter: ['!=', ['get', 'crossing'], ''],
        paint: { 'line-color': C.crossing, 'line-width': w(3.5, 14) }, layout: { 'line-cap': 'round' } })
      m.addLayer({ id: 'glow', type: 'line', source: 'glow', paint: { 'line-color': C.glow, 'line-width': w(11, 36), 'line-opacity': 0.5, 'line-blur': 2 } })
      m.addLayer({ id: 'route-casing', type: 'line', source: 'route-cur', paint: { 'line-color': '#ffffff', 'line-width': w(5.5, 14), 'line-opacity': 0.95 }, layout: { 'line-cap': 'round', 'line-join': 'round' } })
      m.addLayer({ id: 'route-base', type: 'line', source: 'route-base', paint: { 'line-color': '#2b2823', 'line-width': w(2.8, 9), 'line-dasharray': [1.2, 1.2] }, layout: { 'line-cap': 'butt' } })
      m.addLayer({ id: 'route-cur', type: 'line', source: 'route-cur', paint: { 'line-color': C.routeCur, 'line-width': w(3, 9), 'line-opacity': 0.9 }, layout: { 'line-cap': 'round', 'line-join': 'round' } })
      m.addLayer({ id: 'contra', type: 'line', source: 'contra', paint: { 'line-color': C.contra, 'line-width': w(3.5, 12) } })
      m.addLayer({ id: 'closed', type: 'line', source: 'closed', paint: { 'line-color': C.closed, 'line-width': w(4, 14) }, layout: { 'line-cap': 'round' } })
      m.addLayer({ id: 'closed-dash', type: 'line', source: 'closed', paint: { 'line-color': '#fff', 'line-width': w(1.4, 4), 'line-dasharray': [1.5, 1.5] } })
      m.addLayer({ id: 'preview', type: 'line', source: 'preview', paint: { 'line-color': '#b3261e', 'line-width': w(9, 28), 'line-opacity': 0.28, 'line-blur': 1 } })
      m.addLayer({ id: 'hl', type: 'line', source: 'hl', paint: { 'line-color': '#7a2fd0', 'line-width': w(8, 26), 'line-opacity': 0.55 }, layout: { 'line-cap': 'round' } })
      m.addLayer({ id: 'hl-pt', type: 'circle', source: 'hl-pt', paint: { 'circle-radius': 13, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': '#7a2fd0', 'circle-stroke-width': 3 } })
      m.addLayer({ id: 'help', type: 'circle', source: 'help', paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, ['case', ['get', 'active'], 4.5, 2], 16, ['case', ['get', 'active'], 10, 4]] as unknown as number,
        'circle-color': ['case', ['get', 'active'], '#ffffff', '#9a9488'],
        'circle-stroke-color': ['case', ['get', 'active'], '#0f766e', '#ffffff'],
        'circle-stroke-width': ['case', ['get', 'active'], 3, 1] } })
      m.addLayer({ id: 'zones', type: 'circle', source: 'zones', paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, ['case', ['get', 'selected'], 8, ['get', 'affected'], 6.5, 4.5], 16, ['case', ['get', 'selected'], 15, ['get', 'affected'], 12.5, 10]] as unknown as number,
        'circle-opacity': ['case', ['get', 'dim'], 0.4, 1] as unknown as number,
        'circle-stroke-opacity': ['case', ['get', 'dim'], 0.4, 1] as unknown as number,
        'circle-color': ['case',
          ['==', ['get', 'status'], 'cut_off'], '#111111',
          ['<', ['get', 'delta'], -0.5], '#2a9d8f',
          ['<=', ['get', 'delta'], 0.5], '#f8f5ee',
          ['interpolate', ['linear'], ['get', 'delta'], 1, '#f4c27f', 800, '#e9873f', 2000, '#cf4b28', 3500, '#8c1c13']],
        'circle-stroke-color': ['case', ['get', 'selected'], '#1f5fbf', ['get', 'glow'], C.glow, ['get', 'hl'], '#7a2fd0', '#3d3a35'],
        'circle-stroke-width': ['case', ['get', 'selected'], 3, ['get', 'glow'], 3, ['get', 'hl'], 3, 1.2] } })

      const roadLayers = ['crossings', 'roads-major', 'roads-mid', 'roads-minor', 'closed', 'contra']
      const tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: 'cf-tip', offset: 10 })
      roadLayers.concat(['zones', 'help']).forEach(l => {
        m.on('mouseenter', l, () => (m.getCanvas().style.cursor = 'pointer'))
        m.on('mouseleave', l, () => { m.getCanvas().style.cursor = cb.current.closeMode ? 'crosshair' : ''; tip.remove() })
      })
      m.on('mousemove', ev => {
        const f = m.queryRenderedFeatures([[ev.point.x - 4, ev.point.y - 4], [ev.point.x + 4, ev.point.y + 4]],
          { layers: ['zones', 'help', ...roadLayers] })[0]
        if (!f) return tip.remove()
        const pr = f.properties as Record<string, string | number | boolean>
        let html = ''
        if (f.layer.id === 'zones') {
          html = `<b>${pr.name}</b><br/>${pr.label}`
        } else if (f.layer.id === 'help') {
          html = `<b>${pr.name}</b><br/>Assistance point · rule ${pr.rules}${pr.active ? '' : ' (inactive)'}`
        } else {
          html = `<b>${pr.name || 'Unnamed road'}</b><br/>${pr.highway} · ${Math.round(Number(pr.len))} m · ${pr.oneway ? 'one-way' : 'two-way'}`
            + (pr.carriageway ? `<br/>River crossing · ${String(pr.carriageway).split(':')[1]}` : '')
            + `<br/><span class="mono">${pr.eid}</span>`
        }
        tip.setLngLat(ev.lngLat).setHTML(html).addTo(m)
      })
      m.on('click', ev => {
        const box: [maplibregl.PointLike, maplibregl.PointLike] = [[ev.point.x - 5, ev.point.y - 5], [ev.point.x + 5, ev.point.y + 5]]
        const z = m.queryRenderedFeatures(box, { layers: ['zones'] })[0]
        if (z) return cb.current.onZoneClick(String(z.properties!.id))
        if (!cb.current.closeMode) return
        const r = m.queryRenderedFeatures(box, { layers: roadLayers })[0]
        if (r) cb.current.onRoadClick(String(r.properties!.eid), String(r.properties!.carriageway || ''))
      })
      ready.current = true
      refresh()
    })
    return () => { m.remove(); map.current = null; ready.current = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ---------------------------------------------------------------- data refresh
  const refresh = () => {
    const m = map.current
    if (!m || !ready.current) return
    const { evaluation: ev, selectedZone, hovered, meta, rule, reversed, preview, current, highlight } = cb.current
    const set = (id: string, d: GeoJSON.FeatureCollection) => (m.getSource(id) as GeoJSONSource).setData(d)
    set('closed', lines(ev?.closed_eids ?? []))
    set('contra', lines(reversed.map(e => `rev:${e}`)))
    const cwEids = new Map(meta.crossings.flatMap(x => x.carriageways.map(c => [c.id, c.eids] as const)))
    const newCw = (preview?.closed_carriageways ?? []).filter(c => !current.closed_carriageways.includes(c))
    const newSeg = (preview?.closed_segments ?? []).filter(e => !current.closed_segments.includes(e))
    set('preview', lines([...newCw.flatMap(c => cwEids.get(c) ?? []), ...newSeg]))
    set('glow', lines(hovered ? hovered.segments.map(e => hovered.type === 'contraflow' ? `rev:${e}` : e) : []))
    const rows = new Map((ev?.zones ?? []).map(z => [z.zone, z]))
    const glowZones = new Set(hovered ? [...hovered.zones_improved.map(z => z.zone), ...hovered.zones_restored] : [])
    const hlZones = new Set(hovered ? [] : highlight?.zones ?? [])
    const hl = lines(highlight?.eids ?? [])
    set('hl', hl)
    // a ring at the centre of the highlighted roads, so a very short link is still visible
    const pts = hl.features.flatMap(f => (f.geometry as GeoJSON.LineString).coordinates)
    set('hl-pt', { type: 'FeatureCollection', features: pts.length ? [{ type: 'Feature', properties: {}, geometry: { type: 'Point',
      coordinates: [pts.reduce((a, c) => a + c[0], 0) / pts.length, pts.reduce((a, c) => a + c[1], 0) / pts.length] } }] : [] })
    const disrupted = !!ev && (ev.scenario.closed_carriageways.length + ev.scenario.closed_segments.length > 0)
    set('zones', {
      type: 'FeatureCollection', features: meta.zones.map(z => {
        const r = rows.get(z.id)
        const label = !r ? '' : r.status === 'cut_off' ? 'No modeled road access to an assistance point'
          : `${km(r.cur)} to ${r.help[0] ?? 'nearest assistance point'}` + (r.delta && Math.abs(r.delta) > 0.5 ? ` (${signedKm(r.delta, 2)})` : '')
        return { type: 'Feature', geometry: { type: 'Point', coordinates: [z.lon, z.lat] },
          properties: { id: z.id, name: z.name, delta: r?.delta ?? 0, status: r?.status ?? 'same', label,
            selected: z.id === selectedZone, glow: glowZones.has(z.id), hl: hlZones.has(z.id),
            affected: (r?.status ?? 'same') !== 'same', dim: disrupted && (r?.status ?? 'same') === 'same' && z.id !== selectedZone && !hlZones.has(z.id) && !glowZones.has(z.id) } }
      }),
    })
    set('help', {
      type: 'FeatureCollection', features: meta.help_points.map(h => ({
        type: 'Feature' as const, geometry: { type: 'Point' as const, coordinates: [h.lon, h.lat] },
        properties: { name: h.name, rules: h.rules.join(', '), active: h.rules.includes(rule) },
      })).sort((a, b) => Number(a.properties.active) - Number(b.properties.active)),
    })
    const route = selectedZone && ev ? ev.routes[selectedZone] : null
    set('route-base', lines(route ? route.baseline : []))
    set('route-cur', lines(route?.current ?? []))
    m.getCanvas().style.cursor = cb.current.closeMode ? 'crosshair' : ''
  }

  useEffect(refresh)

  // crossing labels as HTML markers (no glyph server needed)
  useEffect(() => {
    const m = map.current
    if (!m) return
    markers.current.forEach(mk => mk.remove())
    markers.current = p.meta.crossings.map(x => {
      const div = document.createElement('div')
      div.className = 'cf-xlabel'
      const closed = x.carriageways.filter(c => p.evaluation?.scenario.closed_carriageways.includes(c.id)).length
      div.innerHTML = `<span>${x.name}</span>${closed ? `<em>${closed === x.carriageways.length ? 'closed' : `${closed}/${x.carriageways.length} closed`}</em>` : ''}`
      return new maplibregl.Marker({ element: div, anchor: 'left', offset: [14, 0] }).setLngLat([x.lon, x.lat]).addTo(m)
    })
  }, [p.meta, p.evaluation])

  // Names + deltas for the most affected neighbourhoods (and the selected one). Labels are placed greedily, in order of
  // impact, at the first of several positions that overlaps no earlier label or crossing label; if none fits, the
  // label is omitted (the full list is always in the side panel).
  const zmarkers = useRef<maplibregl.Marker[]>([])
  const placeLabels = () => {
    const m = map.current
    if (!m) return
    zmarkers.current.forEach(mk => mk.remove())
    zmarkers.current = []
    const { evaluation: ev, selectedZone: sel, meta, highlight } = cb.current
    if (!ev) return
    const byId = new Map(meta.zones.map(z => [z.id, z]))
    const rows = ev.zones.filter(z => z.status !== 'same' && Math.abs(z.delta ?? 1e9) > 0.5)
      .sort((a, b) => Math.abs(b.delta ?? 1e9) - Math.abs(a.delta ?? 1e9)).slice(0, 6)
    const picked = ev.zones.find(z => z.zone === sel && !rows.some(x => x.zone === z.zone))
    type Row = { zone: string; status: string; delta?: number | null; text?: string }
    // while an investigation result is hovered, label ITS neighbourhoods (hypothesis), not the applied scenario's
    const order: Row[] = highlight?.labels?.length
      ? highlight.labels.map(l => ({ zone: l.id, status: l.text === 'cut off' ? 'cut_off' : 'longer', delta: 1, text: l.text }))
      : [...(picked ? [picked] : []), ...rows]
    type Box = { x0: number; y0: number; x1: number; y1: number }
    const hit = (a: Box, b: Box) => a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0
    const boxes: Box[] = meta.crossings.map(x => {
      const pt = m.project([x.lon, x.lat])
      return { x0: pt.x + 12, y0: pt.y - 10, x1: pt.x + 16 + x.name.length * 6.6, y1: pt.y + 10 }
    })
    const cvs = m.getCanvas()
    const W = cvs.clientWidth, H = cvs.clientHeight
    for (const r of order) {
      const z = byId.get(r.zone)
      if (!z) continue
      const txt = r.text ?? (r.status === 'cut_off' ? 'cut off' : r.delta && Math.abs(r.delta) > 0.5 ? signedKm(r.delta, 2) : '')
      const w = z.name.length * 6.4 + txt.length * 7 + 22, h = 20
      const pt = m.project([z.lon, z.lat])
      const cands: [number, number][] = [[13, -h / 2], [-13 - w, -h / 2], [-w / 2, -h - 11], [-w / 2, 11], [13, -h - 8], [13, 8], [-13 - w, -h - 8], [-13 - w, 8]]
      const spot = cands.find(([dx, dy]) => {
        const b = { x0: pt.x + dx, y0: pt.y + dy, x1: pt.x + dx + w, y1: pt.y + dy + h }
        return b.x0 > 4 && b.y0 > 44 && b.x1 < W - 50 && b.y1 < H - 24 && !boxes.some(o => hit(o, b))
      })
      if (!spot) continue
      boxes.push({ x0: pt.x + spot[0], y0: pt.y + spot[1], x1: pt.x + spot[0] + w, y1: pt.y + spot[1] + h })
      const div = document.createElement('div')
      div.className = `cf-zlabel ${r.status === 'cut_off' ? 'cut' : (r.delta ?? 0) < 0 ? 'good' : ''}`
      div.innerHTML = `<b>${z.name}</b><span>${txt}</span>`
      zmarkers.current.push(new maplibregl.Marker({ element: div, anchor: 'top-left', offset: spot }).setLngLat([z.lon, z.lat]).addTo(m))
    }
  }
  useEffect(() => {
    placeLabels()
    const m = map.current
    if (!m) return
    m.on('zoomend', placeLabels)
    m.on('resize', placeLabels)
    return () => { m.off('zoomend', placeLabels); m.off('resize', placeLabels) }
  }, [p.evaluation, p.selectedZone, p.meta, p.highlight])

  // the side panels can be collapsed or become drawers: keep the canvas sized to its container
  useEffect(() => {
    if (!el.current) return
    const ro = new ResizeObserver(() => map.current?.resize())
    ro.observe(el.current)
    return () => ro.disconnect()
  }, [])

  return <div ref={el} className="map" />
}
