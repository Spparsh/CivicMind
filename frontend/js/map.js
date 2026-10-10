/* CivicMind — city map. Officers/admins see everything; citizens see their own
   reports and generalised incident areas (the API enforces this too). */
const user = CM.requireAuth();
const isStaff = !!user && ['officer', 'admin'].includes(user.role);
const $ = (id) => document.getElementById(id);
const SEV_COLOR = (s) => s >= 4 ? '#D9342B' : s === 3 ? '#E0A106' : '#2E9E5B';
const PRIO_COLOR = { Critical: '#D9342B', High: '#E8551E', Medium: '#E0A106', Low: '#2E9E5B' };
let map, layers = {}, data = { complaints: [], incidents: [], assets: [], findings: [] };

document.addEventListener('DOMContentLoaded', async () => {
  if (!user) return;
  renderNav('map.html', user);
  document.querySelectorAll('.staff-only').forEach(el => { el.hidden = !isStaff; });
  $('mapTitle').textContent = isStaff ? 'City map' : 'My reports & nearby incidents';
  $('scopeNote').textContent = isStaff ? 'All reports, incidents, assets and open risk findings.'
    : 'You see your own reports. Incidents are shown as approximate areas without other citizens\' details.';
  if (typeof L === 'undefined') { $('map').innerHTML = emptyState('The map library could not load. Check your connection.'); return; }
  let center = { lat: 18.5204, lng: 73.8567 };
  try { center = (await CM.api('/api/config')).city_center; } catch { /* default */ }
  map = L.map('map', { zoomControl: false }).setView([center.lat, center.lng], 13);
  L.control.zoom({ position: 'topright' }).addTo(map);    // keeps the filter panel (top-left) clear
  initLegendToggle();
  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap contributors', maxZoom: 19 }).addTo(map);
  let tileErrors = 0;
  tiles.on('tileerror', () => { if (++tileErrors > 4) $('tileWarn').hidden = false; });
  ['incidents', 'complaints', 'assets', 'findings'].forEach(k => { layers[k] = L.layerGroup().addTo(map); });

  ['fCategory', 'fStatus', 'layComplaints', 'layIncidents', 'layAssets', 'layFindings', 'layPriority']
    .forEach(id => $(id).addEventListener('change', render));
  $('refreshBtn').addEventListener('click', () => CM.busy($('refreshBtn'), 'Loading…', loadAll));
  $('panelToggle').addEventListener('click', () => {
    const body = $('panelBody'); body.hidden = !body.hidden;
    $('panelToggle').textContent = body.hidden ? 'Show' : 'Hide';
    $('panelToggle').setAttribute('aria-expanded', String(!body.hidden));
  });
  await loadAll(true);
  if (isStaff && typeof UtilityLayer !== 'undefined') UtilityLayer.mount(map);
  setInterval(() => { if (!document.hidden) loadAll(); }, 60000);
});

/* Legend: collapsed by default so it never covers the filters; the choice is remembered. */
function initLegendToggle() {
  const btn = $('legendToggle'), body = $('legendBody'), box = $('mapLegend');
  let open = false;
  try { open = localStorage.getItem('cm_legend_open') === '1'; } catch { /* private mode */ }
  const set = (v) => {
    open = v; body.hidden = !v; box.classList.toggle('is-open', v); btn.setAttribute('aria-expanded', String(v));
    try { localStorage.setItem('cm_legend_open', v ? '1' : '0'); } catch { /* private mode */ }
  };
  set(open);
  btn.addEventListener('click', () => set(!open));
}

async function loadAll(fit) {
  try {
    const reqs = [CM.api('/api/map/complaints'), CM.api('/api/map/incidents')];
    if (isStaff) reqs.push(CM.api('/api/map/assets'), CM.api('/api/risk/findings'));
    const [c, i, a = [], f = []] = await Promise.all(reqs);
    data = { complaints: c, incidents: i, assets: a, findings: f };
    const cats = [...new Set([...c.map(x => x.category), ...i.map(x => x.category)])].sort();
    const sel = $('fCategory'), current = sel.value;
    sel.innerHTML = '<option value="">All categories</option>' + cats.map(x => `<option>${CM.esc(x)}</option>`).join('');
    sel.value = cats.includes(current) ? current : '';
    $('updated').textContent = 'Updated ' + new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
    render();
    if (fit) fitAll();
  } catch (e) { $('updated').textContent = 'Could not load map data'; CM.fail(e); }
}

function fitAll() {
  const pts = [...data.complaints.map(c => [c.latitude, c.longitude]), ...data.incidents.map(i => [i.centroid_lat, i.centroid_lng]),
               ...data.assets.map(a => [a.latitude, a.longitude])];
  if (pts.length) map.fitBounds(pts, { padding: [40, 40], maxZoom: 16 });
}

function popup(title, lines, link) {
  // Built from escaped values only; short by design.
  return `<div class="pop"><h4>${CM.esc(title)}</h4>${lines.map(l => `<div class="small">${l}</div>`).join('')}
    ${link ? `<a class="btn sm primary" style="margin-top:8px" href="${link.href}">${CM.esc(link.label)}</a>` : ''}</div>`;
}

function render() {
  Object.values(layers).forEach(l => l.clearLayers());
  const fc = $('fCategory').value, fst = $('fStatus').value;
  const onlyHigh = isStaff && $('layPriority').checked;
  let nc = 0, ni = 0, na = 0, nf = 0;

  if ($('layComplaints').checked) {
    data.complaints.filter(c => (!fc || c.category === fc) &&
      (!fst || (fst === 'open' ? !['resolved', 'rejected'].includes(c.status) : c.status === fst)))
      .forEach(c => {
        nc++;
        L.circleMarker([c.latitude, c.longitude], { radius: 7, color: '#fff', weight: 2, fillColor: SEV_COLOR(c.severity), fillOpacity: .95 })
          .bindPopup(popup(c.public_id, [`${CM.esc(c.category)} · ${CM.esc(c.ward || '')}`,
            `<span class="badge ${CM.sClass(c.status)}">${CM.esc(CM.statusText(c.status))}</span> ${syntheticTag(c.is_synthetic)}`],
            c.incident_id ? { href: 'incident.html?id=' + encodeURIComponent(c.incident_id), label: 'Open incident' } : null))
          .addTo(layers.complaints);
      });
  }

  if ($('layIncidents').checked) {
    data.incidents.filter(i => (!fc || i.category === fc) && (!onlyHigh || ['High', 'Critical'].includes(i.priority_label)))
      .forEach(i => {
        ni++;
        const col = PRIO_COLOR[i.priority_label] || '#E8551E';
        L.circle([i.centroid_lat, i.centroid_lng], { radius: isStaff ? Math.max(60, (i.spread_km || 0) * 1000) : 250,
          color: col, weight: 1.5, fillColor: col, fillOpacity: i.status === 'resolved' ? .04 : .12, dashArray: i.status === 'resolved' ? '4 4' : null })
          .addTo(layers.incidents);
        const link = isStaff || i.linked ? { href: 'incident.html?id=' + encodeURIComponent(i.id), label: 'Open incident' } : null;
        L.marker([i.centroid_lat, i.centroid_lng], { icon: L.divIcon({ className: '', iconSize: [44, 24],
            html: `<div class="inc-pin" style="background:${col}">⚠ ${Number(i.report_count) || 0}</div>` }) })
          .bindPopup(popup(i.title, [
            `${isStaff ? CM.esc(i.public_id) + ' · ' : ''}${CM.esc(i.report_count)} reports · ${CM.esc(i.ward || '')}`,
            `<span class="badge ${CM.pClass(i.priority_label)}">${CM.esc(i.priority_label)}${isStaff ? ' · ' + CM.esc(i.priority_score) : ''}</span>
             <span class="badge ${CM.sClass(i.status)}">${CM.esc(CM.statusText(i.status))}</span>`,
            isStaff ? '' : '<span class="tiny muted">Approximate area</span>'], link))
          .addTo(layers.incidents);
      });
  }

  if (isStaff && $('layAssets').checked) {
    data.assets.forEach(a => {
      na++;
      const col = a.criticality === 'critical' ? '#7B4FD1' : '#2F6FDB';
      L.marker([a.latitude, a.longitude], { icon: L.divIcon({ className: '', iconSize: [16, 16],
          html: `<div class="asset-pin" style="background:${col}"></div>` }) })
        .bindPopup(popup(a.asset_code, [`${CM.esc(a.type_label)} · ${CM.esc(CM.titleCase(a.criticality))} ${syntheticTag(a.is_synthetic)}`,
          `${CM.esc(a.open_complaints)} open report(s) · ${CM.esc(a.open_findings)} open finding(s)`,
          a.inspection_overdue ? '<span class="badge critical">Inspection overdue</span>' : ''],
          { href: 'assets.html?id=' + encodeURIComponent(a.id), label: 'Asset record' }))
        .addTo(layers.assets);
    });
  }

  if (isStaff && $('layFindings').checked) {
    data.findings.forEach(f => {
      const lat = f.asset_lat ?? f.centroid_lat, lng = f.asset_lng ?? f.centroid_lng;
      if (lat == null || lng == null) return;
      nf++;
      L.marker([lat, lng], { icon: L.divIcon({ className: '', iconSize: [22, 22], iconAnchor: [0, 22],
          html: '<div class="flag-pin">🚩</div>' }) })
        .bindPopup(popup(`${f.rule_title}`, [`<span class="badge ${CM.sevClass(f.severity)}">${CM.esc(CM.titleCase(f.severity))}</span>`,
          CM.esc(f.explanation)], f.incident_id ? { href: 'incident.html?id=' + encodeURIComponent(f.incident_id), label: 'Open incident' } : null))
        .addTo(layers.findings);
    });
  }

  $('cCount').textContent = nc; $('iCount').textContent = ni; $('aCount').textContent = na; $('fCount').textContent = nf;
}
