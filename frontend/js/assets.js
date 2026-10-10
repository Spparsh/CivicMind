/* CivicMind — infrastructure page (officer/admin) */
const user = CM.requireAuth(['officer', 'admin']);
const $ = (id) => document.getElementById(id);
let assets = [], selected = null;

document.addEventListener('DOMContentLoaded', async () => {
  if (!user) return;
  renderNav('assets.html', user);
  $('scanBtn').addEventListener('click', () => CM.busy($('scanBtn'), 'Scanning…', async () => {
    try {
      const s = await CM.api('/api/risk/scan', { method: 'POST' });
      CM.toast(`${s.active} active finding(s): ${s.new} new, ${s.cleared} cleared`, 'ok');
      loadFindings(); loadAssets();
    } catch (e) { CM.fail(e); }
  }));
  $('fStatus').addEventListener('change', loadFindings);
  $('fType').addEventListener('change', renderAssets);
  $('assetTable').addEventListener('click', (e) => { const tr = e.target.closest('tr[data-id]'); if (tr) showAsset(Number(tr.dataset.id)); });
  $('assetTable').addEventListener('keydown', (e) => { const tr = e.target.closest('tr[data-id]'); if (tr && e.key === 'Enter') showAsset(Number(tr.dataset.id)); });
  $('findingsBody').addEventListener('click', reviewClick);
  $('detail').addEventListener('submit', addMaintenance);
  try {
    (await CM.api('/api/assets/types')).forEach(t => {
      const o = document.createElement('option'); o.value = t.type; o.textContent = t.label; $('fType').appendChild(o);
    });
  } catch { /* filter optional */ }
  await Promise.all([loadFindings(), loadAssets()]);
  const qid = Number(new URLSearchParams(location.search).get('id'));
  if (qid) showAsset(qid);
});

async function loadFindings() {
  const body = $('findingsBody');
  try {
    const rows = await CM.api('/api/risk/findings?status=' + encodeURIComponent($('fStatus').value));
    body.innerHTML = rows.map(f => `
      <tr class="static">
        <td><span class="badge ${CM.sevClass(f.severity)}">${CM.esc(CM.titleCase(f.severity))}</span></td>
        <td class="small"><strong>${CM.esc(f.rule_id)}</strong> · ${CM.esc(f.rule_title)}</td>
        <td class="small">${CM.esc(f.explanation)}<div class="tiny muted">Next: ${CM.esc(f.recommended_step || '—')}</div>
            <div class="tiny muted">Detected ${CM.esc(CM.fmtDate(f.detected_at))} ${syntheticTag(f.is_synthetic)}</div></td>
        <td class="small">${f.asset_code ? `<button class="link-btn" data-asset="${Number(f.asset_id)}">${CM.esc(f.asset_code)}</button>` : ''}
            ${f.incident_public_id ? `<div><a class="link" href="incident.html?id=${encodeURIComponent(f.incident_id)}">${CM.esc(f.incident_public_id)}</a></div>` : ''}</td>
        <td><span class="badge ${CM.sClass(f.status)}">${CM.esc(CM.statusText(f.status))}</span></td>
        <td>${['open', 'acknowledged', 'dismissed'].includes(f.status) ? reviewButtons(f) : ''}</td>
      </tr>`).join('') || '<tr class="static"><td colspan="6" class="empty">No findings in this view.</td></tr>';
  } catch (e) { body.innerHTML = `<tr class="static"><td colspan="6" class="empty">${CM.esc(e.message)}</td></tr>`; }
}
function reviewButtons(f) {
  const opts = { open: ['acknowledged', 'dismissed'], acknowledged: ['dismissed', 'open'], dismissed: ['open'] }[f.status];
  const label = { acknowledged: 'Acknowledge', dismissed: 'Dismiss', open: 'Reopen' };
  return opts.map(s => `<button class="btn sm ghost" data-review="${Number(f.id)}" data-status="${s}">${label[s]}</button>`).join('');
}
async function reviewClick(e) {
  const a = e.target.closest('[data-asset]');
  if (a) return showAsset(Number(a.dataset.asset));
  const b = e.target.closest('[data-review]');
  if (!b) return;
  await CM.busy(b, '…', async () => {
    try {
      await CM.api(`/api/risk/findings/${b.dataset.review}/review`, { method: 'POST', body: { status: b.dataset.status } });
      CM.toast('Finding updated', 'ok'); loadFindings();
    } catch (ex) { CM.fail(ex); }
  });
}

async function loadAssets() {
  try { assets = await CM.api('/api/assets'); renderAssets(); }
  catch (e) { document.querySelector('#assetTable tbody').innerHTML = `<tr class="static"><td colspan="6" class="empty">${CM.esc(e.message)}</td></tr>`; }
}
function renderAssets() {
  const t = $('fType').value;
  const rows = assets.filter(a => !t || a.asset_type === t);
  document.querySelector('#assetTable tbody').innerHTML = rows.map(a => `
    <tr data-id="${Number(a.id)}" tabindex="0" class="${selected === a.id ? 'selected' : ''}">
      <td><strong>${CM.esc(a.asset_code)}</strong> ${syntheticTag(a.is_synthetic)}<div class="tiny muted">${CM.esc(a.name)}</div></td>
      <td class="small">${CM.esc(a.type_label)}</td>
      <td><span class="badge ${CM.sevClass(a.criticality)}">${CM.esc(CM.titleCase(a.criticality))}</span></td>
      <td class="num">${CM.esc(a.open_complaints)}</td>
      <td class="num">${a.open_findings ? `<span class="badge high">${CM.esc(a.open_findings)}</span>` : '0'}</td>
      <td class="small">${a.inspection_overdue === true ? '<span class="badge critical">Overdue</span>' : a.inspection_overdue === false ? 'Up to date' : '<span class="muted">No data</span>'}</td>
    </tr>`).join('') || `<tr class="static"><td colspan="6" class="empty">${assets.length ? 'No assets of this type.' : 'No assets yet. An admin can import a GeoJSON file of asset points.'}</td></tr>`;
}

async function showAsset(id) {
  selected = id; renderAssets();
  const box = $('detail');
  box.innerHTML = '<section class="card"><div class="skeleton" style="height:20px;width:50%"></div></section>';
  let a;
  try { a = await CM.api('/api/assets/' + id); } catch (e) { box.innerHTML = `<section class="card">${emptyState(e.message)}</section>`; return; }
  const today = new Date().toISOString().slice(0, 10);
  box.innerHTML = `
  <section class="card" style="margin-bottom:18px">
    <div class="card-h"><h3>${CM.esc(a.asset_code)}</h3><span class="badge ${CM.sevClass(a.criticality)}">${CM.esc(CM.titleCase(a.criticality))}</span></div>
    <p class="small" style="margin-top:0">${CM.esc(a.type_label)} — ${CM.esc(a.name)} ${syntheticTag(a.is_synthetic)}</p>
    ${a.criticality_reason ? `<p class="tiny muted">Critical because: ${CM.esc(a.criticality_reason)}</p>` : ''}
    <div class="kv"><span class="k">Status</span><span class="v">${CM.esc(CM.titleCase(a.operational_status))}</span></div>
    <div class="kv"><span class="k">Ward (approx.)</span><span class="v">${CM.esc(a.ward || '—')}</span></div>
    <div class="kv"><span class="k">Department</span><span class="v">${CM.esc(a.department || '—')}</span></div>
    <div class="kv"><span class="k">Inspection interval</span><span class="v">${a.inspection_interval_days ? CM.esc(a.inspection_interval_days) + ' days' : '—'}</span></div>
    <div class="kv"><span class="k">Last inspected</span><span class="v">${CM.esc(CM.fmtDay(a.last_inspected_at))} ${a.inspection_overdue ? '<span class="badge critical">Overdue</span>' : ''}</span></div>
    <div class="kv"><span class="k">Data source</span><span class="v small">${CM.esc(a.data_source)}</span></div>
  </section>
  <section class="card" style="margin-bottom:18px">
    <div class="card-h"><h3>Maintenance history</h3><span class="badge">${a.maintenance.length}</span></div>
    ${a.maintenance.map(m => `<div class="evidence-item"><span class="badge">${CM.esc(CM.titleCase(m.event_type))}</span>
      <div class="small grow">${CM.esc(CM.fmtDay(m.performed_at))} — ${CM.esc(m.outcome || '')} ${syntheticTag(m.is_synthetic)}
      <div class="tiny muted">${CM.esc(m.performed_by || '')}${m.recorded_by_name ? ' · logged by ' + CM.esc(m.recorded_by_name) : ''} · source: ${CM.esc(m.data_source)}</div>
      ${m.notes ? `<div class="tiny">${CM.esc(m.notes)}</div>` : ''}</div></div>`).join('') || '<p class="small muted">No records.</p>'}
    <details style="margin-top:10px"><summary class="small">＋ Log maintenance or inspection</summary>
      <form id="maintForm" data-asset="${Number(a.id)}" style="margin-top:10px">
        <div class="grid g2" style="gap:10px">
          <div class="field"><label class="f" for="mType">Type</label>
            <select id="mType"><option value="inspection">Inspection</option><option value="repair">Repair</option><option value="replacement">Replacement</option><option value="cleaning">Cleaning</option></select></div>
          <div class="field"><label class="f" for="mDate">Date</label><input type="date" id="mDate" max="${today}" value="${today}" required/></div>
        </div>
        <div class="field"><label class="f" for="mOutcome">Outcome</label><input id="mOutcome" maxlength="200" placeholder="e.g. Lamp replaced"/></div>
        <div class="field"><label class="f" for="mBy">Performed by</label><input id="mBy" maxlength="120" placeholder="Team / contractor"/></div>
        <button class="btn primary sm" type="submit" id="mBtn">Save record</button>
      </form>
    </details>
  </section>
  <section class="card" style="margin-bottom:18px">
    <div class="card-h"><h3>Linked reports</h3><span class="badge">${a.complaints.length}</span></div>
    ${a.complaints.map(c => `<div class="evidence-item"><span class="badge ${CM.sClass(c.status)}">${CM.esc(CM.statusText(c.status))}</span>
      <div class="small grow"><strong>${CM.esc(c.public_id)}</strong> · ${CM.esc(c.category)} · ${CM.esc(Math.round(c.distance_m || 0))} m · match ${CM.esc(c.match_status)}
      <div class="tiny muted">${CM.esc(CM.fmtDate(c.created_at))}${c.incident_id ? ` · <a class="link" href="incident.html?id=${encodeURIComponent(c.incident_id)}">incident →</a>` : ''}</div></div></div>`).join('') || '<p class="small muted">No reports linked.</p>'}
  </section>
  <section class="card"><div class="card-h"><h3>Findings on this asset</h3></div>
    ${a.findings.map(f => `<div class="finding sev-${CM.esc(f.severity)}"><span class="badge ${CM.sevClass(f.severity)}">${CM.esc(CM.titleCase(f.severity))}</span>
      <span class="badge ${CM.sClass(f.status)}">${CM.esc(CM.statusText(f.status))}</span>
      <p class="small" style="margin:6px 0 0">${CM.esc(f.explanation)}</p></div>`).join('') || '<p class="small muted">No findings.</p>'}
  </section>`;
}

async function addMaintenance(e) {
  if (e.target.id !== 'maintForm') return;
  e.preventDefault();
  const id = e.target.dataset.asset;
  await CM.busy($('mBtn'), 'Saving…', async () => {
    try {
      await CM.api(`/api/assets/${id}/maintenance`, { method: 'POST', body: {
        event_type: $('mType').value, performed_at: $('mDate').value,
        outcome: $('mOutcome').value.trim() || null, performed_by: $('mBy').value.trim() || null,
        data_source: 'Officer entry' } });
      CM.toast('Record saved', 'ok');
      await Promise.all([loadAssets(), loadFindings()]);
      showAsset(Number(id));
    } catch (ex) { CM.fail(ex); }
  });
}
