/* ==========================================================================
   Lichess Data Studio — Multi-Page Dimensional Analytics & Cache Controller
   ========================================================================== */

const CLIENT_REPORT_TTL_MS = 180 * 1000; // 3 min client memory cache
const SESSION_LIST_TTL_MS = 180 * 1000;  // 3 min sessionStorage cache for Drive players
const SESSION_RUNS_TTL_MS = 45 * 1000;   // 45s sessionStorage cache for workflow runs

const VALID_PAGES = new Set([
  "overview",
  "ratings",
  "openings",
  "timing",
  "games",
  "pipeline",
]);

const state = {
  activePage: "overview",
  activePlayer: "Wizard-King",
  players: [],
  filters: {
    color: "all",
    speed: "all",
    rated: "all",
    source: "all",
    date_from: "",
    date_to: "",
    search: "",
    page: 1,
    page_size: 20,
  },
  dashboardData: null,
  lastCacheLabel: "Client Cache",
  reportCache: new Map(), // key -> { ts, data }
  cacheStats: null,
  ratingCategoryFilter: "main", // "main" | "all"
  openingSideTab: "overall",    // "overall" | "white" | "black"
  activeRunPollTimer: null,
  authToken: sessionStorage.getItem("lichess_portal_token") || "",
  authStatus: null,
  charts: {
    overviewRating: null,
    ratingHistory: null,
    monthlyTrends: null,
    activityHour: null,
    activityDow: null,
    statusBreakdown: null,
  },
};

function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (state.authToken) {
    headers["X-Portal-Auth"] = state.authToken;
  }
  return headers;
}

async function apiFetch(url, options = {}) {
  const opts = {
    ...options,
    headers: authHeaders(options.headers || {}),
  };
  return fetch(url, opts);
}

// Chart.js Global Typography & Theme Defaults
Chart.defaults.color = "hsl(218, 12%, 70%)";
Chart.defaults.borderColor = "hsla(215, 16%, 80%, 0.08)";
Chart.defaults.font.family = "'Plus Jakarta Sans', -apple-system, sans-serif";
Chart.defaults.font.size = 10.5;

const CATEGORY_COLORS = {
  bullet: "hsl(37, 86%, 56%)",
  blitz: "hsl(154, 64%, 48%)",
  rapid: "hsl(204, 82%, 58%)",
  classical: "hsl(28, 85%, 62%)",
  puzzle: "hsl(340, 75%, 60%)",
  correspondence: "hsl(180, 60%, 50%)",
  chess960: "hsl(95, 58%, 52%)",
  crazyhouse: "hsl(12, 76%, 58%)",
  threeCheck: "hsl(48, 88%, 58%)",
  horde: "hsl(215, 25%, 68%)",
};

/* ==========================================================================
   Utility & Cache Helpers
   ========================================================================== */

function escapeHtml(val) {
  return String(val ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtInt(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  return Number(n).toLocaleString();
}

function fmtDiff(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "0";
  const val = Math.round(Number(n));
  if (val > 0) return `+${val.toLocaleString()}`;
  return val.toLocaleString();
}

function diffClass(n) {
  const val = Number(n || 0);
  if (val > 0) return "diff-pos";
  if (val < 0) return "diff-neg";
  return "diff-zero";
}

function showToast(message) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.remove();
  }, 4500);
}

function setLoading(isLoading, message = "Loading player analytics...") {
  const banner = document.getElementById("loading-banner");
  const textEl = document.getElementById("loading-banner-text");
  if (!banner || !textEl) return;
  textEl.textContent = message;
  banner.classList.toggle("active", Boolean(isLoading));
}

function getReportCacheKey(username, filters) {
  return `${String(username || "").toLowerCase()}|${JSON.stringify(filters)}`;
}

function invalidateClientReportCache(username = null) {
  if (!username) {
    state.reportCache.clear();
    return;
  }
  const target = username.toLowerCase();
  for (const key of state.reportCache.keys()) {
    if (key.startsWith(`${target}|`) || key.startsWith("__all__|")) {
      state.reportCache.delete(key);
    }
  }
}

function getSessionCache(key, ttlMs) {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.ts !== "number") return null;
    if (Date.now() - parsed.ts > ttlMs) {
      sessionStorage.removeItem(key);
      return null;
    }
    return parsed.data;
  } catch {
    return null;
  }
}

function setSessionCache(key, data) {
  try {
    sessionStorage.setItem(key, JSON.stringify({ ts: Date.now(), data }));
  } catch {
    // Ignore storage quota errors cleanly
  }
}

function updateCacheStatusChip(source) {
  const chip = document.getElementById("chip-cache-status");
  if (!chip) return;
  chip.classList.remove("sync");
  if (source === "client_cache") {
    chip.textContent = "Cache: Client Memory (0ms)";
  } else if (source === "memory_cache") {
    chip.textContent = "Cache: Server Memory";
  } else if (source === "drive_sync") {
    chip.textContent = "Cache: Drive Synced";
    chip.classList.add("sync");
  } else if (source === "warehouse_compute") {
    chip.textContent = "Cache: Warehouse Built";
  } else {
    chip.textContent = "Cache: Ready";
  }
}

function renderCacheTelemetry(stats) {
  if (!stats) return;
  state.cacheStats = stats;
  const memEl = document.getElementById("cache-mem-players");
  const repEl = document.getElementById("cache-report-queries");
  const madeEl = document.getElementById("cache-drive-made");
  const savedEl = document.getElementById("cache-drive-saved");
  if (memEl) memEl.textContent = fmtInt(stats.cached_players_in_memory);
  if (repEl) {
    repEl.textContent = `${fmtInt(stats.cached_dashboard_queries)} srv / ${fmtInt(
      state.reportCache.size
    )} cli`;
  }
  if (madeEl) madeEl.textContent = fmtInt(stats.drive_calls_made);
  if (savedEl) savedEl.textContent = fmtInt(stats.drive_calls_saved);
}

async function refreshCacheTelemetry() {
  try {
    const res = await apiFetch("/api/cache/stats");
    if (!res.ok) return;
    const stats = await res.json();
    renderCacheTelemetry(stats);
  } catch (err) {
    console.error("Failed to load cache stats:", err);
  }
}

/* ==========================================================================
   Multi-Page Hash Navigation
   ========================================================================== */

function getPageFromHash() {
  const raw = (window.location.hash || "").replace(/^#\/?/, "").trim().toLowerCase();
  return VALID_PAGES.has(raw) ? raw : "overview";
}

function navigateTo(page, updateHash = true) {
  const targetPage = VALID_PAGES.has(page) ? page : "overview";
  state.activePage = targetPage;

  if (updateHash) {
    const desiredHash = `#/${targetPage}`;
    if (window.location.hash !== desiredHash) {
      window.location.hash = desiredHash;
    }
  }

  document.querySelectorAll("#page-nav-list .nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.nav === targetPage);
  });

  document.querySelectorAll(".report-page").forEach((section) => {
    section.classList.toggle("active", section.dataset.page === targetPage);
  });

  const filterBar = document.getElementById("global-filter-bar");
  if (filterBar) {
    filterBar.style.display = targetPage === "pipeline" ? "none" : "flex";
  }

  if (targetPage === "pipeline") {
    const triggerInput = document.getElementById("trigger-username-input");
    if (triggerInput && !triggerInput.value.trim() && state.activePlayer !== "__ALL__") {
      triggerInput.value = state.activePlayer;
    }
    refreshCacheTelemetry();
  }

  requestAnimationFrame(() => {
    renderActivePage();
  });
}

/* ==========================================================================
   Auth & Directory API Calls
   ========================================================================== */

async function checkAuthStatus() {
  try {
    const res = await apiFetch("/api/auth/status");
    if (!res.ok) return null;
    const status = await res.json();
    state.authStatus = status;

    const badge = document.getElementById("auth-status-badge");
    const unlockBox = document.getElementById("trigger-unlock-box");
    const allowedHint = document.getElementById("allowed-players-hint");
    const viewOverlay = document.getElementById("view-lock-overlay");

    if (viewOverlay) {
      viewOverlay.style.display =
        status.view_protected && !status.view_authorized ? "grid" : "none";
    }

    if (badge) {
      if (!status.trigger_protected) {
        badge.className = "auth-badge open";
        badge.textContent = `Open · ${status.max_triggers_per_hour}/hr`;
      } else if (status.trigger_authorized) {
        badge.className = "auth-badge open";
        badge.textContent = `Unlocked · ${status.authorized_user}`;
      } else {
        badge.className = "auth-badge locked";
        badge.textContent = "Locked · Key Needed";
      }
    }

    if (unlockBox) {
      unlockBox.style.display =
        status.trigger_protected && !status.trigger_authorized ? "flex" : "none";
    }

    if (allowedHint) {
      if (status.allowed_players && status.allowed_players.length > 0) {
        allowedHint.style.display = "block";
        allowedHint.textContent = `Allowed subset: ${status.allowed_players.join(", ")}`;
      } else {
        allowedHint.style.display = "none";
      }
    }

    return status;
  } catch (err) {
    console.error("Auth status error:", err);
    return null;
  }
}

async function verifyPortalToken(token) {
  const res = await fetch("/api/auth/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Invalid access key.");
  }
  const data = await res.json();
  state.authToken = token;
  sessionStorage.setItem("lichess_portal_token", token);
  await checkAuthStatus();
  return data;
}

async function loadPlayersDirectory(forceRefresh = false) {
  try {
    if (!forceRefresh) {
      const cached = getSessionCache("lichess_players_cache", SESSION_LIST_TTL_MS);
      if (cached && Array.isArray(cached.players)) {
        state.players = cached.players;
        if (cached.cache_stats) renderCacheTelemetry(cached.cache_stats);
        renderPlayerDirectory();
        return;
      }
    }

    const res = await apiFetch(`/api/players?refresh=${forceRefresh}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.players = data.players || [];
    setSessionCache("lichess_players_cache", {
      players: state.players,
      cache_stats: data.cache_stats || null,
    });
    if (data.cache_stats) {
      renderCacheTelemetry(data.cache_stats);
    }
    renderPlayerDirectory();
    if (forceRefresh) {
      showToast(`Refreshed ${state.players.length} Drive player folders.`);
    }
  } catch (err) {
    console.error("Failed to load players:", err);
  }
}

function renderPlayerDirectory() {
  const container = document.getElementById("player-directory-list");
  if (!container) return;
  container.innerHTML = "";

  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className = `player-item ${state.activePlayer === "__ALL__" ? "active" : ""}`;
  const syncedCount = state.players.filter((p) => p.is_synced).length;
  allBtn.innerHTML = `
    <div class="player-item-left">
      <div class="player-avatar">&#931;</div>
      <div class="player-name-text">All Synced Players</div>
    </div>
    <span class="player-badge">${syncedCount}/${state.players.length}</span>
  `;
  allBtn.addEventListener("click", () => selectPlayer("__ALL__"));
  container.appendChild(allBtn);

  state.players.forEach((p) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `player-item ${
      state.activePlayer.toLowerCase() === p.username.toLowerCase() ? "active" : ""
    }`;
    const initials = escapeHtml(p.username.slice(0, 2).toUpperCase());
    const badgeText = p.total_games > 0 ? `${fmtInt(p.total_games)}` : "Drive";
    btn.innerHTML = `
      <div class="player-item-left">
        <div class="player-avatar">${initials}</div>
        <div class="player-name-text">${escapeHtml(p.username)}</div>
      </div>
      <span class="player-badge">${badgeText}</span>
    `;
    btn.addEventListener("click", () => selectPlayer(p.username));
    container.appendChild(btn);
  });
}

async function selectPlayer(username, forceSync = false) {
  state.activePlayer = username;
  state.filters.page = 1;
  renderPlayerDirectory();

  const triggerInput = document.getElementById("trigger-username-input");
  if (triggerInput && username !== "__ALL__") {
    triggerInput.value = username;
  }

  await fetchDashboardData(forceSync);
}

async function fetchDashboardData(forceSync = false) {
  const uname = state.activePlayer;
  const cacheKey = getReportCacheKey(uname, state.filters);

  if (forceSync) {
    invalidateClientReportCache(uname);
    sessionStorage.removeItem("lichess_players_cache");
  } else {
    const hit = state.reportCache.get(cacheKey);
    if (hit && Date.now() - hit.ts < CLIENT_REPORT_TTL_MS) {
      state.dashboardData = hit.data;
      renderHeaderMeta(hit.data, "client_cache");
      renderActivePage();
      return;
    }
  }

  setLoading(
    true,
    forceSync
      ? `Syncing latest CSV reports for ${uname} from Google Drive...`
      : `Loading warehouse analytics for ${uname === "__ALL__" ? "All Synced Players" : uname}...`
  );

  try {
    const params = new URLSearchParams({
      color: state.filters.color,
      speed: state.filters.speed,
      rated: state.filters.rated,
      source: state.filters.source,
      page: String(state.filters.page),
      page_size: String(state.filters.page_size),
      force_sync: String(forceSync),
    });
    if (state.filters.date_from) params.set("date_from", state.filters.date_from);
    if (state.filters.date_to) params.set("date_to", state.filters.date_to);
    if (state.filters.search) params.set("search", state.filters.search);

    const res = await apiFetch(
      `/api/dashboard/${encodeURIComponent(uname)}?${params.toString()}`
    );
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      throw new Error(errBody.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    state.dashboardData = data;
    state.reportCache.set(cacheKey, { ts: Date.now(), data });

    const cacheSource = data.cache_info?.source || (forceSync ? "drive_sync" : "warehouse_compute");
    renderHeaderMeta(data, cacheSource);
    renderActivePage();

    if (forceSync) {
      await loadPlayersDirectory(true);
    } else if (state.activePage === "pipeline") {
      refreshCacheTelemetry();
    }
  } catch (err) {
    showToast(`Error loading dashboard: ${err.message}`);
  } finally {
    setLoading(false);
  }
}

/* ==========================================================================
   Header & Dimensional Page Renderers
   ========================================================================== */

function renderHeaderMeta(data, cacheSource) {
  updateCacheStatusChip(cacheSource);

  if (!data || data.empty) {
    document.getElementById("dashboard-player-title").textContent =
      data?.username || state.activePlayer;
    document.getElementById("chip-drive-sync").textContent = "Warehouse Rows: 0";
    document.getElementById("chip-date-span").textContent = "Range: --";
    return;
  }

  const s = data.summary;
  const isAll = data.username === "__ALL__";
  const displayTitle = isAll
    ? "All Synced Players (Unified SSIS Warehouse)"
    : data.username;

  document.getElementById("dashboard-player-title").textContent = displayTitle;
  const lichessLink = document.getElementById("dashboard-lichess-link");
  if (lichessLink) {
    if (isAll) {
      lichessLink.style.display = "none";
    } else {
      lichessLink.style.display = "inline";
      lichessLink.href = `https://lichess.org/@/${encodeURIComponent(data.username)}`;
    }
  }

  document.getElementById("chip-drive-sync").textContent = `Warehouse Rows: ${fmtInt(
    s.unfiltered_total_games
  )} (${fmtInt(s.white_games)} W / ${fmtInt(s.black_games)} B)`;
  document.getElementById("chip-ssis-mode").textContent =
    state.filters.color === "all"
      ? "SSIS Split: All Games Table"
      : `SSIS Split: ${state.filters.color.toUpperCase()} Table`;
  document.getElementById("chip-date-span").textContent =
    s.first_game_date && s.last_game_date
      ? `Range: ${s.first_game_date} → ${s.last_game_date}`
      : "Range: --";
}

function renderActivePage() {
  const data = state.dashboardData;
  if (!data) return;

  if (data.empty && state.activePage !== "pipeline") {
    const kpiGames = document.getElementById("kpi-total-games");
    if (kpiGames) kpiGames.textContent = "0";
    return;
  }

  switch (state.activePage) {
    case "overview":
      renderOverviewPage(data);
      break;
    case "ratings":
      renderRatingsPage(data);
      break;
    case "openings":
      renderOpeningsPage(data);
      break;
    case "timing":
      renderTimingPage(data);
      break;
    case "games":
      renderGameLogTable(data.recent_games);
      break;
    case "pipeline":
      renderCacheTelemetry(state.cacheStats);
      break;
    default:
      renderOverviewPage(data);
  }
}

/* --- Page 1: Overview & Color Split --- */
function renderOverviewPage(data) {
  const s = data.summary;
  const ratedPct =
    s.games > 0 ? Math.round((s.rated_games * 100.0) / s.games) : 0;
  document.getElementById("kpi-rated-pct").textContent = `${ratedPct}% rated`;
  document.getElementById("kpi-total-games").textContent = fmtInt(s.games);
  document.getElementById("kpi-tournament-sub").textContent = `${fmtInt(
    s.tournament_games
  )} tournament games`;

  document.getElementById("kpi-wdl-counts").textContent = `${fmtInt(
    s.wins
  )}W · ${fmtInt(s.draws)}D · ${fmtInt(s.losses)}L`;
  document.getElementById("kpi-win-rate").textContent = `${s.win_rate}% Win`;
  document.getElementById("bar-win").style.width = `${s.win_rate}%`;
  document.getElementById("bar-draw").style.width = `${s.draw_rate}%`;
  document.getElementById("bar-loss").style.width = `${s.loss_rate}%`;

  document.getElementById("kpi-current-rating").textContent = s.current_rating
    ? fmtInt(s.current_rating)
    : "--";
  document.getElementById("kpi-peak-rating").textContent = s.peak_rating
    ? `Peak Rating: ${fmtInt(s.peak_rating)}`
    : "Peak: --";
  document.getElementById("kpi-peak-date").textContent = s.peak_rating_date
    ? `Peak on ${s.peak_rating_date}`
    : "";

  const netEl = document.getElementById("kpi-net-diff");
  netEl.textContent = fmtDiff(s.net_rating_diff);
  netEl.className = `kpi-value ${diffClass(s.net_rating_diff)}`;
  document.getElementById("kpi-avg-rating").textContent = s.avg_rating
    ? `Avg Rating: ${fmtInt(s.avg_rating)}`
    : "Avg Rating: --";

  document.getElementById("kpi-opp-rating").textContent = s.avg_opponent_rating
    ? fmtInt(s.avg_opponent_rating)
    : "--";
  const edge =
    s.avg_rating && s.avg_opponent_rating
      ? Math.round(s.avg_rating - s.avg_opponent_rating)
      : 0;
  document.getElementById("kpi-rating-edge").textContent = `Player vs Opp: ${fmtDiff(
    edge
  )} pts`;

  document.getElementById("kpi-avg-turns").textContent = s.avg_turns
    ? `${s.avg_turns} turns`
    : "--";
  document.getElementById("kpi-avg-moves").textContent = s.avg_moves
    ? `${s.avg_moves} half-moves (ply)`
    : "--";

  renderColorSplitCards(data.color_split);
  renderOverviewRatingChart(data.rating_history);
  renderOverviewOpeningsSnapshot(data.openings?.overall || []);
}

function renderColorSplitCards(split) {
  if (!split) return;
  const w = split.white || {};
  const b = split.black || {};

  document.getElementById("split-white-wdl").textContent = `${fmtInt(
    w.wins
  )}W / ${fmtInt(w.draws)}D / ${fmtInt(w.losses)}L`;
  document.getElementById("split-white-bar-w").style.width = `${w.win_rate || 0}%`;
  document.getElementById("split-white-bar-d").style.width = `${w.draw_rate || 0}%`;
  document.getElementById("split-white-bar-l").style.width = `${w.loss_rate || 0}%`;
  document.getElementById("split-white-games").textContent = fmtInt(w.games);
  document.getElementById("split-white-winrate").textContent = `${w.win_rate || 0}%`;
  const wdEl = document.getElementById("split-white-diff");
  wdEl.textContent = fmtDiff(w.net_rating_diff);
  wdEl.className = diffClass(w.net_rating_diff);
  document.getElementById("split-white-turns").textContent = w.avg_turns || "--";
  document.getElementById("split-white-opening").textContent = w.top_opening || "N/A";

  document.getElementById("split-black-wdl").textContent = `${fmtInt(
    b.wins
  )}W / ${fmtInt(b.draws)}D / ${fmtInt(b.losses)}L`;
  document.getElementById("split-black-bar-w").style.width = `${b.win_rate || 0}%`;
  document.getElementById("split-black-bar-d").style.width = `${b.draw_rate || 0}%`;
  document.getElementById("split-black-bar-l").style.width = `${b.loss_rate || 0}%`;
  document.getElementById("split-black-games").textContent = fmtInt(b.games);
  document.getElementById("split-black-winrate").textContent = `${b.win_rate || 0}%`;
  const bdEl = document.getElementById("split-black-diff");
  bdEl.textContent = fmtDiff(b.net_rating_diff);
  bdEl.className = diffClass(b.net_rating_diff);
  document.getElementById("split-black-turns").textContent = b.avg_turns || "--";
  document.getElementById("split-black-opening").textContent = b.top_opening || "N/A";
}

function renderOverviewRatingChart(rh) {
  const ctx = document.getElementById("chart-overview-rating");
  if (!ctx) return;
  if (state.charts.overviewRating) {
    state.charts.overviewRating.destroy();
  }

  const dates = rh?.dates || [];
  const seriesObj = rh?.series || {};
  const coreCats = ["bullet", "blitz", "rapid", "classical"];

  const datasets = [];
  Object.keys(seriesObj).forEach((cat) => {
    if (!coreCats.includes(cat.toLowerCase())) return;
    const color = CATEGORY_COLORS[cat] || "hsl(215, 30%, 65%)";
    datasets.push({
      label: cat.charAt(0).toUpperCase() + cat.slice(1),
      data: seriesObj[cat],
      borderColor: color,
      backgroundColor: color,
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 3,
      tension: 0.2,
      spanGaps: true,
    });
  });

  state.charts.overviewRating = new Chart(ctx, {
    type: "line",
    data: { labels: dates, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "top",
          labels: { boxWidth: 10, usePointStyle: true },
        },
      },
      scales: {
        x: { ticks: { maxTicksLimit: 7 }, grid: { display: false } },
        y: { ticks: { precision: 0 } },
      },
    },
  });
}

function renderOverviewOpeningsSnapshot(rows) {
  const tbody = document.querySelector("#table-overview-openings tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const top5 = (rows || []).slice(0, 5);
  if (top5.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" style="text-align:center; padding:16px;">No opening records</td></tr>`;
    return;
  }
  top5.forEach((op) => {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    tr.innerHTML = `
      <td><span class="eco-badge">${escapeHtml(op.eco)}</span></td>
      <td class="opening-name-cell" title="${escapeHtml(op.name)}">${escapeHtml(op.short_name || op.name)}</td>
      <td class="mono-cell">${fmtInt(op.games)}</td>
      <td class="mono-cell" style="color: var(--win-green); font-weight:600;">${op.win_rate}%</td>
    `;
    tr.addEventListener("click", () => {
      applySearchFilter(op.eco !== "N/A" ? op.eco : op.short_name);
      navigateTo("openings");
    });
    tbody.appendChild(tr);
  });
}

/* --- Page 2: Ratings & Trends --- */
function renderRatingsPage(data) {
  renderRatingCategoryPills(data.rating_history?.latest_by_category || {});
  renderRatingHistoryChart(data.rating_history);
  renderMonthlyTrendsChart(data.monthly_trends);
  renderSpeedsTable(data.speed_breakdown, data.time_controls);
}

function renderRatingCategoryPills(latestMap) {
  const wrap = document.getElementById("rating-category-pills");
  if (!wrap) return;
  wrap.innerHTML = "";
  const entries = Object.entries(latestMap || {});
  if (entries.length === 0) return;

  entries.forEach(([cat, stats]) => {
    const color = CATEGORY_COLORS[cat] || "hsl(215, 30%, 65%)";
    const pill = document.createElement("div");
    pill.className = "cat-pill";
    pill.innerHTML = `
      <span class="cat-pill-dot" style="background:${color};"></span>
      <span class="cat-pill-name">${escapeHtml(cat)}</span>
      <span class="cat-pill-val">${fmtInt(stats.current)}</span>
      <span class="cat-pill-peak">(Peak ${fmtInt(stats.peak)})</span>
    `;
    wrap.appendChild(pill);
  });
}

function renderRatingHistoryChart(rh) {
  const ctx = document.getElementById("chart-rating-history");
  if (!ctx) return;
  if (state.charts.ratingHistory) {
    state.charts.ratingHistory.destroy();
  }

  const dates = rh?.dates || [];
  const seriesObj = rh?.series || {};
  const coreCats = ["bullet", "blitz", "rapid", "classical", "puzzle"];

  const datasets = [];
  Object.keys(seriesObj).forEach((cat) => {
    if (
      state.ratingCategoryFilter === "main" &&
      !coreCats.includes(cat.toLowerCase())
    ) {
      return;
    }
    const color = CATEGORY_COLORS[cat] || "hsl(215, 30%, 65%)";
    datasets.push({
      label: cat.charAt(0).toUpperCase() + cat.slice(1),
      data: seriesObj[cat],
      borderColor: color,
      backgroundColor: color,
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.2,
      spanGaps: true,
    });
  });

  state.charts.ratingHistory = new Chart(ctx, {
    type: "line",
    data: { labels: dates, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "top",
          labels: { boxWidth: 10, usePointStyle: true },
        },
      },
      scales: {
        x: {
          ticks: { maxTicksLimit: 10 },
          grid: { display: false },
        },
        y: {
          ticks: { precision: 0 },
        },
      },
    },
  });
}

function renderMonthlyTrendsChart(monthlyList) {
  const ctx = document.getElementById("chart-monthly-trends");
  if (!ctx) return;
  if (state.charts.monthlyTrends) {
    state.charts.monthlyTrends.destroy();
  }

  const items = (monthlyList || []).slice(-36);
  const labels = items.map((m) => m.month);

  state.charts.monthlyTrends = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          type: "line",
          label: "Win Rate (%)",
          data: items.map((m) => m.win_rate),
          borderColor: "hsl(37, 86%, 56%)",
          backgroundColor: "hsl(37, 86%, 56%)",
          borderWidth: 2,
          pointRadius: 2,
          yAxisID: "y1",
          tension: 0.25,
        },
        {
          type: "bar",
          label: "Games Played",
          data: items.map((m) => m.games),
          backgroundColor: "hsla(204, 80%, 58%, 0.35)",
          borderColor: "hsl(204, 80%, 58%)",
          borderWidth: 1,
          borderRadius: 3,
          yAxisID: "y",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "top", labels: { boxWidth: 10 } },
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } },
        y: {
          type: "linear",
          position: "left",
          title: { display: true, text: "Games" },
        },
        y1: {
          type: "linear",
          position: "right",
          min: 0,
          max: 100,
          grid: { drawOnChartArea: false },
          title: { display: true, text: "Win %" },
        },
      },
    },
  });
}

function renderSpeedsTable(speeds, timeControls) {
  const speedsBody = document.querySelector("#table-speeds tbody");
  if (!speedsBody) return;
  speedsBody.innerHTML = "";

  if ((!speeds || speeds.length === 0) && (!timeControls || timeControls.length === 0)) {
    speedsBody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:16px;">No speed data</td></tr>`;
    return;
  }

  (speeds || []).forEach((sp) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="color: var(--text-primary); font-weight: 600; text-transform: capitalize;">${escapeHtml(sp.speed)}</td>
      <td class="mono-cell">${fmtInt(sp.games)}</td>
      <td class="mono-cell" style="color: var(--win-green);">${sp.win_rate}%</td>
      <td class="mono-cell">${sp.peak_rating || "--"} / ${sp.current_rating || "--"}</td>
      <td class="${diffClass(sp.net_rating_diff)}">${fmtDiff(sp.net_rating_diff)}</td>
    `;
    speedsBody.appendChild(tr);
  });

  (timeControls || []).slice(0, 6).forEach((tc) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono-cell" style="color: var(--text-secondary);">Clock ${escapeHtml(tc.time_control)}</td>
      <td class="mono-cell">${fmtInt(tc.games)}</td>
      <td class="mono-cell">${tc.win_rate}%</td>
      <td class="mono-cell">${tc.peak_rating || "--"}</td>
      <td class="${diffClass(tc.net_rating_diff)}">${fmtDiff(tc.net_rating_diff)}</td>
    `;
    speedsBody.appendChild(tr);
  });
}

/* --- Page 3: Openings (ECO) --- */
function renderOpeningsPage(data) {
  renderOpeningHighlights(data.openings);
  renderOpeningsTable(data.openings);
}

function renderOpeningHighlights(openingsObj) {
  const rows =
    (openingsObj && openingsObj[state.openingSideTab]) ||
    openingsObj?.overall ||
    [];

  const setCard = (prefix, eco, name, sub) => {
    const ecoEl = document.getElementById(`op-highlight-${prefix}-eco`);
    const nameEl = document.getElementById(`op-highlight-${prefix}-name`);
    const subEl = document.getElementById(`op-highlight-${prefix}-sub`);
    if (ecoEl) ecoEl.textContent = eco || "--";
    if (nameEl) {
      nameEl.textContent = name || "--";
      nameEl.title = name || "";
    }
    if (subEl) subEl.textContent = sub || "--";
  };

  if (rows.length === 0) {
    setCard("most", "--", "No openings", "--");
    setCard("best", "--", "No openings", "--");
    setCard("gain", "--", "No openings", "--");
    return;
  }

  const most = rows[0];
  setCard(
    "most",
    most.eco,
    most.short_name || most.name,
    `${fmtInt(most.games)} games · ${most.win_rate}% win rate`
  );

  const eligibleBest = rows.filter((r) => r.games >= 5);
  const bestPool = eligibleBest.length > 0 ? eligibleBest : rows;
  const best = [...bestPool].sort(
    (a, b) => b.win_rate - a.win_rate || b.games - a.games
  )[0];
  setCard(
    "best",
    best.eco,
    best.short_name || best.name,
    `${best.win_rate}% win across ${fmtInt(best.games)} games`
  );

  const gainer = [...rows].sort(
    (a, b) => (b.net_rating_diff || 0) - (a.net_rating_diff || 0)
  )[0];
  setCard(
    "gain",
    gainer.eco,
    gainer.short_name || gainer.name,
    `${fmtDiff(gainer.net_rating_diff)} rating pts (${fmtInt(gainer.games)} games)`
  );
}

function renderOpeningsTable(openingsObj) {
  const tbody = document.querySelector("#table-openings tbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  const rows = (openingsObj && openingsObj[state.openingSideTab]) || [];
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding: 18px;">No openings match current filters</td></tr>`;
    return;
  }

  rows.forEach((op) => {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    tr.title = "Click to filter games by this opening";
    tr.innerHTML = `
      <td><span class="eco-badge">${escapeHtml(op.eco)}</span></td>
      <td class="opening-name-cell" title="${escapeHtml(op.name)}">${escapeHtml(op.name)}</td>
      <td class="mono-cell">${fmtInt(op.games)}</td>
      <td class="mono-cell">${op.wins}/${op.draws}/${op.losses}</td>
      <td>
        <div style="display:flex; align-items:center; gap:6px;">
          <span class="mono-cell" style="color: var(--text-primary); font-weight:600;">${op.win_rate}%</span>
          <div class="wdl-bar" style="width: 46px; flex-shrink:0;">
            <div class="wdl-win" style="width:${op.win_rate}%;"></div>
            <div class="wdl-draw" style="width:${op.draw_rate}%;"></div>
            <div class="wdl-loss" style="width:${op.loss_rate}%;"></div>
          </div>
        </div>
      </td>
      <td class="${diffClass(op.net_rating_diff)}">${fmtDiff(op.net_rating_diff)}</td>
      <td class="mono-cell">${op.avg_turns || "--"}</td>
    `;
    tr.addEventListener("click", () => {
      const term = op.eco !== "N/A" ? op.eco : op.short_name;
      applySearchFilter(term);
      showToast(`Filtered repertoire by ${term}`);
    });
    tbody.appendChild(tr);
  });
}

/* --- Page 4: Timing, Terminations & Rivals --- */
function renderTimingPage(data) {
  renderActivityBarChart(
    "chart-activity-hour",
    "activityHour",
    data.activity?.by_hour || [],
    "hour",
    "hsl(37, 86%, 56%)"
  );
  renderActivityBarChart(
    "chart-activity-dow",
    "activityDow",
    data.activity?.by_day_of_week || [],
    "day",
    "hsl(204, 80%, 58%)"
  );
  renderStatusBreakdownChart(data.status_breakdown);
  renderOpponentsTable(data.opponents);
}

function renderActivityBarChart(canvasId, chartKey, rows, labelKey, barHue) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  if (state.charts[chartKey]) {
    state.charts[chartKey].destroy();
  }

  const labels = (rows || []).map((r) => r[labelKey]);
  const barSoft = barHue.replace("hsl(", "hsla(").replace(")", ", 0.35)");

  state.charts[chartKey] = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          type: "line",
          label: "Win Rate (%)",
          data: (rows || []).map((r) => r.win_rate),
          borderColor: "hsl(154, 64%, 46%)",
          backgroundColor: "hsl(154, 64%, 46%)",
          borderWidth: 2,
          pointRadius: 2,
          yAxisID: "y1",
          tension: 0.25,
        },
        {
          type: "bar",
          label: "Games",
          data: (rows || []).map((r) => r.games),
          backgroundColor: barSoft,
          borderColor: barHue,
          borderWidth: 1,
          borderRadius: 3,
          yAxisID: "y",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "top", labels: { boxWidth: 10 } },
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 12 } },
        y: { position: "left", title: { display: true, text: "Games" } },
        y1: {
          position: "right",
          min: 0,
          max: 100,
          grid: { drawOnChartArea: false },
          title: { display: true, text: "Win %" },
        },
      },
    },
  });
}

function renderStatusBreakdownChart(statusList) {
  const ctx = document.getElementById("chart-status-breakdown");
  if (!ctx) return;
  if (state.charts.statusBreakdown) {
    state.charts.statusBreakdown.destroy();
  }

  const topItems = (statusList || []).slice(0, 7);
  const labels = topItems.map((i) => i.status.toUpperCase());

  state.charts.statusBreakdown = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Wins",
          data: topItems.map((i) => i.wins),
          backgroundColor: "hsl(154, 64%, 46%)",
          borderRadius: 4,
        },
        {
          label: "Draws",
          data: topItems.map((i) => i.draws),
          backgroundColor: "hsl(215, 16%, 62%)",
          borderRadius: 4,
        },
        {
          label: "Losses",
          data: topItems.map((i) => i.losses),
          backgroundColor: "hsl(4, 76%, 58%)",
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: "y",
      plugins: {
        legend: { position: "top", labels: { boxWidth: 10 } },
      },
      scales: {
        x: { stacked: true },
        y: { stacked: true, grid: { display: false } },
      },
    },
  });
}

function renderOpponentsTable(opponents) {
  const oppBody = document.querySelector("#table-opponents tbody");
  if (!oppBody) return;
  oppBody.innerHTML = "";
  if (!opponents || opponents.length === 0) {
    oppBody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 16px;">No opponent data</td></tr>`;
    return;
  }

  opponents.forEach((o) => {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    tr.title = "Click to filter head-to-head games";
    tr.innerHTML = `
      <td style="color: var(--text-primary); font-weight: 600;">${escapeHtml(o.opponent)}</td>
      <td class="mono-cell">${fmtInt(o.games)}</td>
      <td class="mono-cell">${o.wins}W - ${o.draws}D - ${o.losses}L</td>
      <td class="mono-cell">${o.win_rate}%</td>
      <td class="${diffClass(o.net_rating_diff)}">${fmtDiff(o.net_rating_diff)}</td>
    `;
    tr.addEventListener("click", () => {
      applySearchFilter(o.opponent);
      showToast(`Filtered head-to-head games vs ${o.opponent}`);
    });
    oppBody.appendChild(tr);
  });
}

/* --- Page 5: Game Log Explorer --- */
function renderGameLogTable(recentGames) {
  const tbody = document.querySelector("#table-games tbody");
  if (!tbody || !recentGames) return;
  tbody.innerHTML = "";

  const items = recentGames.items || [];
  document.getElementById("page-indicator").textContent = `Page ${
    recentGames.page
  } of ${recentGames.total_pages} (${fmtInt(recentGames.total_filtered)} games)`;
  document.getElementById("btn-prev-page").disabled = recentGames.page <= 1;
  document.getElementById("btn-next-page").disabled =
    recentGames.page >= recentGames.total_pages;

  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="11" style="text-align:center; padding: 20px;">No games match the selected filters</td></tr>`;
    return;
  }

  items.forEach((g) => {
    const tr = document.createElement("tr");
    const sideBadge =
      g.played_as === "white" ? "&#9812; White" : "&#9818; Black";
    const rDiff = g.player_rating_diff
      ? `<span class="${diffClass(g.player_rating_diff)}">(${fmtDiff(
          g.player_rating_diff
        )})</span>`
      : "";
    tr.innerHTML = `
      <td><a href="${escapeHtml(g.lichess_url)}" target="_blank" rel="noopener" class="external-link">${escapeHtml(g.game_id)} &#8599;</a></td>
      <td class="mono-cell">${escapeHtml((g.created_at || "").slice(0, 16))}</td>
      <td style="font-weight: 600; color: var(--text-primary); white-space:nowrap;">${escapeHtml(g.player_name)}</td>
      <td class="mono-cell">${sideBadge}</td>
      <td style="white-space:nowrap;">${escapeHtml(g.opponent_name)} <span class="mono-cell" style="color:var(--text-muted);">(${g.opponent_rating || "?"})</span></td>
      <td class="mono-cell">${escapeHtml(g.speed)} · ${escapeHtml(g.time_control)}</td>
      <td><span class="result-pill ${escapeHtml(g.result)}">${escapeHtml(g.result)}</span></td>
      <td class="mono-cell">${escapeHtml(g.status)}</td>
      <td class="mono-cell">${g.player_rating || "--"} ${rDiff}</td>
      <td style="max-width: 170px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(g.opening_name)}">
        <span class="eco-badge">${escapeHtml(g.opening_eco)}</span> ${escapeHtml(g.opening_name)}
      </td>
      <td class="mono-cell">${g.turns || "--"}</td>
    `;
    tbody.appendChild(tr);
  });
}

function applySearchFilter(term) {
  const searchEl = document.getElementById("filter-search");
  if (searchEl) searchEl.value = term || "";
  state.filters.search = term || "";
  state.filters.page = 1;
  fetchDashboardData(false);
}

/* ==========================================================================
   Page 6: GitHub Actions Pipeline Controller & Live Stepper
   ========================================================================== */

function setPipelineDotActive(isActive) {
  const dot = document.getElementById("nav-pipeline-dot");
  if (dot) {
    dot.style.display = isActive ? "inline-block" : "none";
  }
}

async function lookupLichessProfile() {
  const input = document.getElementById("trigger-username-input");
  const username = input?.value?.trim();
  if (!username) {
    showToast("Enter a Lichess username first.");
    return;
  }
  const card = document.getElementById("lichess-preview-card");
  try {
    const res = await apiFetch(`/api/lichess/profile/${encodeURIComponent(username)}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Player not found on Lichess");
    }
    const prof = await res.json();
    document.getElementById("preview-username").textContent = `${
      prof.title ? prof.title + " " : ""
    }${prof.username} ${prof.online ? "🟢" : ""}`;
    document.getElementById("preview-profile-link").href = prof.url;
    document.getElementById("preview-total-games").textContent = `Total Lichess Games: ${fmtInt(
      prof.count?.all || 0
    )} (Rated: ${fmtInt(prof.count?.rated || 0)})`;

    const grid = document.getElementById("preview-ratings-grid");
    grid.innerHTML = "";
    ["bullet", "blitz", "rapid", "classical", "puzzle"].forEach((cat) => {
      if (prof.ratings && prof.ratings[cat]) {
        const pill = document.createElement("div");
        pill.className = "preview-rating-pill";
        pill.innerHTML = `<span>${escapeHtml(cat)}</span><strong>${prof.ratings[cat].rating}</strong>`;
        grid.appendChild(pill);
      }
    });
    card.classList.add("visible");
  } catch (err) {
    card.classList.remove("visible");
    showToast(err.message);
  }
}

async function triggerGitHubPipeline(usernameOverride = null) {
  const input = document.getElementById("trigger-username-input");
  const username = (
    usernameOverride ||
    input?.value?.trim() ||
    (state.activePlayer !== "__ALL__" ? state.activePlayer : "")
  ).trim();

  if (!username) {
    showToast("Please enter a Lichess username to trigger the GitHub Action.");
    return;
  }

  if (state.authStatus?.trigger_protected && !state.authStatus?.trigger_authorized) {
    const unlockBox = document.getElementById("trigger-unlock-box");
    if (unlockBox) unlockBox.style.display = "flex";
    document.getElementById("trigger-access-key-input")?.focus();
    showToast("Enter your trigger access key to dispatch runs.");
    return;
  }

  const btn = document.getElementById("btn-trigger-pipeline");
  if (btn) btn.disabled = true;

  showToast(`Dispatching GitHub Action pipeline for '${username}'...`);

  try {
    const res = await apiFetch("/api/pipeline/trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      if (res.status === 401) {
        await checkAuthStatus();
      }
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const payload = await res.json();
    showToast(`GitHub Action started for ${payload.username}! Tracking live progress...`);
    sessionStorage.removeItem("lichess_runs_cache");
    await loadRecentRuns(true);
    if (payload.run && payload.run.id) {
      startPollingRun(payload.run.id, payload.username);
    } else {
      setTimeout(async () => {
        const runs = await loadRecentRuns(true);
        if (runs && runs.length > 0) {
          startPollingRun(runs[0].id, payload.username);
        }
      }, 3000);
    }
  } catch (err) {
    showToast(`Pipeline trigger error: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadRecentRuns(forceRefresh = false) {
  try {
    if (!forceRefresh) {
      const cached = getSessionCache("lichess_runs_cache", SESSION_RUNS_TTL_MS);
      if (Array.isArray(cached)) {
        renderRecentRunsList(cached);
        setPipelineDotActive(
          Boolean(state.activeRunPollTimer) ||
            cached.some((r) => r.status === "in_progress" || r.status === "queued")
        );
        return cached;
      }
    }

    const res = await apiFetch("/api/pipeline/runs?limit=6");
    if (!res.ok) return [];
    const data = await res.json();
    const runs = data.runs || [];
    setSessionCache("lichess_runs_cache", runs);
    renderRecentRunsList(runs);
    setPipelineDotActive(
      Boolean(state.activeRunPollTimer) ||
        runs.some((r) => r.status === "in_progress" || r.status === "queued")
    );
    return runs;
  } catch (err) {
    console.error("Failed to load runs:", err);
    return [];
  }
}

function renderRecentRunsList(runs) {
  const container = document.getElementById("recent-runs-list");
  if (!container) return;
  container.innerHTML = "";

  if (!runs || runs.length === 0) {
    container.innerHTML = `<div style="font-size:11px; color:var(--text-muted); padding:8px;">No recent workflow runs found.</div>`;
    return;
  }

  runs.forEach((r) => {
    const row = document.createElement("div");
    row.className = "run-row";
    let dotClass = "dot-queued";
    if (r.status === "in_progress") dotClass = "dot-running";
    else if (r.status === "completed") {
      dotClass = r.conclusion === "success" ? "dot-success" : "dot-failed";
    }
    const labelUser = r.username ? ` · ${escapeHtml(r.username)}` : "";
    const timeStr = r.createdAt
      ? new Date(r.createdAt).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        })
      : "";

    row.innerHTML = `
      <div style="display:flex; align-items:center; min-width:0;">
        <span class="step-status-dot ${dotClass}"></span>
        <span style="font-weight:600; color:var(--text-primary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
          #${String(r.id).slice(-5)}${labelUser}
        </span>
      </div>
      <span class="mono-cell" style="color:var(--text-muted); font-size:11px;">
        ${escapeHtml(r.status === "completed" ? r.conclusion : r.status)} · ${escapeHtml(timeStr)}
      </span>
    `;
    row.addEventListener("click", () => {
      startPollingRun(r.id, r.username);
    });
    container.appendChild(row);
  });
}

function startPollingRun(runId, username) {
  if (state.activeRunPollTimer) {
    clearInterval(state.activeRunPollTimer);
  }
  setPipelineDotActive(true);
  const stepperCard = document.getElementById("active-run-stepper");
  stepperCard?.classList.add("visible");

  const pollOnce = async () => {
    try {
      const res = await apiFetch(`/api/pipeline/runs/${runId}?auto_sync=true`);
      if (!res.ok) return;
      const detail = await res.json();
      renderRunStepper(detail, username);

      if (detail.status === "completed") {
        clearInterval(state.activeRunPollTimer);
        state.activeRunPollTimer = null;
        setPipelineDotActive(false);

        sessionStorage.removeItem("lichess_runs_cache");
        sessionStorage.removeItem("lichess_players_cache");
        await loadRecentRuns(true);

        const targetPlayer = detail.username || username;
        if (detail.conclusion === "success") {
          invalidateClientReportCache(targetPlayer);
          showToast(
            `Pipeline completed! Synced fresh Google Drive data${
              targetPlayer ? ` for ${targetPlayer}` : ""
            }.`
          );
          if (targetPlayer) {
            await selectPlayer(targetPlayer, true);
          } else {
            await fetchDashboardData(true);
          }
          await refreshCacheTelemetry();
        } else {
          showToast(`Workflow run #${runId} finished with: ${detail.conclusion}`);
        }
      }
    } catch (err) {
      console.error("Poll error:", err);
    }
  };

  pollOnce();
  state.activeRunPollTimer = setInterval(pollOnce, 3000);
}

function renderRunStepper(detail, fallbackUser) {
  const titleEl = document.getElementById("stepper-title");
  const linkEl = document.getElementById("stepper-gh-link");
  const stepsEl = document.getElementById("stepper-steps");
  if (!titleEl || !stepsEl) return;

  const uname = detail.username || fallbackUser || "";
  titleEl.textContent = `Run #${String(detail.id).slice(-5)}${
    uname ? ` (${uname})` : ""
  } — ${detail.status === "completed" ? detail.conclusion : detail.status}`;
  if (detail.url && linkEl) linkEl.href = detail.url;

  stepsEl.innerHTML = "";
  const jobs = detail.jobs || [];
  const steps = jobs[0]?.steps || [];
  steps.forEach((st) => {
    let dotClass = "dot-queued";
    if (st.status === "in_progress") dotClass = "dot-running";
    else if (st.status === "completed") {
      dotClass = st.conclusion === "success" ? "dot-success" : "dot-failed";
    }
    const div = document.createElement("div");
    div.className = "step-item";
    div.innerHTML = `
      <span><span class="step-status-dot ${dotClass}"></span>${escapeHtml(st.name)}</span>
      <span class="mono-cell" style="color:var(--text-muted); font-size:10.5px;">${escapeHtml(
        st.status === "completed" ? st.conclusion : st.status
      )}</span>
    `;
    stepsEl.appendChild(div);
  });
}

/* ==========================================================================
   Event Listeners & Initialization
   ========================================================================== */

function bindEvents() {
  // Multi-page sidebar navigation
  document.querySelectorAll("#page-nav-list .nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      navigateTo(btn.dataset.nav);
    });
  });

  // In-page jump shortcuts
  document.querySelectorAll(".jump-nav-btn[data-jump]").forEach((btn) => {
    btn.addEventListener("click", () => {
      navigateTo(btn.dataset.jump);
    });
  });

  // Header "Trigger Lichess Pull" shortcut -> Pipeline page
  document.getElementById("btn-goto-pipeline")?.addEventListener("click", () => {
    const triggerInput = document.getElementById("trigger-username-input");
    if (triggerInput && state.activePlayer !== "__ALL__") {
      triggerInput.value = state.activePlayer;
    }
    navigateTo("pipeline");
  });

  window.addEventListener("hashchange", () => {
    navigateTo(getPageFromHash(), false);
  });

  // Piece Color Segmented Control (SSIS Conditional Split)
  document.querySelectorAll("[data-color]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document
        .querySelectorAll("[data-color]")
        .forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.filters.color = btn.getAttribute("data-color");
      state.filters.page = 1;
      fetchDashboardData(false);
    });
  });

  // Dropdown & Date Filters
  ["speed", "rated", "source"].forEach((key) => {
    const el = document.getElementById(`filter-${key}`);
    if (el) {
      el.addEventListener("change", () => {
        state.filters[key] = el.value;
        state.filters.page = 1;
        fetchDashboardData(false);
      });
    }
  });

  ["date-from", "date-to"].forEach((idPart) => {
    const el = document.getElementById(`filter-${idPart}`);
    const stateKey = idPart.replace("-", "_");
    if (el) {
      el.addEventListener("change", () => {
        state.filters[stateKey] = el.value;
        state.filters.page = 1;
        fetchDashboardData(false);
      });
    }
  });

  // Search Filter (debounced)
  let searchTimer = null;
  const searchEl = document.getElementById("filter-search");
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.filters.search = searchEl.value;
        state.filters.page = 1;
        fetchDashboardData(false);
      }, 320);
    });
  }

  // Reset Filters
  document.getElementById("btn-reset-filters")?.addEventListener("click", () => {
    state.filters = {
      color: "all",
      speed: "all",
      rated: "all",
      source: "all",
      date_from: "",
      date_to: "",
      search: "",
      page: 1,
      page_size: 20,
    };
    document
      .querySelectorAll("[data-color]")
      .forEach((b) => b.classList.toggle("active", b.dataset.color === "all"));
    document.getElementById("filter-speed").value = "all";
    document.getElementById("filter-rated").value = "all";
    document.getElementById("filter-source").value = "all";
    document.getElementById("filter-date-from").value = "";
    document.getElementById("filter-date-to").value = "";
    document.getElementById("filter-search").value = "";
    fetchDashboardData(false);
  });

  // Rating Category Toggles
  document
    .querySelectorAll("#rating-cat-toggles .seg-btn")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        document
          .querySelectorAll("#rating-cat-toggles .seg-btn")
          .forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.ratingCategoryFilter = btn.dataset.cat;
        if (state.dashboardData) {
          renderRatingHistoryChart(state.dashboardData.rating_history);
        }
      });
    });

  // Opening Side Toggles
  document
    .querySelectorAll("#opening-tab-toggles .seg-btn")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        document
          .querySelectorAll("#opening-tab-toggles .seg-btn")
          .forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.openingSideTab = btn.dataset.side;
        if (state.dashboardData) {
          renderOpeningsPage(state.dashboardData);
        }
      });
    });

  // Pagination
  document.getElementById("btn-prev-page")?.addEventListener("click", () => {
    if (state.filters.page > 1) {
      state.filters.page -= 1;
      fetchDashboardData(false);
    }
  });
  document.getElementById("btn-next-page")?.addEventListener("click", () => {
    state.filters.page += 1;
    fetchDashboardData(false);
  });

  // Pipeline & Drive Refresh Actions
  document
    .getElementById("btn-lookup-profile")
    ?.addEventListener("click", lookupLichessProfile);
  document
    .getElementById("btn-trigger-pipeline")
    ?.addEventListener("click", () => triggerGitHubPipeline(null));
  document
    .getElementById("btn-sync-active-player")
    ?.addEventListener("click", () => {
      fetchDashboardData(true);
    });
  document
    .getElementById("btn-refresh-players")
    ?.addEventListener("click", () => loadPlayersDirectory(true));
  document
    .getElementById("btn-refresh-runs")
    ?.addEventListener("click", async () => {
      await loadRecentRuns(true);
      await refreshCacheTelemetry();
    });

  // Export CSV
  document.getElementById("btn-export-csv")?.addEventListener("click", () => {
    const params = new URLSearchParams({
      color: state.filters.color,
      speed: state.filters.speed,
      rated: state.filters.rated,
      source: state.filters.source,
    });
    if (state.filters.date_from) params.set("date_from", state.filters.date_from);
    if (state.filters.date_to) params.set("date_to", state.filters.date_to);
    if (state.filters.search) params.set("search", state.filters.search);
    if (state.authToken) params.set("token", state.authToken);
    window.location.href = `/api/export/${encodeURIComponent(
      state.activePlayer
    )}?${params.toString()}`;
  });

  // Auth unlock handlers
  document
    .getElementById("btn-unlock-trigger")
    ?.addEventListener("click", async () => {
      const key = document.getElementById("trigger-access-key-input")?.value?.trim();
      if (!key) return showToast("Enter an access key.");
      try {
        const res = await verifyPortalToken(key);
        showToast(`Unlocked as ${res.authorized_user}`);
      } catch (err) {
        showToast(err.message);
      }
    });

  document
    .getElementById("btn-unlock-view")
    ?.addEventListener("click", async () => {
      const key = document.getElementById("view-passcode-input")?.value?.trim();
      if (!key) return showToast("Enter the portal passcode.");
      try {
        await verifyPortalToken(key);
        await Promise.all([
          loadPlayersDirectory(false),
          loadRecentRuns(false),
          fetchDashboardData(false),
        ]);
      } catch (err) {
        showToast(err.message);
      }
    });

  document
    .getElementById("auth-status-badge")
    ?.addEventListener("click", () => {
      const unlockBox = document.getElementById("trigger-unlock-box");
      if (unlockBox && state.authStatus?.trigger_protected) {
        unlockBox.style.display =
          unlockBox.style.display === "none" ? "flex" : "none";
      } else {
        showToast(
          `Pipeline is open with rate limiting (${
            state.authStatus?.cooldown_seconds || 120
          }s cooldown, ${state.authStatus?.max_triggers_per_hour || 10}/hr).`
        );
      }
    });
}

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  const initialPage = getPageFromHash();
  navigateTo(initialPage, false);

  const auth = await checkAuthStatus();
  if (auth && auth.view_protected && !auth.view_authorized) {
    return;
  }
  await Promise.all([
    loadPlayersDirectory(false),
    loadRecentRuns(false),
    fetchDashboardData(false),
  ]);
});
