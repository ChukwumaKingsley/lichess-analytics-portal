/* ==========================================================================
   Lichess Cloud Pipeline & Real-Time Analytics Portal — Frontend Controller
   ========================================================================== */

const state = {
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
  ratingCategoryFilter: "main", // "main" | "all"
  activityMode: "hour",         // "hour" | "dow"
  openingSideTab: "overall",    // "overall" | "white" | "black"
  secondaryTableView: "speeds", // "speeds" | "opponents"
  activeRunPollTimer: null,
  charts: {
    ratingHistory: null,
    statusBreakdown: null,
    monthlyTrends: null,
    activity: null,
  },
};

// Chart.js Global Typography & Theme Defaults
Chart.defaults.color = "hsl(218, 12%, 70%)";
Chart.defaults.borderColor = "hsla(215, 16%, 80%, 0.08)";
Chart.defaults.font.family = "'Plus Jakarta Sans', -apple-system, sans-serif";
Chart.defaults.font.size = 11.5;

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
   Utility Helpers
   ========================================================================== */

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
  if (isLoading) {
    banner.classList.add("active");
  } else {
    banner.classList.remove("active");
  }
}

/* ==========================================================================
   API Calls & Data Sync
   ========================================================================== */

async function loadPlayersDirectory(forceRefresh = false) {
  try {
    const res = await fetch(`/api/players?refresh=${forceRefresh}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.players = data.players || [];
    renderPlayerDirectory();
  } catch (err) {
    console.error("Failed to load players:", err);
  }
}

function renderPlayerDirectory() {
  const container = document.getElementById("player-directory-list");
  if (!container) return;
  container.innerHTML = "";

  // Add Unified Warehouse Option (Mirrors SSIS Looping Through files.dtsx)
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
    const initials = p.username.slice(0, 2).toUpperCase();
    const badgeText = p.total_games > 0 ? `${fmtInt(p.total_games)}` : "Drive";
    btn.innerHTML = `
      <div class="player-item-left">
        <div class="player-avatar">${initials}</div>
        <div class="player-name-text">${p.username}</div>
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
  loadPlayersDirectory(false);
}

async function fetchDashboardData(forceSync = false) {
  const uname = state.activePlayer;
  setLoading(
    true,
    forceSync
      ? `Downloading latest CSV reports for ${uname} from Google Drive...`
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

    const res = await fetch(
      `/api/dashboard/${encodeURIComponent(uname)}?${params.toString()}`
    );
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      throw new Error(errBody.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    state.dashboardData = data;
    renderDashboard(data);
  } catch (err) {
    showToast(`Error loading dashboard: ${err.message}`);
  } finally {
    setLoading(false);
  }
}

/* ==========================================================================
   Dashboard Rendering
   ========================================================================== */

function renderDashboard(data) {
  if (!data || data.empty) {
    document.getElementById("dashboard-player-title").textContent =
      data?.username || state.activePlayer;
    document.getElementById("kpi-total-games").textContent = "0";
    return;
  }

  const s = data.summary;
  const isAll = data.username === "__ALL__";
  const displayTitle = isAll
    ? "All Synced Players (Unified SSIS Warehouse)"
    : data.username;

  document.getElementById("dashboard-player-title").textContent = displayTitle;
  const lichessLink = document.getElementById("dashboard-lichess-link");
  if (isAll) {
    lichessLink.style.display = "none";
  } else {
    lichessLink.style.display = "inline";
    lichessLink.href = `https://lichess.org/@/${encodeURIComponent(data.username)}`;
  }

  // Metadata Chips
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

  // 1. KPI Strip
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

  // 2. SSIS White vs Black Split Cards
  renderColorSplitCards(data.color_split);

  // 3. Charts
  renderRatingHistoryChart(data.rating_history);
  renderStatusBreakdownChart(data.status_breakdown);
  renderMonthlyTrendsChart(data.monthly_trends);
  renderActivityChart(data.activity);

  // 4. Tables
  renderOpeningsTable(data.openings);
  renderSpeedsAndOpponentsTables(
    data.speed_breakdown,
    data.time_controls,
    data.opponents
  );
  renderGameLogTable(data.recent_games);
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
  document.getElementById("split-white-opening").textContent =
    w.top_opening || "N/A";

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
  document.getElementById("split-black-opening").textContent =
    b.top_opening || "N/A";
}

/* ==========================================================================
   Chart.js Visualizations
   ========================================================================== */

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
          ticks: { maxTicksLimit: 8 },
          grid: { display: false },
        },
        y: {
          ticks: { precision: 0 },
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

  const topItems = (statusList || []).slice(0, 6);
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

function renderMonthlyTrendsChart(monthlyList) {
  const ctx = document.getElementById("chart-monthly-trends");
  if (!ctx) return;
  if (state.charts.monthlyTrends) {
    state.charts.monthlyTrends.destroy();
  }

  const items = (monthlyList || []).slice(-36); // Last 36 active months for clean view
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

function renderActivityChart(activity) {
  const ctx = document.getElementById("chart-activity");
  if (!ctx) return;
  if (state.charts.activity) {
    state.charts.activity.destroy();
  }

  const isHour = state.activityMode === "hour";
  const rows = isHour
    ? activity?.by_hour || []
    : activity?.by_day_of_week || [];
  const labels = rows.map((r) => (isHour ? r.hour : r.day));

  state.charts.activity = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          type: "line",
          label: "Win Rate (%)",
          data: rows.map((r) => r.win_rate),
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
          data: rows.map((r) => r.games),
          backgroundColor: "hsla(37, 86%, 56%, 0.35)",
          borderColor: "hsl(37, 86%, 56%)",
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
        x: { grid: { display: false } },
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

/* ==========================================================================
   Tables Rendering
   ========================================================================== */

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
    tr.title = "Click to filter by this opening";
    tr.innerHTML = `
      <td><span class="eco-badge">${op.eco}</span></td>
      <td style="color: var(--text-primary); font-weight: 500; max-width: 260px;">${op.name}</td>
      <td class="mono-cell">${fmtInt(op.games)}</td>
      <td class="mono-cell">${op.wins}W / ${op.draws}D / ${op.losses}L</td>
      <td>
        <div style="display:flex; align-items:center; gap:8px;">
          <span class="mono-cell" style="color: var(--text-primary); font-weight:600;">${op.win_rate}%</span>
          <div class="wdl-bar" style="width: 64px;">
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
      const searchEl = document.getElementById("filter-search");
      if (searchEl) {
        searchEl.value = op.eco !== "N/A" ? op.eco : op.short_name;
        state.filters.search = searchEl.value;
        state.filters.page = 1;
        fetchDashboardData(false);
      }
    });
    tbody.appendChild(tr);
  });
}

function renderSpeedsAndOpponentsTables(speeds, timeControls, opponents) {
  const speedsBody = document.querySelector("#table-speeds tbody");
  if (speedsBody) {
    speedsBody.innerHTML = "";
    (speeds || []).forEach((sp) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td style="color: var(--text-primary); font-weight: 600; text-transform: capitalize;">${sp.speed}</td>
        <td class="mono-cell">${fmtInt(sp.games)}</td>
        <td class="mono-cell" style="color: var(--win-green);">${sp.win_rate}%</td>
        <td class="mono-cell">${sp.peak_rating || "--"} / ${sp.current_rating || "--"}</td>
        <td class="${diffClass(sp.net_rating_diff)}">${fmtDiff(sp.net_rating_diff)}</td>
      `;
      speedsBody.appendChild(tr);
    });

    (timeControls || []).slice(0, 5).forEach((tc) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono-cell" style="color: var(--text-secondary);">Clock ${tc.time_control}</td>
        <td class="mono-cell">${fmtInt(tc.games)}</td>
        <td class="mono-cell">${tc.win_rate}%</td>
        <td class="mono-cell">${tc.peak_rating || "--"}</td>
        <td class="${diffClass(tc.net_rating_diff)}">${fmtDiff(tc.net_rating_diff)}</td>
      `;
      speedsBody.appendChild(tr);
    });
  }

  const oppBody = document.querySelector("#table-opponents tbody");
  if (oppBody) {
    oppBody.innerHTML = "";
    if (!opponents || opponents.length === 0) {
      oppBody.innerHTML = `<tr><td colspan="5" style="text-align:center;">No opponent data</td></tr>`;
    } else {
      opponents.forEach((o) => {
        const tr = document.createElement("tr");
        tr.style.cursor = "pointer";
        tr.title = "Click to filter games against this opponent";
        tr.innerHTML = `
          <td style="color: var(--text-primary); font-weight: 600;">${o.opponent}</td>
          <td class="mono-cell">${fmtInt(o.games)}</td>
          <td class="mono-cell">${o.wins}W - ${o.draws}D - ${o.losses}L</td>
          <td class="mono-cell">${o.win_rate}%</td>
          <td class="${diffClass(o.net_rating_diff)}">${fmtDiff(o.net_rating_diff)}</td>
        `;
        tr.addEventListener("click", () => {
          const searchEl = document.getElementById("filter-search");
          if (searchEl) {
            searchEl.value = o.opponent;
            state.filters.search = o.opponent;
            state.filters.page = 1;
            fetchDashboardData(false);
          }
        });
        oppBody.appendChild(tr);
      });
    }
  }
}

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
      <td><a href="${g.lichess_url}" target="_blank" rel="noopener" class="external-link">${g.game_id} &#8599;</a></td>
      <td class="mono-cell">${g.created_at}</td>
      <td style="font-weight: 600; color: var(--text-primary);">${g.player_name}</td>
      <td class="mono-cell">${sideBadge}</td>
      <td>${g.opponent_name} <span class="mono-cell" style="color:var(--text-muted);">(${g.opponent_rating || "?"})</span></td>
      <td class="mono-cell">${g.speed} · ${g.time_control}</td>
      <td><span class="result-pill ${g.result}">${g.result}</span></td>
      <td class="mono-cell">${g.status}</td>
      <td class="mono-cell">${g.player_rating || "--"} ${rDiff}</td>
      <td style="max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${g.opening_name}">
        <span class="eco-badge">${g.opening_eco}</span> ${g.opening_name}
      </td>
      <td class="mono-cell">${g.turns || "--"}</td>
    `;
    tbody.appendChild(tr);
  });
}

/* ==========================================================================
   GitHub Actions Pipeline Controller & Live Stepper
   ========================================================================== */

async function lookupLichessProfile() {
  const input = document.getElementById("trigger-username-input");
  const username = input?.value?.trim();
  if (!username) {
    showToast("Enter a Lichess username first.");
    return;
  }
  const card = document.getElementById("lichess-preview-card");
  try {
    const res = await fetch(`/api/lichess/profile/${encodeURIComponent(username)}`);
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
        pill.innerHTML = `<span>${cat}</span><strong>${prof.ratings[cat].rating}</strong>`;
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

  const btn = document.getElementById("btn-trigger-pipeline");
  const topBtn = document.getElementById("btn-trigger-active-player");
  if (btn) btn.disabled = true;
  if (topBtn) topBtn.disabled = true;

  showToast(`Dispatching GitHub Action pipeline for '${username}'...`);

  try {
    const res = await fetch("/api/pipeline/trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const payload = await res.json();
    showToast(`GitHub Action started for ${payload.username}! Tracking live progress...`);
    await loadRecentRuns();
    if (payload.run && payload.run.id) {
      startPollingRun(payload.run.id, payload.username);
    } else {
      // Poll recent runs to attach to the new run ID
      setTimeout(async () => {
        const runs = await loadRecentRuns();
        if (runs && runs.length > 0) {
          startPollingRun(runs[0].id, payload.username);
        }
      }, 3000);
    }
  } catch (err) {
    showToast(`Pipeline trigger error: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
    if (topBtn) topBtn.disabled = false;
  }
}

async function loadRecentRuns() {
  try {
    const res = await fetch("/api/pipeline/runs?limit=6");
    if (!res.ok) return [];
    const data = await res.json();
    const runs = data.runs || [];
    renderRecentRunsList(runs);
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

  runs.forEach((r) => {
    const row = document.createElement("div");
    row.className = "run-row";
    let dotClass = "dot-queued";
    if (r.status === "in_progress") dotClass = "dot-running";
    else if (r.status === "completed") {
      dotClass = r.conclusion === "success" ? "dot-success" : "dot-failed";
    }
    const labelUser = r.username ? ` · ${r.username}` : "";
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
        ${r.status === "completed" ? r.conclusion : r.status} · ${timeStr}
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
  const stepperCard = document.getElementById("active-run-stepper");
  stepperCard.classList.add("visible");

  const pollOnce = async () => {
    try {
      const res = await fetch(`/api/pipeline/runs/${runId}?auto_sync=true`);
      if (!res.ok) return;
      const detail = await res.json();
      renderRunStepper(detail, username);

      if (detail.status === "completed") {
        clearInterval(state.activeRunPollTimer);
        state.activeRunPollTimer = null;
        await loadRecentRuns();
        await loadPlayersDirectory(true);

        const targetPlayer = detail.username || username;
        if (detail.conclusion === "success") {
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
  const uname = detail.username || fallbackUser || "";

  titleEl.textContent = `Run #${String(detail.id).slice(-5)}${
    uname ? ` (${uname})` : ""
  } — ${detail.status === "completed" ? detail.conclusion : detail.status}`;
  if (detail.url) linkEl.href = detail.url;

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
      <span><span class="step-status-dot ${dotClass}"></span>${st.name}</span>
      <span class="mono-cell" style="color:var(--text-muted); font-size:10.5px;">${
        st.status === "completed" ? st.conclusion : st.status
      }</span>
    `;
    stepsEl.appendChild(div);
  });
}

/* ==========================================================================
   Event Listeners & Initialization
   ========================================================================== */

function bindEvents() {
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

  // Activity Mode Toggles (Hour vs Day of Week)
  document
    .querySelectorAll("#activity-mode-toggles .seg-btn")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        document
          .querySelectorAll("#activity-mode-toggles .seg-btn")
          .forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.activityMode = btn.dataset.mode;
        if (state.dashboardData) {
          renderActivityChart(state.dashboardData.activity);
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
          renderOpeningsTable(state.dashboardData.openings);
        }
      });
    });

  // Secondary Table View Toggles (Speeds vs Opponents)
  document
    .querySelectorAll("#secondary-table-toggles .seg-btn")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        document
          .querySelectorAll("#secondary-table-toggles .seg-btn")
          .forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const view = btn.dataset.view;
        document.getElementById("view-speeds-wrap").style.display =
          view === "speeds" ? "block" : "none";
        document.getElementById("view-opponents-wrap").style.display =
          view === "opponents" ? "block" : "none";
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

  // Sidebar & Header Actions
  document
    .getElementById("btn-lookup-profile")
    ?.addEventListener("click", lookupLichessProfile);
  document
    .getElementById("btn-trigger-pipeline")
    ?.addEventListener("click", () => triggerGitHubPipeline(null));
  document
    .getElementById("btn-trigger-active-player")
    ?.addEventListener("click", () => {
      if (state.activePlayer === "__ALL__") {
        showToast("Select a specific player to trigger a Lichess pull.");
        return;
      }
      triggerGitHubPipeline(state.activePlayer);
    });
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
    ?.addEventListener("click", () => loadRecentRuns());

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
    window.location.href = `/api/export/${encodeURIComponent(
      state.activePlayer
    )}?${params.toString()}`;
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  await Promise.all([
    loadPlayersDirectory(false),
    loadRecentRuns(),
    fetchDashboardData(false),
  ]);
});
