/* CivicMind — landing + sign-in / registration */
const $ = (id) => document.getElementById(id);

function showError(el, msg) { el.textContent = msg; el.hidden = !msg; }

function setTab(register) {
  $('loginForm').hidden = register;
  $('registerForm').hidden = !register;
  $('tabLogin').classList.toggle('active', !register);
  $('tabRegister').classList.toggle('active', register);
  $('tabLogin').setAttribute('aria-selected', String(!register));
  $('tabRegister').setAttribute('aria-selected', String(register));
}
$('tabLogin').addEventListener('click', () => setTab(false));
$('tabRegister').addEventListener('click', () => setTab(true));
// Header / hero links choose which form is shown when they jump to the sign-in card.
document.querySelectorAll('[data-tab]').forEach(a => a.addEventListener('click', () => setTab(a.dataset.tab === 'register')));

function safeHome(home) {
  // Only follow same-origin, known pages returned by the server.
  return ['/citizen.html', '/dashboard.html', '/admin.html'].includes(home) ? home.slice(1) : 'index.html';
}

$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = $('email').value.trim(), password = $('password').value;
  showError($('loginError'), '');
  if (!email || !password) return showError($('loginError'), 'Enter your email and password.');
  await CM.busy($('loginBtn'), 'Signing in…', async () => {
    try {
      const d = await CM.api('/api/auth/login', { method: 'POST', body: { email, password } });
      CM.setSession(d.token, d.user);
      location.href = safeHome(d.home);
    } catch (err) { showError($('loginError'), err.message); }
  });
});

$('registerForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = { name: $('rName').value.trim(), email: $('rEmail').value.trim(),
                 password: $('rPass').value, ward: $('rWard').value || null };
  showError($('registerError'), '');
  if (body.name.length < 2) return showError($('registerError'), 'Please enter your name.');
  if (!/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(body.email)) return showError($('registerError'), 'Enter a valid email address.');
  if (body.password.length < 8) return showError($('registerError'), 'Password must be at least 8 characters.');
  await CM.busy($('registerBtn'), 'Creating account…', async () => {
    try {
      const d = await CM.api('/api/auth/register', { method: 'POST', body });
      CM.setSession(d.token, d.user);
      location.href = safeHome(d.home);
    } catch (err) { showError($('registerError'), err.message); }
  });
});

(async () => {
  if (new URLSearchParams(location.search).get('expired')) $('expiredNote').hidden = false;

  try {
    const h = await CM.api('/api/health');
    const stat = (id, n) => (window.CMMotion ? CMMotion.countUp($(id), n) : ($(id).textContent = CM.num(n)));
    stat('sComplaints', h.complaints);
    stat('sIncidents', h.incidents);
    stat('sAssets', h.assets);
    $('sAI').textContent = 'TinyFish + Sarvam AI';
    $('demoNote').hidden = !h.demo_database;
  } catch { /* stats are decorative; the form still works */ }

  try {
    const c = await CM.api('/api/config');
    (c.wards || []).forEach(w => { const o = document.createElement('option'); o.value = w.ward; o.textContent = w.ward; $('rWard').appendChild(o); });
  } catch { /* optional field */ }

  // An existing VALID session goes straight to its dashboard.
  const u = CM.getUser();
  if (u && CM.getToken()) {
    try { await CM.api('/api/auth/me'); location.href = CM.homeFor(u.role); }
    catch { CM.clearSession(); }
  }
})();
