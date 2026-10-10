/* ============================================================
   CivicMind — shared shell: navbar, notification bell.
   ============================================================ */
const NAV_LINKS = {
  citizen: [['citizen.html', 'Report & track'], ['map.html', 'My map']],
  officer: [['dashboard.html', 'Operations'], ['map.html', 'City map'], ['assets.html', 'Infrastructure']],
  admin:   [['admin.html', 'Admin'], ['data-sources.html', 'Data sources'], ['dashboard.html', 'Operations'],
            ['map.html', 'City map'], ['assets.html', 'Infrastructure']],
};

function renderNav(active, user) {
  const nav = document.getElementById('nav');
  if (!nav) return;
  const links = (NAV_LINKS[user.role] || []).map(([href, label]) =>
    `<a class="${href === active ? 'active' : ''}" href="${href}"${href === active ? ' aria-current="page"' : ''}>${CM.esc(label)}</a>`).join('');
  const initials = (user.name || '?').split(/\s+/).map(w => w[0] || '').slice(0, 2).join('').toUpperCase();
  nav.innerHTML = `
    <div class="wrap nav-inner">
      <a class="brand" href="${CM.homeFor(user.role)}">
        <img src="assets/logo.svg" alt=""/><span class="brand-name">Civic<span class="brand-accent">Mind</span></span>
      </a>
      <button class="btn ghost sm nav-toggle" id="navToggle" aria-expanded="false" aria-controls="navLinks">☰ Menu</button>
      <nav class="nav-links" id="navLinks">${links}</nav>
      <div class="nav-right">
        <button class="btn ghost bell" id="bellBtn" aria-label="Notifications">
          🔔<span class="dot" id="bellDot" hidden>0</span>
        </button>
        <div class="user-chip">
          <div class="avatar" aria-hidden="true">${CM.esc(initials)}</div>
          <div class="user-meta">
            <div class="user-name">${CM.esc(user.name)}</div>
            <div class="tiny muted">${CM.esc(CM.titleCase(user.role))}</div>
          </div>
        </div>
        <button class="btn sm ghost" id="logoutBtn">Sign out</button>
      </div>
    </div>`;
  document.getElementById('logoutBtn').addEventListener('click', CM.logout);
  document.getElementById('bellBtn').addEventListener('click', toggleBell);
  const toggle = document.getElementById('navToggle');
  toggle.addEventListener('click', () => {
    const open = nav.classList.toggle('open');
    toggle.setAttribute('aria-expanded', String(open));
  });
  refreshBell();
  setInterval(() => { if (!document.hidden) refreshBell(); }, 20000);
}

/* ---------- notifications bell ---------- */
const NOTE_ICON = { incident: '⚠️', resolved: '✅', approved: '👍', in_progress: '🔧', info: '📩' };

async function refreshBell() {
  try {
    const d = await CM.api('/api/notifications?unread=1');
    const dot = document.getElementById('bellDot');
    if (dot) { dot.hidden = !d.unread; dot.textContent = d.unread > 9 ? '9+' : String(d.unread); }
  } catch { /* the page shows its own errors */ }
}

function noteItem(n) {
  const item = document.createElement('div');
  item.className = 'list-item static';
  const icon = document.createElement('div');
  icon.className = 'li-icon'; icon.textContent = NOTE_ICON[n.type] || '📩';
  const body = document.createElement('div');
  body.className = 'grow';
  const t = document.createElement('div'); t.className = 'li-title'; t.textContent = n.title;
  const m = document.createElement('div'); m.className = 'small muted'; m.textContent = n.message || '';
  const w = document.createElement('div'); w.className = 'tiny muted'; w.textContent = CM.timeAgo(n.created_at);
  body.append(t, m, w);
  item.append(icon, body);
  return item;
}

async function toggleBell() {
  const existing = document.getElementById('bellPanel');
  if (existing) { existing.remove(); return; }
  let d;
  try { d = await CM.api('/api/notifications'); } catch (e) { return CM.fail(e); }
  const panel = document.createElement('div');
  panel.id = 'bellPanel';
  panel.className = 'card bell-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Notifications');
  panel.innerHTML = '<div class="card-h"><h3>Notifications</h3><button class="btn sm ghost" id="markRead">Mark all read</button></div>';
  const list = document.createElement('div');
  list.className = 'list';
  if (d.items.length) d.items.forEach(n => list.appendChild(noteItem(n)));
  else list.innerHTML = '<div class="empty">No notifications yet.</div>';
  panel.appendChild(list);
  document.body.appendChild(panel);
  document.getElementById('markRead').addEventListener('click', async () => {
    try { await CM.api('/api/notifications/read', { method: 'POST' }); CM.toast('All marked as read', 'ok'); refreshBell(); panel.remove(); }
    catch (e) { CM.fail(e); }
  });
  setTimeout(() => document.addEventListener('click', closeBellOnce), 50);
}
function closeBellOnce(ev) {
  const p = document.getElementById('bellPanel');
  if (p && !p.contains(ev.target) && !ev.target.closest('#bellBtn')) {
    p.remove(); document.removeEventListener('click', closeBellOnce);
  } else if (!p) document.removeEventListener('click', closeBellOnce);
}

/* ---------- small shared renderers ---------- */
function emptyState(text) { return `<div class="empty">${CM.esc(text)}</div>`; }
function syntheticTag(flag) { return flag ? '<span class="badge synthetic" title="Fictional demo record">Synthetic</span>' : ''; }
