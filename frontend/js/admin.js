/* CivicMind — admin console */
const user = CM.requireAuth(['admin']);
const $ = (id) => document.getElementById(id);

document.addEventListener('DOMContentLoaded', () => {
  if (!user) return;
  renderNav('admin.html', user);
  $('addUser').addEventListener('submit', addUser);
  $('importForm').addEventListener('submit', importAssets);
  $('resetBtn').addEventListener('click', resetData);
  $('uiForm').addEventListener('submit', previewImport);
  $('uiResult').addEventListener('click', (e) => { const b = e.target.closest('[data-confirm]'); if (b) confirmImport(b); });
  $('ufScan').addEventListener('click', () => CM.busy($('ufScan'), 'Running…', async () => {
    try { const r = await CM.api('/api/utility/findings/scan', { method: 'POST' }); CM.toast(`${r.active} active finding(s)`, 'ok'); loadFindings(); }
    catch (e) { CM.fail(e); } }));
  $('ufList').addEventListener('click', reviewFinding);
  const snap = new URLSearchParams(location.search).get('snapshot');
  if (snap && /^\d+$/.test(snap)) { $('uiSnapshot').value = snap; setTimeout(() => $('utilImport').scrollIntoView(), 300); }
  loadAll();
});

function loadAll() { loadStats(); loadUsers(); loadConfig(); loadComplaints(); loadResolutions(); loadUtility(); loadJobs(); loadFindings(); loadAudit();
  loadDepartments(); loadContractors(); loadAnalytics(); }

document.addEventListener('DOMContentLoaded', () => {
  if (!user) return;
  $('usersTable').addEventListener('click', userAction);
  $('deptForm').addEventListener('submit', addDepartment);
  $('deptTable').addEventListener('click', toggleDepartment);
  $('conForm').addEventListener('submit', addContractor);
  $('conList').addEventListener('click', contractorAction);
  $('uiJobs').addEventListener('click', rollbackImport);
  $('regConn').addEventListener('submit', (e) => adminForm(e, '/api/utility/connections', {
    from_asset_id: $('rcFrom').value.trim(), to_asset_id: $('rcTo').value.trim(), direction: $('rcDir').value,
    connection_type: 'feeds', notes: $('rcNote').value.trim() }, 'Connection recorded as unverified', [loadUtility, loadAudit]));
  $('regVerify').addEventListener('submit', (e) => adminForm(e, '/api/utility/records/verify', {
    kind: $('rvKind').value, id: $('rvId').value.trim(), status: $('rvStatus').value, note: $('rvNote').value.trim() },
    'Verification decision recorded', [loadUtility, loadFindings, loadAudit]));
  $('regContact').addEventListener('submit', (e) => {
    const v = $('ctPhone').value.trim();
    adminForm(e, '/api/utility/contacts', { organization: $('ctOrg').value.trim(), role: $('ctRole').value.trim(),
      utility_type: $('ctUtil').value, source_note: $('ctSource').value.trim(), ...(v.includes('@') ? { email: v } : { phone: v }) },
      'Contact added as unverified', [loadAudit]);
  });
});

async function adminForm(e, path, body, okMsg, reloads) {
  e.preventDefault();
  const btn = e.target.querySelector('button[type="submit"]');
  await CM.busy(btn, '…', async () => {
    try { await CM.api(path, { method: 'POST', body }); CM.toast(okMsg, 'ok'); e.target.reset(); (reloads || []).forEach(f => f()); }
    catch (ex) { CM.fail(ex); }
  });
}

/* ---------- users: role / department / active (server enforces; role change signs the user out) ---------- */
async function userAction(e) {
  const b = e.target.closest('[data-uid]');
  if (!b) return;
  const id = Number(b.dataset.uid), row = b.closest('tr');
  const body = { role: row.querySelector('[data-f="role"]').value, department: row.querySelector('[data-f="dept"]').value || '',
                 is_active: row.querySelector('[data-f="active"]').checked };
  if (!confirm(`Save changes for this user? Changing role or access signs them out everywhere.`)) return;
  try { await CM.api(`/api/admin/users/${id}`, { method: 'POST', body }); CM.toast('User updated', 'ok'); loadUsers(); loadAudit(); }
  catch (ex) { CM.fail(ex); }
}

let DEPTS = [];
async function loadDepartments() {
  try {
    DEPTS = await CM.api('/api/admin/departments');
    document.querySelector('#deptTable tbody').innerHTML = DEPTS.map(d => `<tr class="static">
      <td class="small"><strong>${CM.esc(d.name)}</strong>${d.in_config ? ' <span class="tiny muted">(routing)</span>' : ''}</td>
      <td class="small">${CM.esc(d.officers)}</td>
      <td><span class="badge ${d.is_active ? 'low' : 'pending'}">${d.is_active ? 'Active' : 'Inactive'}</span></td>
      <td>${d.in_config ? '' : `<button class="link-btn tiny" data-did="${Number(d.id)}" data-active="${d.is_active ? 0 : 1}">${d.is_active ? 'Deactivate' : 'Activate'}</button>`}</td></tr>`).join('');
    loadUsers();
  } catch (e) { CM.fail(e); }
}
async function addDepartment(e) {
  e.preventDefault();
  const name = $('deptName').value.trim();
  if (name.length < 3) return CM.toast('Department name must be at least 3 characters.', 'err');
  try { await CM.api('/api/admin/departments', { method: 'POST', body: { name } }); $('deptName').value = ''; CM.toast('Department added', 'ok'); loadDepartments(); loadConfig(); }
  catch (ex) { CM.fail(ex); }
}
async function toggleDepartment(e) {
  const b = e.target.closest('[data-did]');
  if (!b) return;
  try { await CM.api(`/api/admin/departments/${b.dataset.did}/state`, { method: 'POST', body: { is_active: b.dataset.active === '1' } }); loadDepartments(); }
  catch (ex) { CM.fail(ex); }
}

/* ---------- contractors ---------- */
async function loadContractors() {
  try {
    const rows = await CM.api('/api/contractors');
    $('conList').innerHTML = rows.map(c => `<div class="contact ${c.is_synthetic ? 'warn' : c.verification_status === 'verified' ? 'ok' : ''}">
        <div class="row spread wrap-row"><strong class="small">${CM.esc(c.name)}</strong><span class="tiny muted">${CM.esc(c.contractor_code)}</span></div>
        <div class="tiny">${CM.esc(c.department || '—')} · ${CM.esc(c.contact_person || '')} · ${CM.esc(c.phone || c.email || 'no contact')}</div>
        <div class="tiny"><span class="badge ${c.is_synthetic ? 'synthetic' : c.verification_status === 'verified' ? 'low' : 'pending'}">${CM.esc(c.label)}</span>
          ${c.is_active ? '' : '<span class="badge pending">Inactive</span>'}
          <button class="link-btn tiny" data-cid="${Number(c.id)}" data-op="verify">Mark verified</button> ·
          <button class="link-btn tiny" data-cid="${Number(c.id)}" data-op="${c.is_active ? 'deactivate' : 'activate'}">${c.is_active ? 'Deactivate' : 'Activate'}</button></div></div>`).join('')
      || '<p class="small muted">No contractors registered.</p>';
  } catch (e) { CM.fail(e); }
}
async function addContractor(e) {
  e.preventDefault();
  const body = { contractor_code: $('conCode').value.trim(), name: $('conName').value.trim(), department: $('conDept').value.trim() || null,
    contact_person: $('conPerson').value.trim() || null, phone: $('conPhone').value.trim() || null,
    office_address: $('conAddr').value.trim() || null, is_synthetic: $('conSynthetic').checked };
  try { await CM.api('/api/admin/contractors', { method: 'POST', body }); $('conForm').reset(); CM.toast('Contractor registered (unverified)', 'ok'); loadContractors(); loadAudit(); }
  catch (ex) { CM.fail(ex); }
}
async function contractorAction(e) {
  const b = e.target.closest('[data-cid]');
  if (!b) return;
  const note = prompt('Note for the audit log (5+ characters):');
  if (!note) return;
  const body = { note, ...(b.dataset.op === 'verify' ? { verification_status: 'verified' } : { is_active: b.dataset.op === 'activate' }) };
  try { await CM.api(`/api/admin/contractors/${b.dataset.cid}/status`, { method: 'POST', body }); loadContractors(); loadAudit(); }
  catch (ex) { CM.fail(ex); }
}

/* ---------- import rollback ---------- */
async function rollbackImport(e) {
  const b = e.target.closest('[data-rollback]');
  if (!b) return;
  const typed = prompt(`Remove every record created by ${b.dataset.rollback}? This is refused if anything now depends on them. Type ROLLBACK to confirm.`);
  if (typed !== 'ROLLBACK') return;
  try {
    const r = await CM.api('/api/utility/import/rollback', { method: 'POST', body: { job_code: b.dataset.rollback, confirm: 'ROLLBACK' } });
    CM.toast(`Rolled back: ${r.removed.assets} asset(s), ${r.removed.connections} connection(s)`, 'ok'); loadJobs(); loadUtility(); loadAudit();
  } catch (ex) { CM.fail(ex); }
}

/* ---------- analytics (computed from records) ---------- */
async function loadAnalytics() {
  try {
    const a = await CM.api('/api/admin/analytics');
    const tbl = (title, rows, cols) => `<div><h4 style="margin:0 0 6px">${CM.esc(title)}</h4>${rows.length ? `<table class="table"><tbody>${rows.map(r =>
      `<tr class="static">${cols.map(c => `<td class="small">${CM.esc(r[c] ?? 0)}</td>`).join('')}</tr>`).join('')}</tbody></table>` : '<p class="small muted">No records.</p>'}</div>`;
    $('anaBody').innerHTML = tbl('Reports by category (total · resolved)', a.complaints_by_category, ['category', 'c', 'resolved'])
      + tbl('Reports by ward (total · open)', a.complaints_by_ward, ['ward', 'c', 'open'])
      + tbl('Work orders by status', a.work_orders_by_status, ['status', 'c'])
      + (a.repeat_assets.length ? tbl('Utility assets linked to 2+ incidents', a.repeat_assets, ['asset_id', 'name', 'incidents']) : '')
      + `<p class="tiny muted">${CM.esc(a.definition)}</p>`;
  } catch (e) { CM.fail(e); }
}

/* ---------- utility network data ---------- */
async function loadUtility() {
  try {
    const m = await CM.api('/api/utility/meta');
    const total = m.utility_types.reduce((s, t) => s + t.count, 0);
    const v = Object.fromEntries(m.by_verification.map(r => [r.verification_status, r.c]));
    $('utilStats').innerHTML = `
      <div class="card metric"><div class="k">Utility assets</div><div class="v">${CM.num(total)}</div>
        <div class="sub tiny muted">${m.utility_types.filter(t => t.count).map(t => `${CM.esc(t.label)} ${CM.num(t.count)}`).join(' · ') || 'none yet'}</div></div>
      <div class="card metric"><div class="k">Recorded connections</div><div class="v">${CM.num(m.connections)}</div><div class="sub tiny muted">explicit links only</div></div>
      <div class="card metric"><div class="k">Verification</div><div class="v">${CM.num(v.verified || 0)}</div>
        <div class="sub tiny muted">verified · ${CM.num(v.unverified || 0)} unverified · ${CM.num(v.disputed || 0)} disputed</div></div>
      <div class="card metric"><div class="k">Synthetic records</div><div class="v">${CM.num(m.synthetic_assets)}</div>
        <div class="sub tiny muted">${m.by_provenance.map(p => `${CM.esc(CM.titleCase(p.provenance_class))} ${CM.num(p.c)}`).join(' · ')}</div></div>`;
  } catch (e) { $('utilStats').innerHTML = `<div class="card empty">${CM.esc(e.message)}</div>`; }
}

async function previewImport(e) {
  e.preventDefault();
  const err = $('uiError'); err.hidden = true;
  const f = $('uiFile').files[0];
  if (!f) { err.textContent = 'Choose a GeoJSON or CSV file.'; err.hidden = false; return; }
  if (f.size > 6 * 1024 * 1024) { err.textContent = 'File is larger than 6 MB.'; err.hidden = false; return; }
  const snapshot = $('uiSnapshot').value.trim();
  const body = { format: $('uiFormat').value, content: await f.text() };
  if (snapshot) {
    if (!/^\d+$/.test(snapshot)) { err.textContent = 'Snapshot ID must be a number.'; err.hidden = false; return; }
    body.snapshot_id = Number(snapshot); body.source = { file_name: f.name };
  } else {
    if ($('uiName').value.trim().length < 2) { err.textContent = 'Describe where the data came from (source name).'; err.hidden = false; return; }
    body.source = { name: $('uiName').value.trim(), authority: $('uiAuthority').value.trim() || null, file_name: f.name,
      license_notes: $('uiLicense').value.trim() || null, provenance_class: $('uiProv').value,
      is_synthetic: $('uiProv').value === 'synthetic_demo' };
  }
  await CM.busy($('uiPreviewBtn'), 'Validating…', async () => {
    try { renderPreview(await CM.api('/api/utility/import/preview', { method: 'POST', body })); }
    catch (ex) { err.textContent = ex.message; err.hidden = false; }
  });
}

function issueList(title, rows) {
  if (!rows.length) return '';
  return `<p class="small" style="margin:8px 0 2px"><strong>${CM.esc(title)} (${rows.length})</strong></p>
    <ul class="tiny" style="margin:0;padding-left:18px">${rows.slice(0, 30).map(r => `<li>${CM.esc(r.id || '#' + r.index)} — ${CM.esc(r.reason)}</li>`).join('')}</ul>`;
}

function renderPreview(p) {
  const a = p.summary.assets, c = p.summary.connections;
  $('uiResult').innerHTML = `<div class="card tight soft" style="margin-top:14px">
    <div class="row spread wrap-row"><strong>Preview ${CM.esc(p.job_code)} — nothing saved yet</strong>
      <span class="badge">${CM.esc(CM.titleCase(p.source.provenance_class))}</span></div>
    <p class="small">Assets: <strong>${CM.esc(a.accepted)}</strong> accepted of ${CM.esc(a.received)} ·
       Connections: <strong>${CM.esc(c.accepted)}</strong> accepted of ${CM.esc(c.received)}</p>
    ${issueList('Duplicates (never overwritten)', [...a.duplicates, ...c.duplicates])}
    ${issueList('Rejected assets', a.rejected)}
    ${issueList('Missing relationships', c.missing_relationships)}
    ${issueList('Rejected connections', c.rejected)}
    ${p.summary.warnings.length ? `<p class="small" style="margin:8px 0 2px"><strong>Warnings</strong></p><ul class="tiny" style="margin:0;padding-left:18px">${p.summary.warnings.map(w => `<li>${CM.esc(w)}</li>`).join('')}</ul>` : ''}
    ${p.sample.length ? `<p class="small" style="margin:8px 0 2px"><strong>Sample</strong></p><ul class="tiny" style="margin:0;padding-left:18px">${p.sample.map(s => `<li>${CM.esc(s.asset_id)} · ${CM.esc(s.utility_type)} ${CM.esc(s.asset_type)} · ${CM.esc(s.name)}</li>`).join('')}</ul>` : ''}
    <p class="tiny muted">${CM.esc(p.summary.verification_note)}</p>
    ${p.can_confirm ? `<button class="btn primary" data-confirm="${CM.esc(p.job_code)}">Confirm import of accepted records</button>` : '<p class="small text-red">Nothing valid to import.</p>'}
  </div>`;
}

async function confirmImport(btn) {
  await CM.busy(btn, 'Importing…', async () => {
    try {
      const r = await CM.api('/api/utility/import/confirm', { method: 'POST', body: { job_code: btn.dataset.confirm } });
      const c = r.summary.committed;
      $('uiResult').innerHTML = `<div class="card tight" style="margin-top:14px"><strong>Imported ${CM.esc(r.job_code)}</strong>
        <p class="small">${CM.esc(c.assets)} asset(s) and ${CM.esc(c.connections)} connection(s) saved as unverified.
        ${c.skipped_assets.length ? CM.esc(c.skipped_assets.length) + ' skipped (appeared since the preview).' : ''}</p></div>`;
      CM.toast('Import committed', 'ok'); loadUtility(); loadJobs(); loadFindings(); loadAudit();
    } catch (ex) { CM.fail(ex); }
  });
}

async function loadJobs() {
  try {
    const rows = await CM.api('/api/utility/import/jobs');
    document.querySelector('#uiJobs tbody').innerHTML = rows.map(j => {
      const s = j.summary, done = s.committed;
      return `<tr class="static"><td class="small"><strong>${CM.esc(j.job_code)}</strong><div class="tiny muted">${CM.esc(j.input_format)} · ${CM.esc(j.created_by_name || '')}</div></td>
        <td class="small">${CM.esc(j.source_meta.name || '—')}<div class="tiny muted">${CM.esc(CM.titleCase(j.source_meta.provenance_class || ''))}</div></td>
        <td class="small">${done ? `${CM.esc(done.assets)} assets · ${CM.esc(done.connections)} links` : `${CM.esc(s.assets.accepted)} / ${CM.esc(s.assets.received)} valid`}</td>
        <td><span class="badge ${j.status === 'committed' ? 'resolved' : 'pending'}">${CM.esc(CM.titleCase(j.status))}</span>
          ${j.status === 'committed' ? `<div><button class="link-btn tiny" data-rollback="${CM.esc(j.job_code)}">Roll back</button></div>` : ''}</td>
        <td class="small muted">${CM.esc(CM.timeAgo(j.committed_at || j.created_at))}</td></tr>`;
    }).join('') || '<tr class="static"><td colspan="5" class="empty">No imports yet.</td></tr>';
  } catch (e) { CM.fail(e); }
}

async function loadFindings() {
  try {
    const rows = await CM.api('/api/utility/findings');
    $('ufList').innerHTML = rows.map(f => `<div class="finding sev-${CM.esc(f.severity)}">
        <div class="row spread wrap-row"><strong class="small">${CM.esc(f.rule_title)}</strong>
          <span class="badge ${CM.sevClass(f.severity)}">${CM.esc(CM.titleCase(f.severity))}</span></div>
        <div class="small">${CM.esc(f.explanation)}</div>
        <div class="tiny muted">${CM.esc(f.rule_id)} · assets ${f.asset_ids.map(a => CM.esc(a)).join(', ')} · ${CM.esc(CM.titleCase(f.status))}${f.is_synthetic ? ' · synthetic data' : ''}</div>
        ${f.status === 'open' ? `<button class="link-btn tiny" data-fid="${Number(f.id)}" data-st="acknowledged">Acknowledge</button> ·
          <button class="link-btn tiny" data-fid="${Number(f.id)}" data-st="dismissed">Dismiss</button>` : ''}</div>`).join('')
      || '<p class="small muted">No active findings.</p>';
  } catch (e) { CM.fail(e); }
}

async function reviewFinding(e) {
  const b = e.target.closest('[data-fid]');
  if (!b) return;
  try { await CM.api(`/api/utility/findings/${b.dataset.fid}/review`, { method: 'POST', body: { status: b.dataset.st } }); loadFindings(); }
  catch (ex) { CM.fail(ex); }
}

async function loadAudit() {
  try {
    const rows = await CM.api('/api/admin/activity?limit=100');
    $('alCount').textContent = rows.length;
    document.querySelector('#alTable tbody').innerHTML = rows.map(a => `<tr class="static">
      <td class="small muted">${CM.esc(CM.fmtDate(a.created_at))}</td><td class="small">${CM.esc(a.actor || 'CivicMind')}</td>
      <td class="small"><code>${CM.esc(a.action)}</code></td>
      <td class="small">${CM.esc(a.detail || '')}${a.incident_public_id ? ` · <a class="link" href="incident.html?id=${encodeURIComponent(a.incident_id)}">${CM.esc(a.incident_public_id)}</a>` : ''}</td></tr>`).join('')
      || '<tr class="static"><td colspan="4" class="empty">No activity yet.</td></tr>';
  } catch (e) { CM.fail(e); }
}

async function loadStats() {
  try {
    const s = await CM.api('/api/admin/stats');
    $('aUsers').textContent = CM.num(s.users);
    $('aComplaints').textContent = CM.num(s.complaints);
    $('aOpen').textContent = `${CM.num(s.open_complaints)} open · ${s.resolution_rate === null ? '—' : s.resolution_rate + '% resolved'}`;
    $('aIncidents').textContent = CM.num(s.incidents);
    $('aResolutions').textContent = `${CM.num(s.resolutions)} verification records`;
    $('aAssets').textContent = CM.num(s.assets);
    $('aFindings').textContent = `${CM.num(s.open_findings)} open risk findings`;
    $('demoBanner').hidden = !s.demo_database;
    $('resetZone').hidden = !s.demo_database;
  } catch (e) { CM.fail(e); }
}

async function loadUsers() {
  try {
    const rows = await CM.api('/api/admin/users');
    const depts = DEPTS.filter(d => d.is_active).map(d => d.name);
    document.querySelector('#usersTable tbody').innerHTML = rows.map(u => `
      <tr class="static">
        <td><strong>${CM.esc(u.name)}</strong>${u.id === user.id ? ' <span class="tiny muted">(you)</span>' : ''}</td>
        <td class="small muted">${CM.esc(u.email)}</td>
        <td><select class="sm-select" data-f="role" aria-label="Role">${['citizen', 'officer', 'admin'].map(r =>
          `<option value="${r}" ${u.role === r ? 'selected' : ''}>${CM.titleCase(r)}</option>`).join('')}</select></td>
        <td><select class="sm-select" data-f="dept" aria-label="Department"><option value="">—</option>${depts.map(d =>
          `<option ${u.department === d ? 'selected' : ''}>${CM.esc(d)}</option>`).join('')}</select></td>
        <td><input type="checkbox" data-f="active" aria-label="Active" ${u.is_active ? 'checked' : ''} style="width:auto"/></td>
        <td><button class="btn sm" data-uid="${Number(u.id)}">Save</button></td>
      </tr>`).join('') || '<tr class="static"><td colspan="6" class="empty">No users.</td></tr>';
  } catch (e) { CM.fail(e); }
}

async function loadConfig() {
  try {
    const c = await CM.api('/api/admin/config');
    $('cfgNote').textContent = c.note;
    $('cfgCats').innerHTML = c.categories.map(x => `<span class="chip static">${CM.esc(x)}</span>`).join('');
    $('cfgDepts').innerHTML = c.departments.map(x => `<span class="chip static">${CM.esc(x)}</span>`).join('');
    if ($('uDept').options.length <= 1) {
      c.departments.forEach(d => { const o = document.createElement('option'); o.value = d; o.textContent = d; $('uDept').appendChild(o); });
      c.wards.forEach(w => { const o = document.createElement('option'); o.value = w; o.textContent = w; $('uWard').appendChild(o); });
    }
  } catch (e) { CM.fail(e); }
}

function checkBadge(pc) {
  if (!pc || !pc.verdict) return '<span class="muted small">—</span>';
  const cls = pc.verdict === 'passed' ? 'low' : pc.verdict === 'rejected' ? 'critical' : 'medium';
  return `<span class="badge ${cls}" title="${CM.esc((pc.reasons || []).join('; '))}">${CM.esc(CM.checkLabel(pc.verdict))}</span>`;
}

async function loadComplaints() {
  try {
    const rows = await CM.api('/api/admin/complaints');
    $('acCount').textContent = rows.length;
    document.querySelector('#acTable tbody').innerHTML = rows.map(c => `
      <tr class="static">
        <td><strong>${CM.esc(c.public_id)}</strong> ${syntheticTag(c.is_synthetic)}
            <div class="tiny muted">${c.incident ? `<a class="link" href="incident.html?id=${encodeURIComponent(c.incident.id)}">${CM.esc(c.incident.public_id)}</a>` : 'not grouped'}</div></td>
        <td class="small">${CM.esc(c.citizen_name || '—')}<div class="tiny muted">${CM.esc(c.citizen_email || '')}</div></td>
        <td class="small">${CM.esc(c.category)}</td>
        <td class="small">${CM.esc(c.ward || '—')}</td>
        <td>${c.has_photo ? `<img class="thumb sm" alt="Complaint photo" data-auth-src="/api/complaints/${Number(c.id)}/photo"/>` : '—'}</td>
        <td>${checkBadge(c.photo_check)}</td>
        <td><span class="badge ${CM.sClass(c.status)}">${CM.esc(CM.statusText(c.status))}</span>
            ${c.resolved_by_name ? `<div class="tiny muted">✓ ${CM.esc(c.resolved_by_name)}</div>` : ''}</td>
        <td class="small muted">${CM.esc(CM.timeAgo(c.created_at))}</td>
      </tr>`).join('') || '<tr class="static"><td colspan="8" class="empty">No complaints yet.</td></tr>';
    CM.hydrateImages($('acTable'));
  } catch (e) { CM.fail(e); }
}

async function loadResolutions() {
  try {
    const rows = await CM.api('/api/admin/resolutions');
    $('arCount').textContent = rows.length;
    document.querySelector('#arTable tbody').innerHTML = rows.map(r => {
      const img = !r.has_proof ? '—' : r.scope === 'incident' && r.action_id
        ? `<img class="thumb sm" alt="Proof photo" data-auth-src="/api/actions/${Number(r.action_id)}/proof"/>`
        : r.scope === 'complaint' ? `<img class="thumb sm" alt="Proof photo" data-auth-src="/api/complaints/${Number(r.id)}/resolution-photo"/>` : '✓';
      return `
      <tr class="static">
        <td><strong>${r.incident_id ? `<a class="link" href="incident.html?id=${encodeURIComponent(r.incident_id)}">${CM.esc(r.ref)}</a>` : CM.esc(r.ref)}</strong>
            <div class="tiny muted">${CM.esc(r.scope === 'incident' ? 'incident' : 'single complaint')} · ${CM.esc(r.category || '')} · ${CM.esc(r.ward || '')}</div></td>
        <td class="small">${CM.esc(r.officer_name || '—')}</td>
        <td class="small">${CM.esc(CM.titleCase(r.verification_type || ''))}<div>${checkBadge(r.proof_check)}</div></td>
        <td class="num small">${r.before_metric != null ? CM.esc(r.before_metric) + ' → ' + CM.esc(r.after_metric) : '—'}</td>
        <td>${img}</td>
        <td><span class="badge ${r.status === 'verified' ? 'resolved' : 'reopened'}">${r.status === 'verified' ? 'Resolved' : 'Reopened'}</span></td>
        <td class="small muted">${CM.esc(CM.timeAgo(r.verified_at))}</td>
      </tr>`; }).join('') || '<tr class="static"><td colspan="7" class="empty">No resolutions yet.</td></tr>';
    CM.hydrateImages($('arTable'));
  } catch (e) { CM.fail(e); }
}

async function addUser(e) {
  e.preventDefault();
  const err = $('uError'); err.hidden = true;
  const body = { name: $('uName').value.trim(), email: $('uEmail').value.trim(), password: $('uPass').value,
                 role: $('uRole').value, department: $('uDept').value || null, ward: $('uWard').value || null };
  if (body.name.length < 2 || !body.email || body.password.length < 8) {
    err.textContent = 'Name, a valid email and a password of at least 8 characters are required.'; err.hidden = false; return;
  }
  await CM.busy($('uBtn'), 'Creating…', async () => {
    try {
      await CM.api('/api/admin/users', { method: 'POST', body });
      CM.toast(`${CM.titleCase(body.role)} account created`, 'ok');
      $('addUser').reset(); loadUsers(); loadStats();
    } catch (ex) { err.textContent = ex.message + ((ex.reasons || []).length ? ' — ' + ex.reasons.join('; ') : ''); err.hidden = false; }
  });
}

async function importAssets(e) {
  e.preventDefault();
  const err = $('gError'); err.hidden = true;
  const f = $('gFile').files[0], source = $('gSource').value.trim();
  if (!f) { err.textContent = 'Choose a GeoJSON file.'; err.hidden = false; return; }
  if (f.size > 2 * 1024 * 1024) { err.textContent = 'File is larger than 2 MB.'; err.hidden = false; return; }
  if (source.length < 2) { err.textContent = 'Describe the data source.'; err.hidden = false; return; }
  await CM.busy($('gBtn'), 'Importing…', async () => {
    let geojson;
    try { geojson = JSON.parse(await f.text()); }
    catch { err.textContent = 'The file is not valid JSON.'; err.hidden = false; return; }
    try {
      const s = await CM.api('/api/assets/import', { method: 'POST', body: { geojson, data_source: source, is_synthetic: $('gSynthetic').checked } });
      $('importResult').innerHTML = `
        <div class="card tight soft">
          <strong>Import finished</strong>
          <p class="small">${CM.esc(s.imported)} imported · ${CM.esc(s.duplicates.length)} duplicate (skipped) · ${CM.esc(s.rejected.length)} rejected · ${CM.esc(s.total)} total</p>
          ${s.duplicates.length ? `<p class="tiny muted">Duplicates: ${CM.esc(s.duplicates.slice(0, 20).join(', '))}</p>` : ''}
          ${s.rejected.length ? `<ul class="tiny">${s.rejected.slice(0, 20).map(r => `<li>Feature ${CM.esc(r.index)}: ${CM.esc(r.reason)}</li>`).join('')}</ul>` : ''}
        </div>`;
      loadStats();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  });
}

async function resetData() {
  if ($('resetConfirm').value !== 'RESET') return CM.toast('Type RESET to confirm.', 'err');
  await CM.busy($('resetBtn'), 'Resetting…', async () => {
    try {
      await CM.api('/api/admin/reset', { method: 'POST', body: { confirm: 'RESET' } });
      $('resetConfirm').value = '';
      CM.toast('Demo civic data cleared', 'ok'); loadAll();
    } catch (e) { CM.fail(e); }
  });
}
