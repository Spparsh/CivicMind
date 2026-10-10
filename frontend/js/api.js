/* ============================================================
   CivicMind — shared client library
   API wrapper, session, toasts, safe rendering and formatting helpers.

   SAFETY RULE for every page: any value that came from the server or the
   user is inserted with CM.esc() (text / quoted attributes) or textContent.
   Never put such values inside inline event handlers — use data-* attributes
   and addEventListener instead.
   ============================================================ */
const CM = (() => {
  const TOKEN_KEY = 'cm_token';
  const USER_KEY  = 'cm_user';
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
  };

  const getToken = () => store.get(TOKEN_KEY);
  const getUser  = () => { try { return JSON.parse(store.get(USER_KEY)); } catch { return null; } };
  const setSession = (token, user) => { store.set(TOKEN_KEY, token); store.set(USER_KEY, JSON.stringify(user)); };
  const clearSession = () => { store.del(TOKEN_KEY); store.del(USER_KEY); };

  function expire() {
    clearSession();
    if (!/index\.html$|\/$/.test(location.pathname)) location.href = 'index.html?expired=1';
  }

  function messageFrom(data, status) {
    const d = data && data.detail;
    if (typeof d === 'string') return d;
    if (d && typeof d.message === 'string') return d.message;
    if (status === 413) return 'That upload is too large.';
    if (status === 429) return 'Too many attempts. Please wait and try again.';
    if (status >= 500) return 'The server had a problem. Please try again.';
    return 'Request failed (' + status + ').';
  }

  /** JSON API call. `raw` sends a Blob/File as-is (used for photo/video uploads). */
  async function api(path, { method = 'GET', body, raw } = {}) {
    const headers = {};
    const token = getToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (raw !== undefined) headers['Content-Type'] = raw.type || 'application/octet-stream';
    let res;
    try {
      res = await fetch(path.replace(/^\/+/, ''), { method, headers,
        body: raw !== undefined ? raw : (body !== undefined ? JSON.stringify(body) : undefined) });
    } catch {
      const err = new Error('Network error — check your connection and try again.');
      err.status = 0; throw err;
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    if (!res.ok) {
      if (res.status === 401 && token && !path.includes('/auth/')) expire();
      const err = new Error(messageFrom(data, res.status));
      err.status = res.status;
      err.detail = (data && data.detail) || null;
      err.reasons = (err.detail && Array.isArray(err.detail.reasons)) ? err.detail.reasons : [];
      throw err;
    }
    return data;
  }

  /** Load a protected image (evidence is never public) into an <img>. */
  async function authImage(img, url) {
    try {
      const res = await fetch(url.replace(/^\/+/, ''), { headers: { Authorization: 'Bearer ' + (getToken() || '') } });
      if (!res.ok) throw new Error();
      const blob = await res.blob();
      img.src = URL.createObjectURL(blob);
      img.classList.remove('img-missing');
      if (img.matches && img.matches('img.thumb')) {      // reachable by keyboard; Enter opens the viewer
        img.tabIndex = 0; img.setAttribute('role', 'button');
        img.setAttribute('aria-label', (img.alt || 'Photo') + ' — open full size');
      }
    } catch {
      img.alt = 'Photo unavailable'; img.classList.add('img-missing');
    }
  }
  /* ---------- photo viewer: tap any loaded thumbnail to see it full size ---------- */
  let lb = null, lbList = [], lbIndex = 0, lbReturn = null;
  function lightboxShow(i) {
    lbIndex = (i + lbList.length) % lbList.length;
    const img = lbList[lbIndex];
    lb.querySelector('img').src = img.src;
    lb.querySelector('img').alt = img.alt || 'Photo';
    lb.querySelector('.lb-cap').textContent = (img.getAttribute('title') || img.alt || '') +
      (lbList.length > 1 ? `  (${lbIndex + 1} / ${lbList.length})` : '');
    lb.querySelectorAll('.lb-nav').forEach(b => { b.hidden = lbList.length < 2; });
  }
  function lightboxClose() {
    if (!lb) return;
    lb.remove(); lb = null; document.removeEventListener('keydown', lightboxKeys);
    if (lbReturn) lbReturn.focus();
  }
  function lightboxKeys(e) {
    if (e.key === 'Escape') lightboxClose();
    else if (e.key === 'ArrowRight') lightboxShow(lbIndex + 1);
    else if (e.key === 'ArrowLeft') lightboxShow(lbIndex - 1);
  }
  function openLightbox(img) {
    const group = img.closest('.media-row, .row, .complaint-body, article, .report-item') || document.body;
    lbList = [...group.querySelectorAll('img.thumb')].filter(x => x.src && !x.classList.contains('img-missing'));
    if (!lbList.includes(img)) lbList = [img];
    lbReturn = img;
    lb = document.createElement('div');
    lb.className = 'lightbox';
    lb.setAttribute('role', 'dialog'); lb.setAttribute('aria-modal', 'true'); lb.setAttribute('aria-label', 'Photo viewer');
    lb.innerHTML = `<button type="button" class="lb-close" aria-label="Close">✕</button>
      <button type="button" class="lb-nav lb-prev" aria-label="Previous photo">‹</button>
      <figure><img alt=""/><figcaption class="lb-cap"></figcaption></figure>
      <button type="button" class="lb-nav lb-next" aria-label="Next photo">›</button>`;
    lb.addEventListener('click', (e) => {
      if (e.target.closest('.lb-prev')) lightboxShow(lbIndex - 1);
      else if (e.target.closest('.lb-next')) lightboxShow(lbIndex + 1);
      else if (e.target.closest('.lb-close') || e.target === lb) lightboxClose();
    });
    let x0 = null;      // swipe on phones
    lb.addEventListener('touchstart', (e) => { x0 = e.touches[0].clientX; }, { passive: true });
    lb.addEventListener('touchend', (e) => {
      if (x0 === null) return;
      const dx = e.changedTouches[0].clientX - x0; x0 = null;
      if (Math.abs(dx) > 50) lightboxShow(lbIndex + (dx < 0 ? 1 : -1));
    });
    document.body.appendChild(lb);
    document.addEventListener('keydown', lightboxKeys);
    lightboxShow(lbList.indexOf(img));
    lb.querySelector('.lb-close').focus();
  }
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('click', (e) => {
      const img = e.target && e.target.closest ? e.target.closest('img.thumb') : null;
      if (img && img.src && !img.classList.contains('img-missing') && !img.closest('.lightbox')) {
        e.preventDefault(); openLightbox(img);
      }
    });
    document.addEventListener('keydown', (e) => {
      const img = e.target && e.target.matches && e.target.matches('img.thumb') ? e.target : null;
      if (img && (e.key === 'Enter' || e.key === ' ') && img.src && !img.classList.contains('img-missing')) {
        e.preventDefault(); openLightbox(img);
      }
    });
  }

  /** Thumbnails for a report's photos and videos (private files: call hydrateImages afterwards). */
  function mediaThumbs(c, size = '') {
    const list = Array.isArray(c.media) ? c.media.slice() : [];
    if (c.has_photo && !list.some(m => m.is_main_photo)) {
      list.unshift({ kind: 'photo', url: `/api/complaints/${Number(c.id)}/photo` });
    }
    return list.map(m => m.kind === 'video'
      ? `<video class="thumb ${esc(size)}" controls preload="metadata" playsinline aria-label="Report video" data-auth-src="${esc(m.url)}"></video>`
      : `<img class="thumb ${esc(size)}" alt="Report photo" data-auth-src="${esc(m.url)}"/>`).join('');
  }
  /** Hydrate every <img|audio|video data-auth-src="..."> under root (private media). */
  function hydrateImages(root = document) {
    root.querySelectorAll('img[data-auth-src], audio[data-auth-src], video[data-auth-src]').forEach(el => {
      const u = el.getAttribute('data-auth-src'); el.removeAttribute('data-auth-src'); authImage(el, u);
    });
  }

  /* ---------- toast ---------- */
  function toast(msg, type = '', reasons) {
    let box = document.getElementById('toasts');
    if (!box) { box = document.createElement('div'); box.id = 'toasts'; box.setAttribute('role', 'status'); document.body.appendChild(box); }
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    el.textContent = msg;
    if (reasons && reasons.length) {
      const ul = document.createElement('ul');
      reasons.slice(0, 4).forEach(r => { const li = document.createElement('li'); li.textContent = r; ul.appendChild(li); });
      el.appendChild(ul);
    }
    box.appendChild(el);
    const ttl = type === 'err' ? 6000 : 3600;
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = '.3s'; }, ttl);
    setTimeout(() => el.remove(), ttl + 400);
  }
  const fail = (e) => toast(e.message, 'err', e.reasons);

  /* ---------- busy buttons ---------- */
  async function busy(btn, label, fn) {
    if (!btn || btn.disabled) return;
    const original = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> ' + esc(label);
    try { return await fn(); }
    finally { btn.disabled = false; btn.innerHTML = original; }
  }

  /* ---------- auth guards ---------- */
  const homeFor = (role) => ({ citizen: 'citizen.html', officer: 'dashboard.html', admin: 'admin.html' }[role] || 'index.html');
  function requireAuth(roles) {
    const u = getUser();
    if (!u || !getToken()) { location.href = 'index.html'; return null; }
    if (roles && roles.length && !roles.includes(u.role)) { location.href = homeFor(u.role); return null; }
    // The cached role only chooses which page to draw. Confirm it with the server so a
    // changed role moves the user to the right portal (the API enforces access regardless).
    api('/api/auth/me').then((me) => {
      if (me && me.role && me.role !== u.role) { setSession(getToken(), { ...u, ...me }); location.href = homeFor(me.role); }
    }).catch(() => { /* 401 is handled by api(); other errors are shown by the page */ });
    return u;
  }
  async function logout() { clearSession(); location.href = 'index.html'; }

  /* ---------- files ---------- */
  const MAX_UPLOAD = 5 * 1024 * 1024;
  function fileToDataURL(file) {
    return new Promise((resolve, reject) => {
      if (!file) return reject(new Error('No file selected.'));
      if (!/^image\/(jpeg|png|webp)$/.test(file.type)) return reject(new Error('Use a JPEG, PNG or WebP photo.'));
      if (file.size > MAX_UPLOAD) return reject(new Error('Photo is larger than 5 MB.'));
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = () => reject(new Error('Could not read the file.'));
      r.readAsDataURL(file);
    });
  }

  /* ---------- formatting ---------- */
  const esc = (s) => String(s ?? '').replace(/[&<>"'`]/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','`':'&#96;' }[c]));
  const pClass = (label) => ({ Critical:'critical', High:'high', Medium:'medium', Low:'low' }[label] || '');
  const sevClass = (s) => ({ critical:'critical', high:'high', medium:'medium', low:'low' }[s] || '');
  const sClass = (s) => ({ active:'active', resolved:'resolved', in_progress:'in_progress', reopened:'reopened',
    pending:'pending', approved:'approved', completed:'completed', clustered:'active', submitted:'pending',
    rejected:'reopened', cancelled:'pending', open:'reopened', acknowledged:'approved', dismissed:'pending', cleared:'resolved' }[s] || '');
  const STATUS_TEXT = { submitted: 'Submitted', clustered: 'Grouped into incident', in_progress: 'In progress',
    resolved: 'Resolved', rejected: 'Rejected', active: 'Awaiting plan approval', reopened: 'Reopened',
    pending: 'Awaiting approval', approved: 'Approved', completed: 'Completed', cancelled: 'Cancelled',
    open: 'Open', acknowledged: 'Acknowledged', dismissed: 'Dismissed', cleared: 'Cleared' };
  const statusText = (s) => STATUS_TEXT[s] || titleCase(s);
  const titleCase = (s) => String(s || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  const num = (n) => (n === null || n === undefined) ? '—' : Number(n).toLocaleString('en-IN');
  const engineLabel = (e) => e === 'sarvam' ? 'AI-assisted (Sarvam)' : 'Rule-based';
  const checkLabel = (v) => ({ passed: 'Passed automated checks', needs_review: 'Needs review', rejected: 'Rejected' }[v] || '—');

  function parseTs(ts) {
    if (!ts) return null;
    const s = String(ts).replace(' ', 'T');
    const d = new Date(/Z|[+-]\d\d:?\d\d$/.test(s) ? s : s + 'Z');
    return isNaN(d) ? null : d;
  }
  function timeAgo(ts) {
    const d = parseTs(ts); if (!d) return '';
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    if (s < 604800) return Math.floor(s / 86400) + 'd ago';
    return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
  }
  function fmtDate(ts) {
    const d = parseTs(ts); if (!d) return '—';
    return d.toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  }
  const fmtDay = (ts) => { const d = parseTs(ts); return d ? d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) : '—'; };

  return { api, getToken, getUser, setSession, clearSession, toast, fail, busy, requireAuth, homeFor, logout,
           authImage, hydrateImages, mediaThumbs, fileToDataURL, esc, pClass, sevClass, sClass, statusText, titleCase, num,
           engineLabel, checkLabel, timeAgo, fmtDate, fmtDay, parseTs };
})();

/* ---- friendly notice if the HTML is opened directly (file://) ---- */
if (location.protocol === 'file:') {
  document.addEventListener('DOMContentLoaded', () => {
    const b = document.createElement('div');
    b.className = 'file-banner';
    b.textContent = 'CivicMind must be opened through its server. Run "python -m uvicorn main:app --port 8000" ' +
      'inside the backend folder, then visit http://localhost:8000.';
    document.body.prepend(b);
  });
}
