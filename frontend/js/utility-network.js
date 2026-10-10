/* CivicMind — utility network layers, asset panel, tracing and Copilot on the officer map.
   Mounted by map.js for officers/admins only (the API refuses everyone else).
   All server text goes through CM.esc; actions use data-* attributes. Nothing here is
   inferred in the browser: connections, traces and candidates come from the API. */
const UtilityLayer = (() => {
  let map = null, meta = null, group = null, highlight = null, features = {}, selected = null, loadTimer = null, loadSeq = 0;
  const enabled = new Set();
  const $ = (id) => document.getElementById(id);
  const esc = (s) => CM.esc(s);
  const colorOf = (ut) => ((meta && meta.utility_types.find(t => t.key === ut)) || {}).color || '#5B6B82';
  const PROV = { verified_as_built: 'Verified as-built', official_unverified: 'Official, not field-verified',
    planned_design: 'Planned / design', approximate_unverified: 'Approximate / unverified', synthetic_demo: 'Synthetic demo' };
  const provBadge = (p, synthetic) => `<span class="badge ${p === 'verified_as_built' ? 'low' : p === 'synthetic_demo' ? 'synthetic' : 'medium'}">${esc(PROV[p] || p)}</span>`
    + (synthetic && p !== 'synthetic_demo' ? ' <span class="badge synthetic">Synthetic</span>' : '');
  const verBadge = (v) => `<span class="badge ${v === 'verified' ? 'low' : v === 'disputed' ? 'critical' : 'pending'}">${esc(CM.titleCase(v))}</span>`;
  const assetLink = (id, label) => `<button type="button" class="link-btn" data-asset="${esc(id)}">${esc(label || id)}</button>`;

  /* ---------------- pure renderers (also used by tests) ---------------- */
  function renderDetail(d) {
    const attrs = Object.entries(d.attributes || {});
    const conns = d.connections || [];
    return `
      <div class="row spread wrap-row"><div><div class="eyebrow">${esc(d.utility_label)} · ${esc(CM.titleCase(d.asset_type))}</div>
        <h3 style="margin:4px 0">${esc(d.name)}</h3><div class="tiny muted">${esc(d.asset_id)}</div></div>
        <button type="button" class="btn sm ghost" data-uact="close" aria-label="Close asset panel">✕</button></div>
      <div class="row wrap-row" style="gap:6px;margin:8px 0">${provBadge(d.provenance_class, d.is_synthetic)} ${verBadge(d.verification_status)}
        ${d.criticality ? `<span class="badge ${CM.sevClass(d.criticality)}">${esc(CM.titleCase(d.criticality))} criticality</span>` : ''}</div>
      ${(d.notes || []).map(n => `<p class="tiny muted" style="margin:2px 0">ⓘ ${esc(n)}</p>`).join('')}
      <details open class="util-sec"><summary>Identity &amp; source</summary>
        <div class="kv"><span class="muted small">Status</span><span class="small">${esc(d.operational_status ? CM.titleCase(d.operational_status) : 'Not recorded')}</span></div>
        <div class="kv"><span class="muted small">Operator</span><span class="small">${esc(d.operator_org || 'Not recorded')}</span></div>
        <div class="kv"><span class="muted small">Geometry</span><span class="small">${esc(d.geometry_type)}${d.length_m ? ' · ' + esc(d.length_m) + ' m drawn' : ''}</span></div>
        ${attrs.map(([k, v]) => `<div class="kv"><span class="muted small">${esc(CM.titleCase(k))}</span><span class="small">${esc(v)}</span></div>`).join('')}
        <div class="kv"><span class="muted small">Data source</span><span class="small">${esc(d.data_source ? d.data_source.name : 'Not recorded')}</span></div>
        <div class="kv"><span class="muted small">Last verified</span><span class="small">${esc(d.last_verified_at ? CM.fmtDay(d.last_verified_at) : 'Never')}</span></div>
      </details>
      <details open class="util-sec"><summary>Recorded connections (${conns.length})</summary>
        ${conns.length ? conns.map(n => `<div class="kv"><span class="small"><span class="badge">${esc(n.relation)}</span> ${assetLink(n.asset.asset_id, n.asset.asset_id)}
            <span class="tiny muted">${esc(n.asset.name)}</span></span><span class="tiny">${esc(n.connection.connection_id)} ${verBadge(n.connection.verification_status)}</span></div>`).join('')
          : '<p class="small muted">No connections are recorded. Lines that touch on the map are not treated as connected.</p>'}
        <div class="row wrap-row" style="margin-top:10px;gap:6px">
          <button type="button" class="btn sm" data-uact="trace" data-dir="upstream">Trace upstream</button>
          <button type="button" class="btn sm" data-uact="trace" data-dir="downstream">Trace downstream</button>
          <button type="button" class="btn sm" data-uact="trace" data-dir="both">Both</button>
          ${d.isolation_applicable ? '<button type="button" class="btn sm primary" data-uact="isolation">Find isolation candidates</button>' : ''}
        </div>
        <div id="utilTrace" aria-live="polite"></div>
      </details>
      <details open class="util-sec"><summary>Operator contacts</summary>
        ${(d.contacts || []).map(c => `<div class="contact ${c.operational_use ? 'ok' : 'warn'}">
            <strong class="small">${esc(c.organization)}</strong> <span class="tiny muted">${esc(c.role)}</span>
            <div class="small">${esc(c.phone || c.email || 'No channel recorded')}</div>
            <div class="tiny"><span class="badge ${c.operational_use ? 'low' : c.is_synthetic ? 'synthetic' : 'pending'}">${esc(c.label)}</span>
              <span class="muted">Last verified ${esc(c.last_verified_at ? CM.fmtDay(c.last_verified_at) : 'never')} · ${esc(c.source || 'source not recorded')}</span></div></div>`).join('')
          || '<p class="small muted">No operator contact is recorded for this asset.</p>'}
      </details>
      <details class="util-sec" data-uact-open="history"><summary>History</summary><div id="utilHistory"><p class="small muted">Loading…</p></div></details>
      <details open class="util-sec"><summary>Ask the Utility Copilot</summary>
        <form class="row" data-uform="copilot" style="gap:6px">
          <label class="sr-only" for="utilQ">Question about this asset</label>
          <input id="utilQ" maxlength="300" placeholder="e.g. Which valves could isolate this pipe?" style="flex:1"/>
          <button class="btn sm" type="submit">Ask</button></form>
        <div class="pill-list" style="margin-top:6px">${['Where does this connect?', 'What is the upstream source?', 'Which control devices are candidates?', 'Who is the operator?', 'What is unverified?']
          .map(q => `<button type="button" class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>
        <div id="utilAnswer" aria-live="polite"></div>
      </details>`;
  }

  function renderTrace(t, kind) {
    const cands = kind === 'isolation' ? [...t.upstream_candidates.map(c => ({ ...c, side: 'upstream' })), ...t.downstream_candidates.map(c => ({ ...c, side: 'downstream' }))]
                                       : t.candidate_isolation_assets;
    const sources = kind === 'isolation' ? t.sources : t.possible_sources;
    return `<div class="trace-box">
      ${kind !== 'isolation' ? `<div class="small"><strong>${esc(CM.titleCase(t.direction))} trace</strong> · ${esc(t.visited_assets.length)} asset(s), ${esc(t.edges.length)} recorded connection(s)</div>` : '<div class="small"><strong>Isolation candidates</strong></div>'}
      ${sources && sources.length ? sources.map(s => `<p class="small" style="margin:6px 0 0">Source: ${assetLink(s.asset_id, s.asset_id)} <span class="tiny muted">${esc(s.name)} · ${esc((s.path || []).join(' → '))}</span></p>`).join('') : ''}
      ${cands && cands.length ? `<div class="small" style="margin-top:8px"><strong>Candidate control devices</strong> <span class="tiny muted">(require operator confirmation)</span></div>
        ${cands.map(c => `<div class="cand ${c.path_verified ? 'ok' : 'warn'}">${assetLink(c.asset_id, c.asset_id)} <span class="tiny muted">${esc(c.name)} · ${esc(c.side)}</span>
          <div class="tiny">${esc((c.path || []).join(' → '))}</div>
          <div class="tiny">${c.path_verified ? '✓ All records on this path are verified' : '⚠ Unverified records: ' + esc((c.unverified_records || []).join(', '))}</div></div>`).join('')}`
        : (kind === 'isolation' || t.direction !== 'downstream' ? '<p class="small muted" style="margin:6px 0 0">No candidate control device found in the recorded network within the trace limit.</p>' : '')}
      ${(t.warnings || []).map(w => `<p class="tiny text-amber" style="margin:4px 0 0">⚠ ${esc(w)}</p>`).join('')}
      ${(t.unknown_links || []).length ? `<p class="tiny muted">Unknown-direction links: ${t.unknown_links.map(u => esc(u.connection_id)).join(', ')}</p>` : ''}
      ${(t.dead_ends || []).length ? `<p class="tiny muted">Record stops at: ${t.dead_ends.map(id => assetLink(id)).join(', ')}</p>` : ''}
      <p class="tiny muted" style="margin:6px 0 0">${esc(t.note || '')}</p></div>`;
  }

  function renderAnswer(r) {
    return `<div class="trace-box"><p class="small" style="white-space:pre-line;margin:0">${esc(r.answer)}</p>
      ${(r.data_gaps || []).length ? `<p class="tiny text-amber" style="margin:6px 0 0">Missing / uncertain: ${esc(r.data_gaps.join(' '))}</p>` : ''}
      <div class="tiny muted" style="margin-top:6px">${r.engine === 'records' ? 'Answer built from records' : 'AI-phrased, grounded on records'} · cites
        ${(r.citations || []).map(c => /^[A-Z]+-/.test(c) ? `<span class="badge">${esc(c)}</span>` : '').join(' ')}</div></div>`;
  }

  function renderHistory(h) {
    return `${h.incidents.length ? h.incidents.map(i => `<div class="kv"><span class="small"><a class="link" href="incident.html?id=${encodeURIComponent(i.id)}">${esc(i.public_id)}</a>
        ${esc(i.title)}</span><span class="tiny">${esc(CM.statusText(i.status))} · ${esc(i.link_status)} · ${esc(i.complaints.length)} report(s)</span></div>`).join('')
      : '<p class="small muted">No incidents linked to this asset.</p>'}
      ${h.maintenance.length ? h.maintenance.map(m => `<div class="kv"><span class="small">${esc(CM.titleCase(m.work_type))} · ${esc(m.outcome || '—')}</span>
        <span class="tiny muted">${esc(CM.fmtDay(m.performed_on))} · ${esc(m.record_id)}</span></div>`).join('')
      : '<p class="small muted">No maintenance records.</p>'}`;
  }

  /* ---------------- map behaviour ---------------- */
  async function mount(leafletMap) {
    map = leafletMap;
    try { meta = await CM.api('/api/utility/meta'); } catch (e) { return; }
    group = L.layerGroup().addTo(map);
    highlight = L.layerGroup().addTo(map);
    const box = $('utilTypes');
    box.innerHTML = meta.utility_types.map(t => `<label><input type="checkbox" data-ut="${esc(t.key)}" ${t.count ? '' : 'disabled'}/>
      <span class="swatch" style="background:${esc(t.color)}"></span> ${esc(t.label)} <strong>${esc(t.count)}</strong></label>`).join('');
    meta.utility_types.filter(t => t.count && ['water', 'sewer', 'electric'].includes(t.key)).forEach(t => {
      enabled.add(t.key); box.querySelector(`[data-ut="${t.key}"]`).checked = true; });
    box.addEventListener('change', (e) => { const ut = e.target.dataset.ut; if (!ut) return;
      e.target.checked ? enabled.add(ut) : enabled.delete(ut); load(); });
    $('utilVerif').addEventListener('change', load);
    $('utilLegend').innerHTML = meta.utility_types.filter(t => t.count).map(t =>
      `<div class="lrow"><span class="swatch" style="background:${esc(t.color)}"></span>${esc(t.label)}</div>`).join('')
      + '<div class="lrow"><span class="swatch dashed"></span>unverified record</div><div class="lrow"><span class="swatch round" style="background:#fff;border:2px solid #0F9D91"></span>control device</div>';
    $('utilPanel').addEventListener('click', onPanelClick);
    $('utilPanel').addEventListener('submit', onPanelSubmit);
    $('utilPanel').addEventListener('toggle', onToggle, true);
    map.on('moveend', () => { clearTimeout(loadTimer); loadTimer = setTimeout(load, 250); });
    await load();
    const q = new URLSearchParams(location.search);
    if (q.get('asset')) select(q.get('asset'), true);
  }

  function bboxParam() {
    const b = map.getBounds(), clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    let [w, s, e, n] = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
    const cx = (w + e) / 2, cy = (s + n) / 2;   // the API caps a bbox at 1° per side
    w = clamp(w, cx - 0.49, cx); e = clamp(e, cx, cx + 0.49); s = clamp(s, cy - 0.49, cy); n = clamp(n, cy, cy + 0.49);
    return [w, s, e, n].map(v => v.toFixed(5)).join(',');
  }

  // Trunk lines are drawn heavier than local pipes/cables; small fittings appear only when zoomed in.
  const TRUNK = new Set(['main', 'trunk_sewer', 'gas_main', 'feeder']);
  const isTrunk = (p) => TRUNK.has(p.asset_type) || p.criticality === 'critical' || p.criticality === 'high';
  const lineWeight = (p, z) => {
    const big = isTrunk(p);
    return z >= 18 ? (big ? 5 : 3) : z >= 17 ? (big ? 3.5 : 2) : z >= 15 ? (big ? 2.6 : 1.4) : (big ? 2 : 1);
  };
  const pointVisible = (p, z) => p.is_source || (p.is_control_device && z >= 17) || z >= 18 || p.asset_id === selected;
  // Draw order: trunks under local lines, and the centre-line networks (sewer/water) under the side ones,
  // so every network stays visible where they run side by side in the same street.
  const UT_ORDER = { sewer: 0, water: 1, gas: 2, storm: 3, telecom: 4, electric: 5 };
  const drawOrder = (f) => (f.geometry.type === 'LineString' ? 0 : 100) + (isTrunk(f.properties) ? 0 : 10)
    + (UT_ORDER[f.properties.utility_type] ?? 6);
  let renderer = null;

  async function load() {
    const seq = ++loadSeq;
    if (!enabled.size) { group.clearLayers(); features = {}; $('utilCount').textContent = '0'; return; }
    const params = new URLSearchParams({ bbox: bboxParam(), utility_types: [...enabled].join(','), limit: '5000' });
    if ($('utilVerif').value) params.set('verification', $('utilVerif').value);
    let fc;
    try { fc = await CM.api('/api/utility/assets?' + params.toString()); }
    catch (e) { $('utilCount').textContent = '!'; return CM.fail(e); }
    if (seq !== loadSeq) return;                       // a newer pan/zoom already asked again
    group.clearLayers(); features = {};
    renderer = renderer || L.canvas({ padding: 0.4, tolerance: 6 });
    const z = map.getZoom();
    let shown = 0;
    fc.features.slice().sort((a, b) => drawOrder(a) - drawOrder(b)).forEach(f => {
      features[f.id] = f;
      const p = f.properties, col = colorOf(p.utility_type), dashed = p.verification_status !== 'verified';
      let layer;
      if (f.geometry.type === 'LineString') {
        layer = L.polyline(f.geometry.coordinates.map(c => [c[1], c[0]]), { renderer, color: col,
          weight: lineWeight(p, z), opacity: .9, lineCap: 'butt', dashArray: dashed ? '7 5' : null });
      } else {
        if (!pointVisible(p, z)) return;
        layer = L.circleMarker([f.geometry.coordinates[1], f.geometry.coordinates[0]], { renderer,
          radius: p.is_source ? 8 : p.is_control_device ? (z >= 18 ? 5 : 3.5) : (z >= 19 ? 4 : 3),
          weight: p.is_control_device ? 2 : 1.2,
          color: p.is_control_device ? '#0F9D91' : '#fff', fillColor: p.is_control_device ? '#fff' : col, fillOpacity: 1,
          dashArray: dashed ? '3 3' : null });
      }
      shown++;
      layer.bindTooltip(`${esc(p.asset_id)} · ${esc(p.name)}`, { sticky: true });
      layer.on('click', () => select(p.asset_id));
      layer.addTo(group);
    });
    $('utilCount').textContent = fc.features.length + (fc.truncated ? '+' : '');
    $('utilCount').title = shown < fc.features.length ? 'Zoom in to see valves, manholes and other fittings' : '';
  }

  async function select(assetId, zoom) {
    selected = assetId;
    const panel = $('utilPanel');
    panel.hidden = false;
    panel.innerHTML = '<p class="small muted">Loading asset…</p>';
    let d;
    try { d = await CM.api('/api/utility/assets/' + encodeURIComponent(assetId)); }
    catch (e) { panel.innerHTML = `<p class="small text-red">${esc(e.message)}</p>`; return; }
    panel.innerHTML = renderDetail(d);
    highlight.clearLayers();
    drawGeometry(d.geometry, '#172B4D', 9, .35);
    if (zoom) {
      const pts = d.geometry.type === 'Point' ? [[d.geometry.coordinates[1], d.geometry.coordinates[0]]]
        : d.geometry.coordinates.map(c => [c[1], c[0]]);
      map.fitBounds(pts, { padding: [80, 80], maxZoom: 18 });
    }
  }

  function drawGeometry(g, color, weight, opacity) {
    if (!g) return;
    if (g.type === 'LineString') L.polyline(g.coordinates.map(c => [c[1], c[0]]), { color, weight, opacity, interactive: false }).addTo(highlight);
    else L.circleMarker([g.coordinates[1], g.coordinates[0]], { radius: 12, color, weight: 3, fill: false, interactive: false }).addTo(highlight);
  }

  function showTraceOnMap(visited, candidates) {
    highlight.clearLayers();
    visited.forEach(v => {
      const f = features[v.asset_id];
      if (f) drawGeometry(f.geometry, '#2D63D8', 9, .3);
      else L.circleMarker([v.lat, v.lng], { radius: 5, color: '#2D63D8', weight: 2, fill: false, interactive: false }).addTo(highlight);
    });
    (candidates || []).forEach(c => L.circleMarker([c.lat, c.lng], { radius: 14, color: c.path_verified ? '#0F9D91' : '#C98210',
      weight: 4, fill: false }).bindTooltip(`Candidate: ${esc(c.asset_id)}`).addTo(highlight));
  }

  async function onPanelClick(e) {
    const t = e.target.closest('[data-uact],[data-asset],[data-q]');
    if (!t) return;
    if (t.dataset.asset) return select(t.dataset.asset, true);
    if (t.dataset.q) { $('utilQ').value = t.dataset.q; return ask(t.dataset.q); }
    const act = t.dataset.uact;
    if (act === 'close') { $('utilPanel').hidden = true; highlight.clearLayers(); selected = null; return; }
    if (act === 'trace' || act === 'isolation') {
      await CM.busy(t, 'Tracing…', async () => {
        try {
          const r = act === 'trace'
            ? await CM.api(`/api/utility/assets/${encodeURIComponent(selected)}/trace?direction=${encodeURIComponent(t.dataset.dir)}`)
            : await CM.api(`/api/utility/assets/${encodeURIComponent(selected)}/isolation`);
          $('utilTrace').innerHTML = renderTrace(r, act);
          if (act === 'trace') showTraceOnMap(r.visited_assets, r.candidate_isolation_assets);
          else showTraceOnMap([], [...r.upstream_candidates, ...r.downstream_candidates]);
        } catch (ex) { CM.fail(ex); }
      });
    }
  }

  async function onToggle(e) {
    if (e.target.dataset && e.target.dataset.uactOpen === 'history' && e.target.open) {
      try { $('utilHistory').innerHTML = renderHistory(await CM.api(`/api/utility/assets/${encodeURIComponent(selected)}/history`)); }
      catch (ex) { $('utilHistory').innerHTML = `<p class="small text-red">${esc(ex.message)}</p>`; }
    }
  }

  function onPanelSubmit(e) {
    if (e.target.dataset.uform !== 'copilot') return;
    e.preventDefault();
    const q = $('utilQ').value.trim();
    if (q.length >= 3) ask(q);
  }

  async function ask(q) {
    $('utilAnswer').innerHTML = '<p class="small muted">Checking the records…</p>';
    try {
      const r = await CM.api(`/api/utility/assets/${encodeURIComponent(selected)}/copilot`, { method: 'POST', body: { question: q } });
      $('utilAnswer').innerHTML = renderAnswer(r);
    } catch (ex) { $('utilAnswer').innerHTML = `<p class="small text-red">${esc(ex.message)}</p>`; }
  }

  return { mount, select, renderDetail, renderTrace, renderAnswer, renderHistory };
})();
