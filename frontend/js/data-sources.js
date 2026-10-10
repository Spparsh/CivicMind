/* CivicMind — admin public data sources (TinyFish Search / Fetch / optional Agent).
   Everything from the provider is untrusted: rendered with CM.esc / textContent only,
   external links only when they are http(s), opened with noopener + noreferrer. */
const user = CM.requireAuth(['admin']);
const $ = (id) => document.getElementById(id);
let STATUS = null, CANDIDATES = [];
const selected = new Set();

document.addEventListener('DOMContentLoaded', () => {
  if (!user) return;
  renderNav('data-sources.html', user);
  $('discoverForm').addEventListener('submit', discover);
  $('fetchBtn').addEventListener('click', () => fetchPages({ candidate_ids: [...selected] }));
  $('manualBtn').addEventListener('click', () => {
    const url = $('manualUrl').value.trim();
    if (!url) return CM.toast('Enter a URL first.', 'err');
    fetchPages({ urls: [url] });
  });
  $('sFilter').addEventListener('change', loadSnapshots);
  $('cTable').addEventListener('click', onCandidateClick);
  $('cTable').addEventListener('change', onCandidateCheck);
  $('sTable').addEventListener('click', (e) => {
    const b = e.target.closest('[data-open]');
    if (b) openReview(Number(b.dataset.open));
  });
  $('reviewPanel').addEventListener('click', onReviewClick);
  $('agentForm').addEventListener('submit', runAgent);
  loadStatus(); loadCandidates(); loadSnapshots();
});

const safeHref = (u) => /^https?:\/\//i.test(String(u || '')) ? CM.esc(u) : null;
function extLink(url, label) {
  const href = safeHref(url);
  return href ? `<a class="link" href="${href}" target="_blank" rel="noopener noreferrer">${CM.esc(label || url)}</a>`
              : CM.esc(label || url);
}
function modeBadge(mode) {
  return { live: '<span class="badge low">Live</span>', mock: '<span class="badge synthetic">Mock — fictional</span>' }[mode]
    || '<span class="badge reopened">Not configured</span>';
}
function detailError(e) {
  return e.message + ((e.detail && e.detail.code) ? ` (${e.detail.code})` : '');
}

/* ---------- status ---------- */
async function loadStatus() {
  try {
    STATUS = await CM.api('/api/admin/data-sources/status');
  } catch (e) { return CM.fail(e); }
  const s = STATUS;
  $('modeBadge').outerHTML = `<span id="modeBadge">${modeBadge(s.provider_mode)}</span>`;
  $('notConfigured').hidden = s.configured;
  if (!s.configured) $('notConfigured').textContent = s.message || 'TinyFish is not configured.';
  $('uSearch').textContent = CM.num(s.usage_today.discovery_runs);
  $('uFetch').textContent = CM.num(s.usage_today.fetched_urls);
  $('uFetchCap').textContent = `max ${s.max_fetch_urls} URLs per request`;
  $('uAgent').textContent = CM.num(s.usage_today.agent_runs);
  $('uAgentCap').textContent = s.agent_enabled ? `limit ${s.agent_daily_limit} per day` : 'disabled';
  $('uImport').innerHTML = s.utility_import_available ? '<span class="badge low">Available</span>'
    : '<span class="badge pending">Not installed yet</span>';
  $('maxUrls').textContent = s.max_fetch_urls;
  $('dBtn').disabled = !s.configured;
  if ($('dTypes').children.length === 0) {
    $('dTypes').innerHTML = s.data_types.map(t => `
      <label class="chip"><input type="checkbox" value="${CM.esc(t.key)}" style="width:auto;margin-right:6px"/>${CM.esc(t.label)}</label>`).join('');
  }
  $('agentBadge').textContent = s.agent_enabled ? 'Enabled' : 'Disabled';
  $('agentBadge').className = 'badge ' + (s.agent_enabled ? 'approved' : 'pending');
  $('agentForm').hidden = !(s.agent_enabled && s.configured);
  $('agentInfo').textContent = 'For public pages that only render with JavaScript. Read-only, limited to domains of '
    + 'selected candidates, capped per day, and metered by TinyFish.' + (s.agent_enabled ? ''
    : ' It is turned off (TINYFISH_ENABLE_AGENT=false). Check TinyFish pricing and limits before enabling it on the server.');
}

/* ---------- discover ---------- */
async function discover(e) {
  e.preventDefault();
  const err = $('dError'); err.hidden = true;
  const body = {
    authority: $('dAuthority').value.trim(), region: $('dRegion').value.trim() || null, topic: $('dTopic').value.trim(),
    data_types: [...$('dTypes').querySelectorAll('input:checked')].map(i => i.value),
    include_domains: $('dDomains').value.split(',').map(s => s.trim()).filter(Boolean),
  };
  if (body.authority.length < 2 || body.topic.length < 2) { err.textContent = 'Enter an authority and a topic.'; err.hidden = false; return; }
  await CM.busy($('dBtn'), 'Searching…', async () => {
    try {
      const d = await CM.api('/api/admin/data-sources/discover', { method: 'POST', body });
      CM.toast(`${d.candidates.length} candidate(s) found${d.dropped ? ` · ${d.dropped} unsafe link(s) ignored` : ''}`, 'ok');
      loadCandidates(d.run.id); loadStatus();
    } catch (ex) { err.textContent = detailError(ex); err.hidden = false; }
  });
}

async function loadCandidates(runId) {
  try {
    const d = await CM.api('/api/admin/data-sources/candidates' + (runId ? `?run_id=${encodeURIComponent(runId)}` : ''));
    CANDIDATES = d.candidates;
    renderCandidates();
  } catch (e) { CM.fail(e); }
}

function renderCandidates() {
  $('cCount').textContent = CANDIDATES.length;
  const tb = document.querySelector('#cTable tbody');
  if (!CANDIDATES.length) { tb.innerHTML = '<tr class="static"><td colspan="6" class="empty">No candidates yet. Run a search above.</td></tr>'; updateFetchBtn(); return; }
  tb.innerHTML = CANDIDATES.map(c => {
    const mock = (c.provenance || {}).provider_mode === 'mock';
    return `<tr class="static">
      <td><input type="checkbox" data-cid="${Number(c.id)}" aria-label="Select candidate" style="width:auto"
           ${selected.has(c.id) ? 'checked' : ''} ${c.review_status === 'rejected' ? 'disabled' : ''}/></td>
      <td class="url-cell"><strong class="small">${CM.esc(c.title || c.url)}</strong> ${mock ? '<span class="badge synthetic">Mock</span>' : ''}
        <div class="tiny">${extLink(c.url)}</div>
        ${c.snippet ? `<div class="tiny muted">${CM.esc(c.snippet)}</div>` : ''}</td>
      <td class="small">${CM.esc(c.site_name || '—')}</td>
      <td class="small muted">${CM.esc(c.result_date || '—')}</td>
      <td><span class="badge ${c.review_status === 'selected' ? 'approved' : c.review_status === 'rejected' ? 'reopened' : 'pending'}">${CM.esc(CM.titleCase(c.review_status))}</span></td>
      <td>${c.review_status === 'rejected'
            ? `<button class="link-btn" data-review="${Number(c.id)}" data-status="candidate">Restore</button>`
            : `<button class="link-btn" data-review="${Number(c.id)}" data-status="rejected">Reject</button>`}</td>
    </tr>`;
  }).join('');
  updateFetchBtn();
}

function updateFetchBtn() {
  const max = STATUS ? STATUS.max_fetch_urls : 10;
  $('fetchBtn').textContent = `Fetch selected (${selected.size}/${max})`;
  $('fetchBtn').disabled = selected.size === 0 || selected.size > max || (STATUS && !STATUS.configured);
}

function onCandidateCheck(e) {
  const box = e.target.closest('input[data-cid]');
  if (!box) return;
  const id = Number(box.dataset.cid);
  const max = STATUS ? STATUS.max_fetch_urls : 10;
  if (box.checked && selected.size >= max) { box.checked = false; return CM.toast(`Select at most ${max} pages per fetch.`, 'err'); }
  box.checked ? selected.add(id) : selected.delete(id);
  updateFetchBtn();
}

async function onCandidateClick(e) {
  const b = e.target.closest('[data-review]');
  if (!b) return;
  const id = Number(b.dataset.review);
  try {
    await CM.api(`/api/admin/data-sources/candidates/${id}/review`, { method: 'POST', body: { status: b.dataset.status } });
    selected.delete(id);
    const c = CANDIDATES.find(x => x.id === id); if (c) c.review_status = b.dataset.status;
    renderCandidates();
  } catch (ex) { CM.fail(ex); }
}

/* ---------- fetch ---------- */
async function fetchPages(body) {
  const btn = body.urls ? $('manualBtn') : $('fetchBtn');
  await CM.busy(btn, 'Fetching…', async () => {
    try {
      const d = await CM.api('/api/admin/data-sources/fetch', { method: 'POST', body: { ...body, format: 'markdown' } });
      $('fetchResult').innerHTML = `<div class="card tight" style="margin-top:12px">
        <strong>${CM.esc(d.snapshots.length)} page(s) stored for review</strong> ${modeBadge(d.provider_mode)}
        ${d.errors.length ? `<ul class="tiny" style="margin:8px 0 0">${d.errors.map(x =>
          `<li>${CM.esc(x.url)} — <strong>${CM.esc(x.error)}</strong>${x.status ? ' (HTTP ' + CM.esc(x.status) + ')' : ''}${x.stage === 'safety_check' ? ' · refused before sending' : ''}</li>`).join('')}</ul>` : ''}
      </div>`;
      selected.clear(); $('manualUrl').value = '';
      $('sFilter').value = 'pending_review';
      loadCandidates(); loadSnapshots(); loadStatus();
    } catch (ex) { CM.toast(detailError(ex), 'err'); }
  });
}

/* ---------- snapshots & review ---------- */
async function loadSnapshots() {
  const f = $('sFilter').value;
  try {
    const rows = await CM.api('/api/admin/data-sources/snapshots' + (f ? `?review_status=${encodeURIComponent(f)}` : ''));
    $('sCount').textContent = rows.length;
    const tb = document.querySelector('#sTable tbody');
    tb.innerHTML = rows.map(s => `<tr class="static">
      <td class="url-cell"><strong class="small">${CM.esc(s.title || s.url)}</strong> ${s.provider_mode === 'mock' ? '<span class="badge synthetic">Mock</span>' : ''}
        <div class="tiny">${extLink(s.final_url || s.url)}</div></td>
      <td class="small">${s.method === 'tinyfish_agent' ? 'Agent' : 'Fetch'}</td>
      <td>${s.fetch_status === 'ok' ? `<span class="badge low">${CM.esc(CM.num(s.content_chars))} chars</span>`
            : `<span class="badge critical">${CM.esc(s.error_code || 'error')}</span>`}
          ${(s.warnings || []).map(w => `<div class="tiny muted">⚠ ${CM.esc(CM.titleCase(w))}</div>`).join('')}</td>
      <td><span class="badge ${s.review_status === 'reviewed' ? 'resolved' : s.review_status === 'rejected' ? 'reopened' : 'pending'}">${CM.esc(CM.titleCase(s.review_status))}</span></td>
      <td class="small muted">${CM.esc(CM.timeAgo(s.fetched_at))}</td>
      <td><button class="btn sm" data-open="${Number(s.id)}">Open</button></td>
    </tr>`).join('') || '<tr class="static"><td colspan="6" class="empty">Nothing here.</td></tr>';
  } catch (e) { CM.fail(e); }
}

async function openReview(id) {
  const panel = $('reviewPanel');
  let s;
  try { s = await CM.api(`/api/admin/data-sources/snapshots/${id}`); } catch (e) { return CM.fail(e); }
  const c = s.candidate;
  panel.hidden = false;
  panel.innerHTML = `
    <div class="card-h"><h3>Review: ${CM.esc(s.title || s.url)}</h3>
      <button class="btn sm ghost" data-act="close">Close</button></div>
    <div class="notice warn">${CM.esc(s.note)}</div>
    <div class="grid g2">
      <div>
        <div class="kv"><span class="muted small">Requested URL</span><span class="small url-cell">${extLink(s.url)}</span></div>
        <div class="kv"><span class="muted small">Final URL</span><span class="small url-cell">${extLink(s.final_url || '—')}</span></div>
        <div class="kv"><span class="muted small">Method</span><span class="small">${s.method === 'tinyfish_agent' ? 'TinyFish Agent' : 'TinyFish Fetch'} ${modeBadge(s.provider_mode)}</span></div>
        <div class="kv"><span class="muted small">Fetched</span><span class="small">${CM.esc(CM.fmtDate(s.fetched_at))}</span></div>
      </div>
      <div>
        <div class="kv"><span class="muted small">Authority</span><span class="small">${CM.esc(c ? c.authority : '— (URL entered directly)')}</span></div>
        <div class="kv"><span class="muted small">Discovery query</span><span class="small">${CM.esc(c ? c.discovery_query : '—')}</span></div>
        <div class="kv"><span class="muted small">Content</span><span class="small">${CM.esc(CM.num(s.content_chars))} chars${s.content_truncated ? ' (truncated)' : ''} · SHA-256 ${CM.esc((s.content_sha256 || '').slice(0, 12))}…</span></div>
        <div class="kv"><span class="muted small">Review</span><span class="small">${CM.esc(CM.titleCase(s.review_status))}${s.review_note ? ' — ' + CM.esc(s.review_note) : ''}</span></div>
      </div>
    </div>
    ${(s.warnings || []).length ? `<p class="small text-red">Warnings: ${s.warnings.map(w => CM.esc(CM.titleCase(w))).join(', ')}</p>` : ''}
    ${s.fetch_status === 'ok'
      ? `<h4 style="margin:16px 0 6px">Extracted content (shown as plain text)</h4><pre class="content-pre" id="contentPre"></pre>`
      : `<p class="small text-red">This fetch failed (${CM.esc(s.error_code || 'error')}${s.error_status ? ', HTTP ' + CM.esc(s.error_status) : ''}). There is no content to review.</p>`}
    ${s.fetch_status === 'ok' ? `
      <div class="field" style="margin-top:12px"><label class="f" for="rNote">Review note</label>
        <input id="rNote" maxlength="500" placeholder="e.g. Official PMC page; layer list matches the published map"/></div>
      <div class="row wrap-row">
        <button class="btn green" data-act="approve" data-id="${Number(s.id)}">Approve as reviewed source</button>
        <button class="btn red" data-act="reject" data-id="${Number(s.id)}">Reject</button>
        ${s.review_status === 'reviewed' ? `<button class="btn" data-act="meta" data-id="${Number(s.id)}">Show import source metadata</button>` : ''}
      </div>
      <div id="metaBox"></div>` : ''}`;
  const pre = $('contentPre');
  if (pre) pre.textContent = s.content_text || '(empty)';
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function onReviewClick(e) {
  const b = e.target.closest('[data-act]');
  if (!b) return;
  const id = Number(b.dataset.id);
  if (b.dataset.act === 'close') { $('reviewPanel').hidden = true; return; }
  if (b.dataset.act === 'meta') {
    try {
      const m = await CM.api(`/api/admin/data-sources/snapshots/${id}/source-metadata`);
      $('metaBox').innerHTML = `<div class="card tight soft" style="margin-top:12px">
        <strong>Import source metadata</strong> <span class="badge">${CM.esc(m.provenance_class)}</span>
        <p class="tiny muted">${CM.esc(m.next_step)}</p><pre class="content-pre" id="metaPre"></pre>
        ${m.utility_import_available ? `<a class="btn sm primary" style="margin-top:10px" href="admin.html?snapshot=${Number(m.snapshot_id)}#utilImport">Use as the source of a utility import →</a>` : ''}</div>`;
      $('metaPre').textContent = JSON.stringify(m, null, 2);
    } catch (ex) { CM.toast(detailError(ex), 'err'); }
    return;
  }
  const decision = b.dataset.act === 'approve' ? 'reviewed' : 'rejected';
  await CM.busy(b, 'Saving…', async () => {
    try {
      await CM.api(`/api/admin/data-sources/snapshots/${id}/review`, { method: 'POST',
        body: { decision, note: ($('rNote') && $('rNote').value.trim()) || null } });
      CM.toast(decision === 'reviewed' ? 'Source approved for the import step' : 'Source rejected', 'ok');
      loadSnapshots(); openReview(id);
    } catch (ex) { CM.toast(detailError(ex), 'err'); }
  });
}

/* ---------- agent ---------- */
async function runAgent(e) {
  e.preventDefault();
  const err = $('aError'); err.hidden = true;
  const body = { url: $('aUrl').value.trim(), extract: $('aExtract').value.trim() };
  if (!body.url || body.extract.length < 10) { err.textContent = 'Enter a URL and describe what to read (10+ characters).'; err.hidden = false; return; }
  await CM.busy($('aBtn'), 'Running (up to a few minutes)…', async () => {
    try {
      const d = await CM.api('/api/admin/data-sources/agent-extract', { method: 'POST', body });
      CM.toast(`Agent run ${d.run.status.toLowerCase()} · ${d.usage.used_today}/${d.usage.daily_limit} today`, d.run.status === 'COMPLETED' ? 'ok' : 'err');
      $('sFilter').value = ''; loadSnapshots(); loadStatus(); openReview(d.snapshot.id);
    } catch (ex) { err.textContent = detailError(ex); err.hidden = false; }
  });
}
