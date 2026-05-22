/* app.js – Charlotte House Finder frontend logic */

const API       = "";   // same origin; Flask serves both API and static files
const PAGE_SIZE = 24;

// ── State ─────────────────────────────────────────────────────────────────
let state = {
  listings:        [],
  total:           0,
  offset:          0,
  filterCity:      "",
  filterType:      "",
  filterBedrooms:  "",
  filterBathrooms: "",
  filterMinPrice:  "",
  filterMaxPrice:  "",
  scraping:        false,
  pollTimer:       null,
};

// ── DOM refs ──────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

const grid          = $("listings-grid");
const stateLoading  = $("state-loading");
const stateEmpty    = $("state-empty");
const stateError    = $("state-error");
const stateErrorMsg = $("state-error-msg");
const statusBadge   = $("status-badge");
const resultCount   = $("result-count");
const pagination    = $("pagination");
const pageInfo      = $("page-info");
const btnPrev       = $("btn-prev");
const btnNext       = $("btn-next");

// ── Utilities ─────────────────────────────────────────────────────────────
function show(el)  { el.classList.remove("hidden"); }
function hide(el)  { el.classList.add("hidden"); }

function fmtDate(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short", day: "numeric", year: "numeric",
    });
  } catch { return iso; }
}

function fmtPrice(raw) {
  if (!raw || raw === "N/A") return "Price N/A";
  const n = parseFloat(String(raw).replace(/,/g, ""));
  if (isNaN(n)) return raw;
  return "$" + n.toLocaleString();
}

function esc(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g,  "&lt;")
    .replace(/>/g,  "&gt;")
    .replace(/"/g,  "&quot;");
}

// Debounce helper for price inputs
function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ── API helpers ───────────────────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    const body = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(body.error || res.statusText);
  }
  return res.json();
}

// ── Areas (filter dropdown) ───────────────────────────────────────────────
async function loadCities() {
  const areas = await apiFetch("/api/cities");
  const sel   = $("filter-city");
  areas.forEach(a => sel.append(new Option(a.label, a.value)));
}

// ── Listings ──────────────────────────────────────────────────────────────
async function loadListings() {
  hide(grid);
  hide(stateEmpty);
  hide(stateError);
  hide(pagination);
  show(stateLoading);

  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: state.offset });
  if (state.filterCity)      params.set("city",         state.filterCity);
  if (state.filterType)      params.set("listing_type", state.filterType);
  if (state.filterBedrooms)  params.set("bedrooms",     state.filterBedrooms);
  if (state.filterBathrooms) params.set("bathrooms",    state.filterBathrooms);
  if (state.filterMinPrice)  params.set("min_price",    state.filterMinPrice);
  if (state.filterMaxPrice)  params.set("max_price",    state.filterMaxPrice);

  try {
    const data = await apiFetch(`/api/listings?${params}`);
    state.listings = data.listings;
    state.total    = data.total;
    renderListings();
  } catch (err) {
    hide(stateLoading);
    stateErrorMsg.textContent = err.message;
    show(stateError);
  }
}

function renderListings() {
  hide(stateLoading);
  grid.innerHTML = "";

  if (state.listings.length === 0) {
    show(stateEmpty);
    resultCount.textContent = "";
    return;
  }

  state.listings.forEach(l => grid.appendChild(buildCard(l)));
  show(grid);

  const from = state.offset + 1;
  const to   = Math.min(state.offset + state.listings.length, state.total);
  resultCount.textContent = `${from}–${to} of ${state.total.toLocaleString()} listings`;

  const totalPages  = Math.ceil(state.total / PAGE_SIZE);
  const currentPage = Math.floor(state.offset / PAGE_SIZE) + 1;
  if (totalPages > 1) {
    pageInfo.textContent = `Page ${currentPage} of ${totalPages}`;
    btnPrev.disabled = state.offset === 0;
    btnNext.disabled = state.offset + PAGE_SIZE >= state.total;
    show(pagination);
  }
}

function buildCard(l) {
  const card = document.createElement("article");
  card.className = "card";

  // Split "Address – City, State" into two lines
  const parts      = (l.title || "").split(" \u2013 ");
  const addrLine   = parts[0] || l.title || "Untitled";
  const cityLine   = parts[1] || "";

  // Type badge
  const typeBadge = l.listing_type === "for_rent"
    ? `<span class="type-badge type-rent">For Rent</span>`
    : `<span class="type-badge type-sale">For Sale</span>`;

  // Source pill (overlaid on image)
  const srcLabel = { redfin: "Redfin", realtor: "Realtor.com" }[l.source] || l.source;
  const srcBadge = l.source
    ? `<span class="source-tag source-${esc(l.source)}">${esc(srcLabel)}</span>`
    : "";

  // Property type
  const propTag = l.property_type
    ? `<span class="prop-tag">${esc(l.property_type)}</span>`
    : "";

  // Meta row
  const meta = [];
  if (l.bedrooms  && l.bedrooms  !== "N/A") meta.push(`<span class="meta-chip">🛏 ${esc(l.bedrooms)} bd</span>`);
  if (l.bathrooms && l.bathrooms !== "N/A") meta.push(`<span class="meta-chip">🚿 ${esc(l.bathrooms)} ba</span>`);
  if (l.sqft      && l.sqft      !== "N/A") meta.push(`<span class="meta-chip">📐 ${Number(l.sqft).toLocaleString()} ft²</span>`);

  // Footer date
  const dateStr = l.date_posted ? `Listed ${fmtDate(l.date_posted)}` : (l.date_scraped ? `Scraped ${fmtDate(l.date_scraped)}` : "");
  const link    = l.url
    ? `<a class="card-link" href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">View Listing →</a>`
    : "";

  card.innerHTML = `
    <div class="card-img">
      ${srcBadge}
      <div class="card-img-icon">🏠</div>
    </div>
    <div class="card-info">
      <div class="card-price-row">
        <span class="card-price">${esc(fmtPrice(l.price))}</span>
        ${typeBadge}
      </div>
      <p class="card-address">${esc(addrLine)}</p>
      ${cityLine ? `<p class="card-location">${esc(cityLine)}</p>` : ""}
    </div>
    <div class="card-details">
      <div class="card-meta">${meta.join("")}</div>
      ${propTag}
    </div>
    <div class="card-footer">
      <span class="card-date">${dateStr}</span>
      ${link}
    </div>
  `;
  return card;
}

// ── Scrape ────────────────────────────────────────────────────────────────
async function startScrape() {
  const source    = $("scrape-source").value;
  const listType  = $("scrape-type").value;
  const maxPages  = parseInt($("scrape-pages").value, 10) || 2;

  try {
    await apiFetch("/api/scrape", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ source, listing_type: listType, max_pages: maxPages }),
    });
    closeModal("modal-scrape");
    startPolling();
  } catch (err) {
    alert("Could not start scrape: " + err.message);
  }
}

// ── Status polling ────────────────────────────────────────────────────────
function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(pollStatus, 3000);
  pollStatus();
}

async function pollStatus() {
  try {
    const s = await apiFetch("/api/status");
    updateStatusBadge(s);
    if (!s.running) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      state.offset    = 0;
      await loadListings();
    }
  } catch { /* silently ignore */ }
}

function updateStatusBadge(s) {
  statusBadge.className = "status-badge";
  if (s.running) {
    statusBadge.classList.add("status-running");
    statusBadge.textContent = "Scraping…";
  } else if (s.message && s.message.startsWith("Scrape failed")) {
    statusBadge.classList.add("status-error");
    statusBadge.textContent = "Error";
  } else if (s.last_run) {
    statusBadge.classList.add("status-done");
    statusBadge.textContent = "Up to date";
  } else {
    statusBadge.classList.add("status-idle");
    statusBadge.textContent = "Idle";
  }
}

// ── History ───────────────────────────────────────────────────────────────
async function showHistory() {
  openModal("modal-history");
  const wrap = $("history-table-wrap");
  wrap.innerHTML = "<p class='empty-msg'>Loading…</p>";

  try {
    const rows = await apiFetch("/api/history");
    if (rows.length === 0) {
      wrap.innerHTML = "<p class='empty-msg'>No scrape runs recorded yet.</p>";
      return;
    }
    const tableRows = rows.map(r => `
      <tr>
        <td>${fmtDate(r.started_at)}</td>
        <td>${esc(r.city)}</td>
        <td>${r.listings_found ?? 0}</td>
        <td>${r.listings_new ?? 0}</td>
        <td><span class="pill ${r.status === "success" ? "pill-success" : "pill-error"}">${esc(r.status)}</span></td>
      </tr>
    `).join("");
    wrap.innerHTML = `
      <table class="history-table">
        <thead>
          <tr><th>Date</th><th>Source</th><th>Found</th><th>New</th><th>Status</th></tr>
        </thead>
        <tbody>${tableRows}</tbody>
      </table>
    `;
  } catch (err) {
    wrap.innerHTML = `<p class='empty-msg'>Error: ${esc(err.message)}</p>`;
  }
}

// ── Modal helpers ─────────────────────────────────────────────────────────
function openModal(id)  { show($(id)); }
function closeModal(id) { hide($(id)); }

// ── Event wiring ──────────────────────────────────────────────────────────
function wireEvents() {
  $("btn-scrape").addEventListener("click", () => openModal("modal-scrape"));
  $("btn-scrape-confirm").addEventListener("click", startScrape);
  $("btn-history").addEventListener("click", showHistory);

  document.querySelectorAll(".modal-close").forEach(btn => {
    btn.addEventListener("click", e => closeModal(e.target.dataset.modal));
  });
  document.querySelectorAll(".modal-overlay").forEach(overlay => {
    overlay.addEventListener("click", e => {
      if (e.target === overlay) closeModal(overlay.id);
    });
  });

  $("filter-city").addEventListener("change", e => {
    state.filterCity = e.target.value;
    state.offset = 0;
    loadListings();
  });
  $("filter-type").addEventListener("change", e => {
    state.filterType = e.target.value;
    state.offset = 0;
    loadListings();
  });
  $("filter-bedrooms").addEventListener("change", e => {
    state.filterBedrooms = e.target.value;
    state.offset = 0;
    loadListings();
  });
  $("filter-bathrooms").addEventListener("change", e => {
    state.filterBathrooms = e.target.value;
    state.offset = 0;
    loadListings();
  });

  const reloadDebounced = debounce(() => { state.offset = 0; loadListings(); }, 400);
  $("filter-min-price").addEventListener("input", e => {
    state.filterMinPrice = e.target.value;
    reloadDebounced();
  });
  $("filter-max-price").addEventListener("input", e => {
    state.filterMaxPrice = e.target.value;
    reloadDebounced();
  });

  $("btn-clear").addEventListener("click", () => {
    $("filter-city").value      = "";
    $("filter-type").value      = "";
    $("filter-bedrooms").value  = "";
    $("filter-bathrooms").value = "";
    $("filter-min-price").value = "";
    $("filter-max-price").value = "";
    state.filterCity      = "";
    state.filterType      = "";
    state.filterBedrooms  = "";
    state.filterBathrooms = "";
    state.filterMinPrice  = "";
    state.filterMaxPrice  = "";
    state.offset = 0;
    loadListings();
  });

  btnPrev.addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - PAGE_SIZE);
    loadListings();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  btnNext.addEventListener("click", () => {
    state.offset += PAGE_SIZE;
    loadListings();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      document.querySelectorAll(".modal-overlay:not(.hidden)").forEach(m => closeModal(m.id));
    }
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────
async function init() {
  wireEvents();
  await loadCities();
  await loadListings();

  try {
    const s = await apiFetch("/api/status");
    updateStatusBadge(s);
    if (s.running) startPolling();
  } catch { /* ignore */ }
}

init();
