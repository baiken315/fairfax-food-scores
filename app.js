// Fairfax Food Grades: static front end. Data comes from data/*.json written by `ffx publish`.
"use strict";

const LETTERS = ["A", "B", "C", "D", "F", "none"];
const PAGE = 200;
const TIER_LABEL = { "priority": "Priority", "priority foundation": "Priority foundation", "core": "Core" };

const state = { meta: null, locations: [], byId: new Map(), markers: new Map(),
                q: "", letters: new Set(), category: "", shown: PAGE, selected: null,
                area: null,        // L.LatLngBounds: "search this area"
                near: null };      // {lat, lon}: "near me" (stays in the browser; never sent anywhere)

const $ = (id) => document.getElementById(id);

// All county text is untrusted: build HTML only through esc().
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtDate(iso) {
  if (!iso) return "unknown date";
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}
function titleCase(s) {
  // Words containing digits (house numbers like "6960H", "#7631") keep their original case.
  return String(s ?? "").split(/(\s+)/).map((w) => /\d/.test(w) ? w
    : w.toLowerCase().replace(/(^|[-'(/])([a-z])/g, (m, p, c) => p + c.toUpperCase())).join("")
    .replace(/\b(Ii|Iii|Iv|Llc|Usa)\b/g, (r) => r.toUpperCase());
}
function displayName(s) {
  // Names arrive in mixed case; shout-case names are softened, anything already mixed is kept.
  return s && s === s.toUpperCase() ? titleCase(s) : (s || "Unnamed");
}
function gradeBadge(letter, size = "") {
  const l = letter || "?";
  const label = letter ? `Grade ${letter}` : "Not graded";
  return `<span class="grade ${size} ${letter ? "g-" + letter : ""}" role="img" aria-label="${label}">${esc(l)}</span>`;
}
function gradeColor(letter) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(`--grade-${(letter || "none").toLowerCase()}`);
  return v.trim() || "#888";
}

function milesBetween(a, b) {
  const R = 3958.8, rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}
function fmtMiles(mi) {
  return mi < 0.1 ? "< 0.1 mi" : mi < 10 ? `${mi.toFixed(1)} mi` : `${Math.round(mi)} mi`;
}

// ------------------------------------------------------------------ map

let map;
let quietMoves = 0;          // programmatic moves in flight: don't offer "Search this area" for those
function moveMap(fn) {
  quietMoves++;
  map.once("moveend", () => setTimeout(() => { quietMoves = Math.max(0, quietMoves - 1); }, 0));
  fn();
}
function initMap() {
  map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([38.84, -77.30], 11);
  // OpenStreetMap's standard tiles: fine for modest traffic under their usage policy. For heavy traffic,
  // switch to a keyed provider (MapTiler, Stadia, Protomaps). Dark mode dims the tiles with a CSS filter.
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  for (const loc of state.locations) {
    if (loc.lat == null || loc.lon == null) continue;
    const m = L.circleMarker([loc.lat, loc.lon], {
      radius: 5, weight: 1, color: "rgba(0,0,0,.35)", fillColor: gradeColor(loc.letter), fillOpacity: 0.9,
    });
    m.bindTooltip(`${esc(displayName(loc.name))} &middot; ${loc.letter ? "Grade " + esc(loc.letter) : "Not graded"}`);
    m.on("click", () => { location.hash = encodeURIComponent(loc.id); });
    m.addTo(map);
    state.markers.set(loc.id, m);
  }

  // "Search this area": offered after the user pans or zooms; applies the current view as a filter.
  const btn = L.DomUtil.create("button", "btn map-area-btn", map.getContainer());
  btn.type = "button";
  btn.id = "area-btn";
  btn.textContent = "Search this area";
  btn.hidden = true;
  L.DomEvent.disableClickPropagation(btn);
  btn.addEventListener("click", () => {
    state.area = map.getBounds();
    btn.hidden = true;
    state.shown = PAGE;
    renderList();
  });
  map.on("moveend", () => { if (!quietMoves && $("detail").hidden) btn.hidden = false; });
}

function syncMarkers(visible) {
  for (const [id, m] of state.markers) {
    const on = visible.has(id);
    m.setStyle({ fillOpacity: on ? 0.9 : 0.08, opacity: on ? 1 : 0.1 });
  }
}

let highlight = null;
function highlightMarker(loc) {
  if (highlight) { highlight.remove(); highlight = null; }
  if (!loc || loc.lat == null) return;
  highlight = L.circleMarker([loc.lat, loc.lon], { radius: 12, weight: 3, color: gradeColor(loc.letter), fill: false }).addTo(map);
  moveMap(() => map.setView([loc.lat, loc.lon], Math.max(map.getZoom(), 15), { animate: true }));
}

// ------------------------------------------------------------------ browse list

function matches(loc) {
  if (state.area && (loc.lat == null || !state.area.contains([loc.lat, loc.lon]))) return false;
  if (state.letters.size && !state.letters.has(loc.letter || "none")) return false;
  if (state.category && loc.category !== state.category) return false;
  if (!state.q) return true;
  const hay = `${loc.name} ${loc.address} ${loc.city} ${loc.zip}`.toLowerCase();
  return state.q.split(/\s+/).every((w) => hay.includes(w));
}

function renderActive() {
  const chips = [];
  if (state.area) chips.push(`<button class="chip active-chip" data-clear="area" aria-label="Clear map area filter">In map area <span aria-hidden="true">&times;</span></button>`);
  if (state.near) chips.push(`<button class="chip active-chip" data-clear="near" aria-label="Stop sorting by distance">Nearest to you first <span aria-hidden="true">&times;</span></button>`);
  $("active").innerHTML = chips.join("");
  $("active").hidden = !chips.length;
}

function renderList() {
  let hits = state.locations.filter(matches);
  const dist = new Map();
  if (state.near) {
    for (const l of hits) dist.set(l.id, l.lat == null ? Infinity : milesBetween(state.near, l));
    hits = hits.slice().sort((a, b) => dist.get(a.id) - dist.get(b.id));
  }
  renderActive();
  $("count").textContent = `${hits.length.toLocaleString()} of ${state.locations.length.toLocaleString()} locations`;
  syncMarkers(new Set(hits.map((l) => l.id)));
  const rows = hits.slice(0, state.shown).map((l) => {
    const d = dist.has(l.id) && isFinite(dist.get(l.id)) ? `${fmtMiles(dist.get(l.id))} &middot; ` : "";
    return `
    <button class="row" data-id="${esc(l.id)}">
      ${gradeBadge(l.letter)}
      <span><span class="name">${esc(displayName(l.name))}</span><br>
      <span class="sub">${d}${esc(titleCase(l.address))}, ${esc(titleCase(l.city))} &middot; ${esc(l.category || "")}</span></span>
    </button>`;
  }).join("");
  const more = hits.length > state.shown ? `<button class="btn more" id="more">Show more</button>` : "";
  $("list").innerHTML = hits.length ? rows + more : `<div class="empty">No locations match.</div>`;
}

function initFilters() {
  const counts = {};
  for (const l of state.locations) counts[l.letter || "none"] = (counts[l.letter || "none"] || 0) + 1;
  $("letters").innerHTML = LETTERS.map((L_) => `
    <button class="chip" aria-pressed="false" data-letter="${L_}">
      ${L_ === "none" ? "Not graded" : gradeBadge(L_, "sm")} <span>${(counts[L_] || 0).toLocaleString()}</span>
    </button>`).join("");
  $("letters").addEventListener("click", (e) => {
    const b = e.target.closest(".chip"); if (!b) return;
    const l = b.dataset.letter;
    state.letters.has(l) ? state.letters.delete(l) : state.letters.add(l);
    b.setAttribute("aria-pressed", state.letters.has(l));
    state.shown = PAGE; renderList();
  });

  const cats = [...new Set(state.locations.map((l) => l.category).filter(Boolean))].sort();
  $("category").insertAdjacentHTML("beforeend", cats.map((c) => `<option>${esc(c)}</option>`).join(""));
  $("category").addEventListener("change", (e) => { state.category = e.target.value; state.shown = PAGE; renderList(); });

  let t;
  $("q").addEventListener("input", (e) => {
    clearTimeout(t);
    t = setTimeout(() => { state.q = e.target.value.trim().toLowerCase(); state.shown = PAGE; renderList(); }, 120);
  });
  $("active").addEventListener("click", (e) => {
    const b = e.target.closest("[data-clear]"); if (!b) return;
    if (b.dataset.clear === "area") state.area = null;
    if (b.dataset.clear === "near") { state.near = null; if (userMarker) { userMarker.remove(); userMarker = null; } }
    state.shown = PAGE; renderList();
  });
  $("near").addEventListener("click", nearMe);
  $("list").addEventListener("click", (e) => {
    if (e.target.id === "more") { state.shown += PAGE; renderList(); return; }
    const r = e.target.closest(".row"); if (r) location.hash = encodeURIComponent(r.dataset.id);
  });
}

// "Near me": the browser asks permission; the position is used only on this page.
let userMarker = null;
function nearMe() {
  const btn = $("near");
  if (!("geolocation" in navigator)) { $("count").textContent = "Your browser can't share your location."; return; }
  btn.disabled = true;
  btn.textContent = "Locating…";
  navigator.geolocation.getCurrentPosition((pos) => {
    btn.disabled = false;
    btn.textContent = "Near me";
    state.near = { lat: pos.coords.latitude, lon: pos.coords.longitude };
    state.area = null;
    if (userMarker) userMarker.remove();
    userMarker = L.layerGroup([
      L.circle([state.near.lat, state.near.lon], { radius: Math.min(pos.coords.accuracy, 2000), weight: 0, fillColor: "#2b7de9", fillOpacity: 0.12, interactive: false }),
      L.circleMarker([state.near.lat, state.near.lon], { radius: 7, weight: 2, color: "#fff", fillColor: "#2b7de9", fillOpacity: 1 })
        .bindTooltip("You are here"),
    ]).addTo(map);
    moveMap(() => map.setView([state.near.lat, state.near.lon], 14));
    $("area-btn").hidden = true;
    state.shown = PAGE;
    renderList();
    const nearest = Math.min(...state.locations.filter((l) => l.lat != null).map((l) => milesBetween(state.near, l)));
    if (nearest > 15) $("count").textContent += " · You appear to be outside Fairfax County";
  }, (err) => {
    btn.disabled = false;
    btn.textContent = "Near me";
    $("count").textContent = err.code === err.PERMISSION_DENIED
      ? "Location permission was denied. You can still search by name, street or city."
      : "Couldn't get your location. Try again, or search by name, street or city.";
  }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 });
}

// ------------------------------------------------------------------ detail

function itemHtml(it) {
  const tags = [];
  if (it.tier) tags.push(`<span class="tag ${it.tier === "priority" ? "priority" : ""}">${esc(TIER_LABEL[it.tier] || it.tier)}</span>`);
  if (it.is_repeat) tags.push(`<span class="tag repeat">Repeat</span>`);
  if (it.all_cdi) tags.push(`<span class="tag">Corrected on site</span>`);
  if (it.item_no <= state.meta.points.last_risk_factor_item) tags.push(`<span class="tag">Risk factor</span>`);
  const viol = (it.violations || []).map((v) => `
    <details><summary>${esc(v.code || "")} ${esc(v.title || "Details")}</summary>
      ${v.observation ? `<p><b>Observed:</b> ${esc(v.observation)}</p>` : ""}
      ${v.corrective_action ? `<p><b>Required:</b> ${esc(v.corrective_action)}</p>` : ""}
    </details>`).join("");
  return `<div class="item"><span class="item-name">${it.item_no}. ${esc(it.name || "")}</span>
    <span class="tags">${tags.join("")}</span>${viol}</div>`;
}

function inspectionHtml(i) {
  let body;
  if (i.parse_status === "ok") {
    body = i.out_items.length ? i.out_items.map(itemHtml).join("")
                              : `<div class="note" style="margin-top:6px">No items out of compliance.</div>`;
  } else if (i.parse_status === "mismatch") {
    body = `<div class="warn" style="margin-top:6px">This report couldn't be read reliably, so it isn't counted in the grade. See the county's report.</div>`;
  } else {
    body = `<div class="note" style="margin-top:6px">Report not available from the county, so it isn't counted in the grade.</div>`;
  }
  return `<div class="card">
    <div class="card-head">
      <span class="date">${esc(fmtDate(i.date))}</span>
      <span class="note">${esc(i.purpose || "Routine")} inspection</span>
      ${i.score != null ? `<span class="score">${Number(i.score).toFixed(0)}/100</span>` : ""}
    </div>
    ${body}
    <div class="note" style="margin-top:8px">${i.report_url ? `<a href="${esc(i.report_url)}" target="_blank" rel="noopener">County report</a>` : ""}
      ${i.follow_up_required ? " &middot; Follow-up inspection required" : ""}</div>
  </div>`;
}

async function showDetail(id) {
  const loc = state.byId.get(id);
  $("browse").hidden = true;
  const el = $("detail");
  el.hidden = false;
  el.innerHTML = `<div class="empty">Loading…</div>`;
  highlightMarker(loc);
  let p;
  try {
    const r = await fetch(`data/loc/${encodeURIComponent(id)}.json`);
    if (!r.ok) throw new Error(r.status);
    p = await r.json();
  } catch {
    el.innerHTML = `<button class="btn back" id="back">&larr; All locations</button><div class="empty">Couldn't load this location.</div>`;
    return;
  }
  if (state.selected !== id) return;            // user moved on while loading
  const e = p.establishment, s = p.score;
  const meaning = s && s.letter ? state.meta.letters.find((x) => x.letter === s.letter)?.meaning : null;
  const cats = s && s.category_percentile != null && e.category
    ? `<div class="fact"><b>${Math.round(100 - s.category_percentile)}%</b><span>of ${esc(e.category)} locations score the same or lower</span></div>` : "";
  const c12 = p.complaints.filter((c) => c.submitted_date && (Date.now() - new Date(c.submitted_date)) < 365 * 864e5).length;

  el.innerHTML = `
    <button class="btn back" id="back">&larr; All locations</button>
    <div class="headline">
      ${gradeBadge(s && s.letter, "big")}
      <div><h2>${esc(displayName(e.name))}</h2>
      <div class="addr">${esc(titleCase(e.address))}, ${esc(titleCase(e.city))} ${esc(e.zip || "")} &middot; ${esc(e.category || "")}</div></div>
    </div>
    ${s && s.letter
      ? `<p class="meaning">${esc(meaning)}.</p>`
      : `<p class="meaning">${s ? "Not enough recent inspections to grade." : "No readable routine inspection reports yet."}</p>`}
    <div class="facts">
      ${s ? `<div class="fact"><b>${Number(s.score).toFixed(1)}</b><span>score out of 100</span></div>` : ""}
      ${s ? `<div class="fact"><b>${s.n_inspections}</b><span>inspections counted</span></div>` : ""}
      ${cats}
      <div class="fact"><b>${c12}</b><span>complaints in the last 12 months</span></div>
    </div>
    <p class="note">Grades weigh recent inspections most and are averaged with one typical inspection for this kind of business, so a single visit can't decide the grade. <a href="method.html">How grades work</a>.</p>

    <div class="section-title">Inspections</div>
    ${p.inspections.length ? p.inspections.map(inspectionHtml).join("") : `<div class="note">No inspections on record.</div>`}

    <div class="section-title">Complaints</div>
    <p class="note">Complaints are allegations. The county doesn't publish what a complaint was about or whether it was confirmed.</p>
    ${p.complaints.length ? p.complaints.map((c) => `
      <div class="card"><div class="card-head"><span class="date">${esc(fmtDate(c.submitted_date))}</span>
        <span class="note">${esc(c.status || "")}</span>
        <span class="score"><a href="${esc(c.detail_url || "#")}" target="_blank" rel="noopener">${esc(c.record_number)}</a></span></div>
        ${c.match_method === "address_ambiguous" || c.match_confidence < 0.7 ? `<div class="note">Matched by address; may belong to another business at this address.</div>` : ""}
      </div>`).join("") : `<div class="note">No complaints matched to this location.</div>`}

    ${p.history.length ? `<div class="section-title">Changes to permit details</div>
      ${p.history.map((h) => `<div class="note">${esc(fmtDate(h.changed_at?.slice(0, 10)))}: ${esc(h.field)} changed from “${esc(h.old_value)}” to “${esc(h.new_value)}”</div>`).join("")}` : ""}
    <p class="note" style="margin-top:24px">Permit ${esc(e.record_id)}. Source: Fairfax County Health Department.</p>`;
  el.scrollTop = 0;
}

function closeDetail() {
  $("area-btn").hidden = true;
  $("detail").hidden = true;
  $("browse").hidden = false;
  if (highlight) { highlight.remove(); highlight = null; }
}

function route() {
  const id = decodeURIComponent(location.hash.slice(1));
  state.selected = id || null;
  if (id && state.byId.has(id)) showDetail(id); else closeDetail();
}

document.addEventListener("click", (e) => {
  if (e.target.id === "back") { history.pushState("", document.title, location.pathname + location.search); route(); }
});
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ boot

(async function main() {
  try {
    const [meta, locs] = await Promise.all([
      fetch("data/meta.json").then((r) => r.json()),
      fetch("data/locations.json").then((r) => r.json()),
    ]);
    state.meta = meta;
    state.locations = locs;
    for (const l of locs) state.byId.set(l.id, l);
  } catch {
    $("list").innerHTML = `<div class="empty">Couldn't load the data.</div>`;
    return;
  }
  initMap();
  initFilters();
  renderList();
  route();
})();
