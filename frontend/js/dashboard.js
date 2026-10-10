/* CivicMind — officer operations dashboard */
const user = CM.requireAuth(['officer', 'admin']);
const $ = (id) => document.getElementById(id);
const C = { orange: '#2D63D8', green: '#1E8E57', blue: '#0F9D91', purple: '#6A4BC4', red: '#C93C3C', amber: '#C98210', line: '#DCE4EE' };
const PALETTE = [C.orange, C.blue, C.green, C.purple, C.amber, C.red, '#3AA6A0', '#B0592F', '#7A8B99', '#C77DBA'];
const charts = {};
let queue = [];

document.addEventListener('DOMContentLoaded', () => {
  if (!user) return;
  renderNav('dashboard.html', user);
  $('detectBtn').addEventListener('click', runDetect);
  $('scanBtn').addEventListener('click', runScan);
  $('qFilter').addEventListener('change', renderQueue);
  $('qSearch').addEventListener('input', renderQueue);
  $('queueTable').addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-id]');
    if (tr) location.href = 'incident.html?id=' + encodeURIComponent(tr.dataset.id);
  });
  $('queueTable').addEventListener('keydown', (e) => {
    const tr = e.target.closest('tr[data-id]');
    if (tr && e.key === 'Enter') location.href = 'incident.html?id=' + encodeURIComponent(tr.dataset.id);
  });
  $('reportsTable').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-resolve]');
    if (b) openResolve(Number(b.dataset.resolve));
  });
  loadAll();
  setInterval(() => { if (!document.hidden && !$('resolveModal').innerHTML) loadDashboard(); }, 30000);
});

function loadAll() { loadDashboard(); loadCharts(); loadUtilityFindings(); loadOpsMap(); loadWorkOrders(); loadNetSummary(); }

/* ---------- utility network records by network, with verification share ---------- */
async function loadNetSummary() {
  const box = $('netSummary');
  let m;
  try { m = await CM.api('/api/utility/meta'); } catch (e) { box.innerHTML = emptyState(e.message); return; }
  const types = m.utility_types.filter(t => t.count);
  if (!types.length) { box.innerHTML = emptyState('No utility records yet. Admins can import them.'); return; }
  const max = Math.max(...types.map(t => t.count));
  const total = types.reduce((a, t) => a + t.count, 0);
  const verified = (m.by_verification.find(v => v.verification_status === 'verified') || { c: 0 }).c;
  box.innerHTML = types.map(t => `
    <div class="net-row">
      <span class="small"><span class="swatch" style="background:${CM.esc(t.color)}"></span> ${CM.esc(t.label)}</span>
      <span class="net-bar"><span style="width:${Math.max(3, Math.round(t.count / max * 100))}%;background:${CM.esc(t.color)}"></span></span>
      <strong class="small num">${CM.esc(t.count)}</strong>
    </div>`).join('') + `
    <p class="tiny muted" style="margin:10px 0 0">${CM.esc(total)} records · ${CM.esc(m.connections)} recorded connections ·
      ${CM.esc(Math.round(verified / total * 100))}% verified${m.synthetic_assets ? ` · ${CM.esc(m.synthetic_assets)} synthetic (demo)` : ''}</p>`;
}

/* ---------- live operations map: open incidents + open reports over the utility network ---------- */
const UT_COLORS = { water: '#1F6FE5', sewer: '#7A4FB5', electric: '#D08A00' };
let opsMap = null, opsNet = null, opsPins = null, opsRenderer = null, opsSeq = 0;

async function loadOpsMap() {
  if (typeof L === 'undefined') { $('opsMap').innerHTML = emptyState('Map library could not load.'); return; }
  if (!opsMap) {
    opsMap = L.map('opsMap', { scrollWheelZoom: false, zoomControl: true }).setView([18.5040, 73.8100], 15);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap contributors', maxZoom: 19 }).addTo(opsMap);
    opsRenderer = L.canvas({ padding: 0.3 });
    opsNet = L.layerGroup().addTo(opsMap);
    opsPins = L.layerGroup().addTo(opsMap);
    document.querySelectorAll('[data-ops-ut]').forEach(cb => cb.addEventListener('change', loadOpsNetwork));
    opsMap.on('moveend', () => { clearTimeout(loadOpsMap.t); loadOpsMap.t = setTimeout(loadOpsNetwork, 250); });
  }
  let incs = [], reps = [];
  try { [incs, reps] = await Promise.all([CM.api('/api/map/incidents'), CM.api('/api/map/complaints')]); }
  catch (e) { $('opsMapNote').textContent = e.message; return; }
  opsPins.clearLayers();
  const open = incs.filter(i => i.status !== 'resolved');
  const pts = [];
  reps.filter(r => r.status !== 'resolved' && r.status !== 'rejected').forEach(r => {
    pts.push([r.latitude, r.longitude]);
    L.circleMarker([r.latitude, r.longitude], { renderer: opsRenderer, radius: 4, color: '#fff', weight: 1.5,
      fillColor: r.severity >= 4 ? '#D9342B' : r.severity >= 3 ? '#E0A106' : '#2E9E5B', fillOpacity: 1 })
      .bindTooltip(`${CM.esc(r.public_id)} · ${CM.esc(r.category)}`).addTo(opsPins);
  });
  open.forEach(i => {
    pts.push([i.centroid_lat, i.centroid_lng]);
    const cls = 'p-' + String(i.priority_label || 'high').toLowerCase();
    L.marker([i.centroid_lat, i.centroid_lng], { icon: L.divIcon({ className: '', iconSize: null,
      html: `<span class="ops-inc ${CM.esc(cls)}">⚠ ${Number(i.report_count) || 0}</span>` }), keyboard: true,
      title: `${i.public_id} — ${i.title}` })
      .bindTooltip(`${CM.esc(i.public_id)} · ${CM.esc(i.title)} · ${CM.esc(i.priority_label)}`)
      .on('click', () => { location.href = 'incident.html?id=' + encodeURIComponent(i.id); })
      .addTo(opsPins);
  });
  // Frame the open incidents (where work is happening); fall back to open reports.
  const focus = open.length ? open.map(i => [i.centroid_lat, i.centroid_lng]) : pts;
  if (!loadOpsMap.fitted && focus.length) { opsMap.fitBounds(focus, { padding: [60, 60], maxZoom: 16 }); loadOpsMap.fitted = true; }
  $('opsMapNote').textContent = `${open.length} open incident(s) and ${pts.length - open.length} open report(s) over the recorded utility network. Click an incident to open it.`;
  loadOpsNetwork();
}

async function loadOpsNetwork() {
  const types = [...document.querySelectorAll('[data-ops-ut]:checked')].map(cb => cb.dataset.opsUt);
  const seq = ++opsSeq;
  if (!types.length) { opsNet.clearLayers(); return; }
  const b = opsMap.getBounds();
  const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(v => v.toFixed(5)).join(',');
  let fc;
  try { fc = await CM.api(`/api/utility/assets?bbox=${bbox}&utility_types=${types.join(',')}&limit=5000`); }
  catch { return; }      // the network layer is context; the incidents above still show
  if (seq !== opsSeq) return;
  opsNet.clearLayers();
  fc.features.forEach(f => {
    if (f.geometry.type !== 'LineString') return;
    const p = f.properties;
    L.polyline(f.geometry.coordinates.map(c => [c[1], c[0]]), { renderer: opsRenderer, interactive: false,
      color: UT_COLORS[p.utility_type] || '#5B6B82', weight: ['main', 'trunk_sewer'].includes(p.asset_type) ? 2.4 : 1.3,
      opacity: .8, dashArray: p.verification_status !== 'verified' ? '5 4' : null }).addTo(opsNet);
  });
}

/* ---------- open work orders (contractor, due date, overdue flag) ---------- */
async function loadWorkOrders() {
  const box = $('woList');
  let rows;
  try { rows = await CM.api('/api/work-orders?status=open'); } catch (e) { box.innerHTML = emptyState(e.message); return; }
  $('woCount').textContent = rows.length;
  box.innerHTML = rows.slice(0, 6).map(w => `
    <div class="wo-row">
      <a class="small link" href="incident.html?id=${encodeURIComponent(w.incident_id)}"><strong>${CM.esc(w.public_id)}</strong> · ${CM.esc(w.task)}</a>
      <span class="badge">${CM.esc(CM.titleCase(String(w.status).replace(/_/g, ' ')))}</span>
      <span class="tiny muted">${CM.esc(w.contractor ? w.contractor.name : (w.team_name || w.department || 'Internal team'))}</span>
      <span class="tiny wo-due ${w.overdue ? 'overdue' : ''}">${w.est_completion ? (w.overdue ? 'Overdue · ' : 'Due ') + CM.esc(CM.fmtDate(w.est_completion)) : 'No date'}</span>
    </div>`).join('') || emptyState('No open work orders.');
}

/* Rule-based utility findings (each names its rule and triggering records). */
async function loadUtilityFindings() {
  const box = $('utilFindings');
  try {
    const rows = await CM.api('/api/utility/findings');
    box.innerHTML = rows.slice(0, 6).map(f => `<div class="finding sev-${CM.esc(f.severity)}">
        <div class="row spread wrap-row"><strong class="small">${CM.esc(f.rule_title)}</strong>
          <span class="badge ${CM.sevClass(f.severity)}">${CM.esc(CM.titleCase(f.severity))}</span></div>
        <div class="small">${CM.esc(f.explanation)}</div>
        <div class="tiny">${f.asset_ids.map(a => `<a class="link" href="map.html?asset=${encodeURIComponent(a)}">${CM.esc(a)}</a>`).join(', ')}
          ${f.incident_ids.map(i => ` · <a class="link" href="incident.html?id=${encodeURIComponent(i)}">incident</a>`).join('')}
          ${f.is_synthetic ? ' · <span class="muted">synthetic data</span>' : ''}</div></div>`).join('')
      || emptyState('No active utility findings.');
  } catch (e) { box.innerHTML = emptyState(e.message); }
}
const set = (id, v) => { $(id).textContent = v; };

async function loadDashboard() {
  let d;
  try { d = await CM.api('/api/dashboard'); } catch (e) { return CM.fail(e); }
  const m = d.metrics, def = d.definitions || {};
  set('kOpen', CM.num(m.open_complaints));
  set('kOpenSub', `${CM.num(m.total_complaints)} total`);
  set('kInc', CM.num(m.open_incidents));
  set('kIncSub', `${CM.num(m.high_priority_incidents)} high or critical`);
  set('kPending', CM.num(m.actions_awaiting_approval));
  set('kProgressSub', `${CM.num(m.actions_in_progress)} approved / in progress`);
  set('kFindings', CM.num(m.open_risk_findings));
  set('kResolved', CM.num(m.resolved_complaints));
  set('kRate', m.resolution_rate === null ? 'No complaints yet' : `${m.resolution_rate}% of complaints`);
  set('kMedian', m.median_resolution_hours === null ? '—' : fmtHours(m.median_resolution_hours));
  set('kOverdue', CM.num(m.overdue_work_orders));
  set('kOpenWo', `${CM.num(m.open_work_orders)} open work order(s)`);
  set('kAwaiting', CM.num(m.awaiting_verification));
  const tips = { kOpen: 'open_complaints', kInc: 'open_incidents', kPending: 'actions_awaiting_approval',
                 kFindings: 'open_risk_findings', kResolved: 'resolution_rate', kMedian: 'median_resolution_hours',
                 kOverdue: 'overdue_work_orders', kAwaiting: 'awaiting_verification' };
  Object.entries(tips).forEach(([id, key]) => { if (def[key]) $(id).closest('.metric').title = def[key]; });
  $('demoBanner').hidden = !m.synthetic_complaints;

  queue = d.priority_queue;
  renderQueue();
  renderFindings(d.findings);

  $('activity').innerHTML = d.recent_activity.map(a => `
    <div class="tl-item">
      <div class="tl-t">${CM.esc(a.detail || a.action)}</div>
      <div class="tl-d">${CM.esc(a.actor || '')} · ${CM.esc(CM.timeAgo(a.created_at))}</div>
    </div>`).join('') || emptyState('No activity yet.');

  const reports = d.recent_reports || [];
  set('repCount', reports.length);
  document.querySelector('#reportsTable tbody').innerHTML = reports.map(r => `
    <tr class="static">
      <td><strong>${CM.esc(r.public_id)}</strong> ${syntheticTag(r.is_synthetic)}
          ${r.incident_id ? `<div class="tiny"><a class="link" href="incident.html?id=${encodeURIComponent(r.incident_id)}">in incident →</a></div>` : ''}</td>
      <td class="small">${CM.esc(r.category)}</td>
      <td class="small">${CM.esc(r.ward || '—')}</td>
      <td class="small">${CM.esc(r.citizen_name || '—')}</td>
      <td class="num"><span class="badge ${r.severity >= 4 ? 'critical' : r.severity === 3 ? 'medium' : 'low'}">${CM.esc(r.severity)}</span></td>
      <td class="small">${r.has_photo ? (r.proof_verified ? '✓ passed' : 'review') : '—'}</td>
      <td><span class="badge ${CM.sClass(r.status)}">${CM.esc(CM.statusText(r.status))}</span></td>
      <td class="small muted">${CM.esc(CM.timeAgo(r.created_at))}</td>
      <td>${r.status === 'resolved'
            ? `<span class="badge resolved">✓ ${CM.esc((r.officer_name || 'Resolved').split(' ')[0])}</span>`
            : `<button class="btn sm primary" data-resolve="${Number(r.complaint_id)}">Resolve</button>`}</td>
    </tr>`).join('') || '<tr><td colspan="9" class="empty">No citizen reports yet.</td></tr>';
}

function fmtHours(h) { return h < 48 ? `${h}h` : `${(h / 24).toFixed(1)}d`; }

function renderQueue() {
  const f = $('qFilter').value, q = $('qSearch').value.trim().toLowerCase();
  const rows = queue.filter(i => (!f || i.priority_label === f) &&
    (!q || [i.public_id, i.title, i.ward, i.category, i.asset_code].join(' ').toLowerCase().includes(q)));
  document.querySelector('#queueTable tbody').innerHTML = rows.map(i => `
    <tr data-id="${Number(i.id)}" tabindex="0">
      <td><strong>${CM.esc(i.public_id)}</strong><div class="tiny muted">${CM.esc(i.title)}</div>
          ${i.asset_code ? `<div class="tiny">🏗️ ${CM.esc(i.asset_code)}</div>` : ''}</td>
      <td class="small"><ul class="reasons">${(i.reasons || []).map(r => `<li>${CM.esc(r)}</li>`).join('') || '<li class="muted">Low on all factors</li>'}</ul></td>
      <td class="num">${CM.esc(i.report_count)}</td>
      <td><span class="badge ${CM.pClass(i.priority_label)}">${CM.esc(i.priority_label)} · ${CM.esc(i.priority_score)}</span></td>
      <td><span class="badge ${CM.sClass(i.status)}">${CM.esc(CM.statusText(i.status))}</span></td>
    </tr>`).join('') || `<tr class="static"><td colspan="5" class="empty">${queue.length ? 'No incidents match the filter.' : 'No open incidents. Incidents appear when nearby related reports are grouped.'}</td></tr>`;
}

function renderFindings(list) {
  const box = $('findings');
  if (!list.length) { box.innerHTML = emptyState('No open findings. Run a scan after new data arrives.'); return; }
  box.innerHTML = list.map(f => `
    <a class="list-item" href="${f.incident_id ? 'incident.html?id=' + encodeURIComponent(f.incident_id) : 'assets.html'}">
      <div class="li-icon sev-${CM.esc(f.severity)}" aria-hidden="true">🚩</div>
      <div class="grow">
        <div class="row wrap-row" style="gap:6px"><span class="badge ${CM.sevClass(f.severity)}">${CM.esc(CM.titleCase(f.severity))}</span>
          <span class="tiny muted">Rule ${CM.esc(f.rule_id)}${f.asset_code ? ' · ' + CM.esc(f.asset_code) : ''}</span>${syntheticTag(f.is_synthetic)}</div>
        <div class="small" style="margin-top:4px">${CM.esc(f.explanation)}</div>
      </div>
    </a>`).join('');
}

/* ---------- charts ---------- */
function chart(id, cfg, hasData) {
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
  const canvas = $(id);
  const box = canvas.parentElement;
  box.querySelector('.chart-empty')?.remove();
  if (!hasData) {
    const p = document.createElement('div'); p.className = 'chart-empty empty small'; p.textContent = 'No data yet.';
    box.appendChild(p); canvas.hidden = true; return;
  }
  canvas.hidden = false;
  charts[id] = new Chart(canvas, cfg);
}

async function loadCharts() {
  if (typeof Chart === 'undefined') {
    document.querySelectorAll('.chart-box').forEach(b => { b.innerHTML = emptyState('Charts could not load (offline?).'); });
    return;
  }
  let d;
  try { d = await CM.api('/api/dashboard/charts'); } catch (e) { return CM.fail(e); }
  const grid = { color: C.line };
  const base = { responsive: true, maintainAspectRatio: false };
  chart('cCat', { type: 'doughnut',
    data: { labels: d.categories.map(c => c.category), datasets: [{ data: d.categories.map(c => c.c), backgroundColor: PALETTE, borderWidth: 2, borderColor: '#fff' }] },
    options: { ...base, cutout: '62%', plugins: { legend: { position: 'right', labels: { boxWidth: 9, font: { size: 10 } } } } } }, d.categories.length > 0);
  chart('cTrend', { type: 'line',
    data: { labels: d.trend.map(t => t.day.slice(5)), datasets: [
      { label: 'Submitted', data: d.trend.map(t => t.created), borderColor: C.orange, backgroundColor: 'rgba(232,85,30,.12)', fill: true, tension: .3 },
      { label: 'Resolved', data: d.trend.map(t => t.resolved), borderColor: C.green, backgroundColor: 'rgba(46,158,91,.12)', fill: true, tension: .3, borderDash: [5, 4] }] },
    options: { ...base, scales: { x: { grid: { display: false } }, y: { grid, beginAtZero: true, ticks: { precision: 0 } } } } },
    d.trend.some(t => t.created || t.resolved));
  const order = ['Critical', 'High', 'Medium', 'Low'];
  const pr = order.map(l => (d.priorities.find(p => p.label === l) || { c: 0 }).c);
  chart('cPrio', { type: 'bar',
    data: { labels: order, datasets: [{ data: pr, backgroundColor: [C.red, C.orange, C.amber, C.green], borderRadius: 8 }] },
    options: { ...base, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false } }, y: { grid, beginAtZero: true, ticks: { precision: 0 } } } } },
    pr.some(Boolean));
  chart('cWard', { type: 'bar',
    data: { labels: d.by_ward.map(w => w.ward), datasets: [{ data: d.by_ward.map(w => w.c), backgroundColor: C.blue, borderRadius: 8 }] },
    options: { ...base, indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { grid, beginAtZero: true, ticks: { precision: 0 } }, y: { grid: { display: false } } } } },
    d.by_ward.length > 0);
}

async function runDetect() {
  await CM.busy($('detectBtn'), 'Grouping…', async () => {
    try {
      const d = await CM.api('/api/incidents/detect', { method: 'POST' });
      CM.toast(d.detected ? `${d.detected} new incident(s) created` : 'No new groups found — reports are already grouped or too few/far apart.', 'ok');
      loadAll();
    } catch (e) { CM.fail(e); }
  });
}
async function runScan() {
  await CM.busy($('scanBtn'), 'Scanning…', async () => {
    try {
      const s = await CM.api('/api/risk/scan', { method: 'POST' });
      CM.toast(`Risk scan: ${s.active} active finding(s), ${s.new} new, ${s.cleared} cleared`, 'ok');
      loadDashboard();
    } catch (e) { CM.fail(e); }
  });
}

/* ---------- resolve a single complaint (proof photo required) ---------- */
let resolving = null, plan = null;

async function openResolve(id) {
  resolving = id; plan = null;
  let c;
  try { c = await CM.api('/api/complaints/' + id); } catch (e) { return CM.fail(e); }
  const modal = $('resolveModal');
  modal.innerHTML = `
    <div class="modal-backdrop" id="mBackdrop">
      <div class="card modal" role="dialog" aria-modal="true" aria-labelledby="mTitle">
        <div class="card-h"><h3 id="mTitle">Resolve ${CM.esc(c.public_id)}</h3>
          <button class="btn sm ghost" id="mClose" aria-label="Close">✕</button></div>
        <div class="complaint-body">
          ${c.has_photo || (c.media || []).length ? `<div class="media-row" style="max-width:300px">${CM.mediaThumbs(c, 'lg')}</div>` : '<div class="thumb lg img-missing">No photo or video</div>'}
          <div>
            <div class="row wrap-row" style="gap:6px"><span class="badge cat">${CM.esc(c.category)}</span><span class="badge">${CM.esc(c.ward || '')}</span>${syntheticTag(c.is_synthetic)}</div>
            <p class="small" style="margin:8px 0 0">${CM.esc(c.description)}</p>
            ${c.asset ? `<p class="tiny muted">Linked asset: ${CM.esc(c.asset.type_label)} ${CM.esc(c.asset.asset_code)} (${Math.round(c.asset.distance_m)} m)</p>` : ''}
          </div>
        </div>
        <div class="divider"></div>
        <div class="eyebrow">Suggested plan <span class="muted">(recommendation — you decide)</span></div>
        <div id="planBox" style="margin-top:8px"><div class="skeleton" style="height:14px;width:70%"></div></div>
        <div class="divider"></div>
        <div class="field">
          <label class="f" for="rProof">Proof-of-work photo <span class="text-red">*</span></label>
          <input type="file" id="rProof" accept="image/jpeg,image/png,image/webp"/>
          <div class="hint">Required. Must be a new photo (not the citizen's photo). Automated checks reject blank, tiny or re-used images.</div>
        </div>
        <div class="field"><label class="f" for="rNotes">Completion notes</label>
          <textarea id="rNotes" maxlength="500" placeholder="e.g. Pothole filled and levelled"></textarea></div>
        <div class="form-error" id="rError" role="alert" hidden></div>
        <button class="btn primary block" id="rBtn">Submit proof &amp; mark resolved</button>
      </div>
    </div>`;
  CM.hydrateImages(modal);
  $('mClose').addEventListener('click', closeResolve);
  $('mBackdrop').addEventListener('click', (e) => { if (e.target.id === 'mBackdrop') closeResolve(); });
  document.addEventListener('keydown', escClose);
  $('rBtn').addEventListener('click', submitResolve);
  $('rProof').focus();
  try {
    plan = await CM.api('/api/complaints/' + id + '/plan');
    const box = $('planBox');
    if (box) box.innerHTML = `
      <div class="card tight soft">
        <p style="font-weight:700;margin:0 0 8px">${CM.esc(plan.action_text)}</p>
        <div class="row wrap-row" style="gap:6px">
          <span class="badge">🏢 ${CM.esc(plan.assigned_team || plan.department)}</span>
          <span class="badge">⏱️ ${CM.esc(plan.eta_label)}</span>
          <span class="badge">${CM.esc(CM.engineLabel(plan.engine))}</span>
        </div>
        <ul class="small" style="padding-left:18px;margin:8px 0 0">${(plan.checklist || []).map(x => `<li>${CM.esc(x)}</li>`).join('')}</ul>
      </div>`;
  } catch {
    const box = $('planBox'); if (box) box.innerHTML = '<p class="hint">Suggested plan unavailable — proceed manually.</p>';
  }
}
function escClose(e) { if (e.key === 'Escape') closeResolve(); }
function closeResolve() { $('resolveModal').innerHTML = ''; resolving = null; plan = null; document.removeEventListener('keydown', escClose); }

async function submitResolve() {
  const f = $('rProof').files[0], err = $('rError');
  err.hidden = true;
  if (!f) { err.textContent = 'A proof-of-work photo is required.'; err.hidden = false; return; }
  await CM.busy($('rBtn'), 'Checking photo…', async () => {
    try {
      const body = { notes: $('rNotes').value.trim(), proof: await CM.fileToDataURL(f) };
      if (plan) { body.plan = plan; body.eta_hours = plan.eta_hours; }
      const res = await CM.api('/api/complaints/' + resolving + '/resolve', { method: 'POST', body });
      CM.toast(`Resolved${res.incident_closed ? ' — incident closed (all linked reports resolved)' : ''}`, 'ok');
      closeResolve(); loadAll();
    } catch (e) {
      err.textContent = e.message + ((e.reasons || []).length ? ' — ' + e.reasons.join('; ') : '');
      err.hidden = false;
    }
  });
}
