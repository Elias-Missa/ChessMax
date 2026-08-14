/* Insights — ChessMax performance intelligence.
 *
 * Two surfaces share one state:
 *   • the launcher — run form, prior runs, and the "Open Insights" gateway
 *   • the full-screen report — seven sections over one metrics payload
 *
 * Filters re-aggregate the dashboard client-side from `metrics.game_explorer`,
 * the per-game fact table the server builds for exactly this purpose. Panels
 * whose inputs only exist per move (loss taxonomy, scramble decay, session
 * tilt…) carry an "All games" badge instead of silently ignoring the filter.
 */
(function () {
  const root = document.getElementById("insights-root");
  if (!root) return;

  // ── Element refs ────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);

  const form = $("insights-form");
  const sourceEl = $("insights-source");
  const usernameEl = $("insights-username");
  const windowEl = $("insights-window");
  const timeClassEl = $("insights-time-class");
  const generateBtn = $("insights-generate");
  const refreshBtn = $("insights-refresh");
  const statusEl = $("insights-status");
  const capNote = $("insights-cap-note");
  const runList = $("insights-run-list");
  const runCountEl = $("insights-run-count");
  const progressBar = $("insights-progress-bar");
  const statGames = $("insights-stat-games");
  const statFixable = $("insights-stat-fixable");
  const statFixableHint = $("insights-stat-fixable-hint");
  const statPractice = $("insights-stat-practice");

  const readyCard = $("insights-ready");
  const readyTitle = $("insights-ready-title");
  const readySub = $("insights-ready-sub");
  const readyStats = $("insights-ready-stats");
  const readyLeak = $("insights-ready-leak");
  const openBtn = $("insights-open");
  const deepBtn = $("insights-open-deep");

  const dashboard = $("insights-dashboard");
  const dashClose = $("dash-close");
  const dashHandle = $("dash-handle");
  const dashChips = $("dash-chips");
  const dashRail = $("dash-rail");
  const dashContent = $("dash-content");
  const dashPractice = $("dash-practice");
  const dashFilterCount = $("dash-filter-count");

  const modalOverlay = $("insights-modal-overlay");

  // ── State ───────────────────────────────────────────────────────────────
  let generating = false;
  let activeRunId = null;
  let currentMetrics = null;
  let currentRunMeta = {};
  let activeSection = "overview";
  let filters = { color: "all", outcome: "all", band: "all" };
  let openingColor = "all";
  let gamesSearch = "";
  let gamesSort = { key: "played_at", dir: "desc" };
  let modalGround = null;
  const charts = {};

  // ── Formatting ──────────────────────────────────────────────────────────

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  const isNum = (v) => v !== null && v !== undefined && Number.isFinite(Number(v));
  const num = (v, digits = 0) => (isNum(v) ? Number(v).toFixed(digits) : "—");
  const pct = (v, digits = 0) => (isNum(v) ? `${(Number(v) * 100).toFixed(digits)}%` : "—");
  const signed = (v, digits = 0) =>
    isNum(v) ? `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(digits)}` : "—";

  function titleCase(s) {
    return String(s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function dateShort(value) {
    if (!value) return "—";
    const text = String(value).slice(0, 10);
    return text.replace(/\./g, "-");
  }

  // ── API ─────────────────────────────────────────────────────────────────

  async function api(path, opts) {
    const resp = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(opts && opts.headers) },
      ...opts,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText || "Request failed");
    }
    return resp;
  }

  const sourceLabel = (s) => (s === "lichess" ? "Lichess" : "Chess.com");

  function practiceHref(kind) {
    if (kind === "forced") return "/training/forced";
    if (kind === "defense") return "/training/defense";
    if (kind === "guess") return "/training/guess-eval";
    return "/training/mistakes";
  }

  function goPractice(kind) {
    closeDashboard();
    const path = practiceHref(kind);
    if (window.__shellNavigate) window.__shellNavigate(path);
    else if (window.__shellSwitchTab) window.__shellSwitchTab("train");
  }

  /**
   * Open a game in Game Review. `ply` (1-based, as stored in `review_moves`)
   * lands the review on the exact move instead of the start of the game — a
   * flagged miss is useless if the user has to hunt for it.
   */
  function goReview(gameId, ply) {
    if (!gameId) return;
    closeDashboard();
    if (window.__shellNavigate) window.__shellNavigate("/game-review");
    // The vol app owns game loading; it reports its own miss into the status line.
    if (window.__volOpenGameById) {
      window.__volOpenGameById(gameId, isNum(ply) ? { ply: Number(ply) } : undefined);
    }
  }

  // ── Launcher status ─────────────────────────────────────────────────────

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  function setProgress(p) {
    if (!progressBar) return;
    const v = Math.max(4, Math.min(100, Number(p) || 0));
    progressBar.style.width = `${v}%`;
  }

  function setBusy(busy) {
    generating = busy;
    if (generateBtn) generateBtn.disabled = busy;
    if (refreshBtn) refreshBtn.disabled = busy || !activeRunId;
    root.classList.toggle("is-busy", !!busy);
    setProgress(busy ? 8 : 100);
  }

  function statusPill(status) {
    const s = String(status || "—").toLowerCase();
    let cls = "insights-pill";
    if (s === "complete" || s === "done") cls += " insights-pill--ok";
    else if (s === "error") cls += " insights-pill--err";
    else if (s === "running" || s === "pending") cls += " insights-pill--busy";
    return `<span class="${cls}">${escapeHtml(status || "—")}</span>`;
  }

  // ── Client-side aggregation over the per-game fact table ────────────────

  const facts = () => (currentMetrics && currentMetrics.game_explorer) || [];

  const filtersActive = () =>
    filters.color !== "all" || filters.outcome !== "all" || filters.band !== "all";

  function filteredFacts() {
    return facts().filter((f) => {
      if (filters.color !== "all" && f.user_color !== filters.color) return false;
      if (filters.outcome !== "all" && f.outcome !== filters.outcome) return false;
      if (filters.band !== "all" && f.rating_band !== filters.band) return false;
      return true;
    });
  }

  /** FIDE score-to-rating-difference, mirroring server/insights_pro.py. */
  function ratingDifference(scoreFraction) {
    const p = Math.min(0.999, Math.max(0.001, scoreFraction));
    return Math.max(-800, Math.min(800, -400 * Math.log10(1 / p - 1)));
  }

  function mean(xs) {
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  }

  function stdev(xs) {
    if (xs.length < 2) return null;
    const m = mean(xs);
    return Math.sqrt(mean(xs.map((x) => (x - m) ** 2)));
  }

  /** Mirrors server/insights_pro.py:_elo_left_on_board so filters move it too. */
  function eloOnTable(rows) {
    const decided = rows.filter((f) => isNum(f.points));
    if (!decided.length) return null;
    let recovered = 0;
    let fullTier = 0;
    for (const f of decided) {
      const hasFull = isNum(f.findable_delta_w);
      if (hasFull) fullTier += 1;
      const dropped = 1 - f.points;
      if (dropped <= 0) continue;
      const pool = hasFull ? f.findable_delta_w : (f.blunders || 0) * 25;
      recovered += Math.min(dropped, (pool || 0) / 100);
    }
    const actual = decided.reduce((a, f) => a + f.points, 0) / decided.length;
    const potential = Math.min(0.999, actual + recovered / decided.length);
    return {
      points: Math.round(ratingDifference(potential) - ratingDifference(actual)),
      basis: fullTier === decided.length ? "findability" : fullTier === 0 ? "blunders" : "mixed",
      full_tier_games: fullTier,
      games: decided.length,
      recoverable_score: recovered,
      actual_score_pct: actual,
      potential_score_pct: potential,
    };
  }

  function consistencyLabel(spread) {
    if (!isNum(spread)) return null;
    if (spread < 6) return "Very consistent";
    if (spread < 10) return "Consistent";
    if (spread < 15) return "Streaky";
    return "Volatile — your floor is the problem";
  }

  /** Everything the dashboard can honestly recompute at game granularity. */
  function aggregate(rows) {
    const decided = rows.filter((f) => isNum(f.points));
    const score = decided.reduce((a, f) => a + f.points, 0);
    const accuracies = rows.map((f) => f.accuracy).filter(isNum);
    const oppRatings = rows.map((f) => f.opponent_rating).filter(isNum);
    const chrono = rows.slice().reverse();
    const userRatings = chrono.map((f) => f.user_rating).filter(isNum);

    const phases = {};
    const counts = {};
    let userMoves = 0, blunders = 0, mistakes = 0, inaccuracies = 0;
    let totalDeltaW = 0, findableDeltaW = 0, findableMoves = 0;
    let critMoves = 0, critLoss = 0, critHandled = 0, critAccSum = 0, critAccN = 0;
    let quietMoves = 0, quietAccSum = 0, quietAccN = 0;
    let critTime = [], quietTime = [];
    let scrambleMoves = 0, scrambleLoss = 0;
    let cleanGames = 0;
    const byHour = {};
    const castling = {};

    for (const f of rows) {
      userMoves += f.user_moves || 0;
      blunders += f.blunders || 0;
      mistakes += f.mistakes || 0;
      inaccuracies += f.inaccuracies || 0;
      totalDeltaW += f.total_delta_w || 0;
      if (isNum(f.findable_delta_w)) {
        findableDeltaW += f.findable_delta_w;
        findableMoves += f.findable_moves || 0;
      }
      if (!f.blunders) cleanGames += 1;

      for (const [k, v] of Object.entries(f.classification_counts || {})) {
        counts[k] = (counts[k] || 0) + v;
      }
      for (const [phase, moves] of Object.entries(f.phase_moves || {})) {
        const b = (phases[phase] = phases[phase] || { moves: 0, loss: 0, accSum: 0 });
        b.moves += moves;
        b.loss += (f.phase_delta_w || {})[phase] || 0;
        const acc = (f.phase_accuracy || {})[phase];
        if (isNum(acc)) b.accSum += acc * moves;
      }

      critMoves += f.critical_moves || 0;
      critLoss += f.critical_delta_w || 0;
      critHandled += f.critical_handled || 0;
      if (isNum(f.critical_accuracy)) {
        critAccSum += f.critical_accuracy * (f.critical_moves || 0);
        critAccN += f.critical_moves || 0;
      }
      quietMoves += f.quiet_moves || 0;
      if (isNum(f.quiet_accuracy)) {
        quietAccSum += f.quiet_accuracy * (f.quiet_moves || 0);
        quietAccN += f.quiet_moves || 0;
      }
      if (isNum(f.critical_time)) critTime.push(f.critical_time);
      if (isNum(f.quiet_time)) quietTime.push(f.quiet_time);

      scrambleMoves += f.scramble_moves || 0;
      scrambleLoss += f.scramble_delta_w || 0;

      if (isNum(f.hour)) {
        const h = (byHour[f.hour] = byHour[f.hour] || { n: 0, wins: 0, accSum: 0, accN: 0 });
        h.n += 1;
        if (f.outcome === "win") h.wins += 1;
        if (isNum(f.accuracy)) { h.accSum += f.accuracy; h.accN += 1; }
      }

      const key = f.castle_side || "never";
      const c = (castling[key] = castling[key] || { n: 0, score: 0, decided: 0 });
      c.n += 1;
      if (isNum(f.points)) { c.score += f.points; c.decided += 1; }
      if (f.castle_relation) {
        const r = (castling[f.castle_relation] =
          castling[f.castle_relation] || { n: 0, score: 0, decided: 0 });
        r.n += 1;
        if (isNum(f.points)) { r.score += f.points; r.decided += 1; }
      }
    }

    const critAcc = critAccN ? critAccSum / critAccN : null;
    const quietAcc = quietAccN ? quietAccSum / quietAccN : null;

    return {
      games: rows.length,
      decided: decided.length,
      wins: decided.filter((f) => f.points === 1).length,
      draws: decided.filter((f) => f.points === 0.5).length,
      losses: decided.filter((f) => f.points === 0).length,
      score,
      scorePct: decided.length ? score / decided.length : null,
      expectedScore: decided.reduce((a, f) => a + (f.expected_points || 0), 0),
      hasExpected: decided.some((f) => isNum(f.expected_points)),
      accuracy: {
        mean: mean(accuracies),
        best: accuracies.length ? Math.max(...accuracies) : null,
        worst: accuracies.length ? Math.min(...accuracies) : null,
        stdev: stdev(accuracies),
      },
      rating: {
        start: userRatings[0] ?? null,
        end: userRatings[userRatings.length - 1] ?? null,
        delta: userRatings.length >= 2 ? userRatings[userRatings.length - 1] - userRatings[0] : null,
        perf: oppRatings.length && decided.length
          ? Math.round(mean(oppRatings) + ratingDifference(score / decided.length))
          : null,
        meanOpponent: mean(oppRatings),
      },
      userMoves, blunders, mistakes, inaccuracies, cleanGames,
      totalDeltaW,
      findableDeltaW: findableMoves ? findableDeltaW : null,
      findableMoves,
      counts,
      phases,
      critical: {
        moves: critMoves,
        loss: critLoss,
        handled: critHandled,
        accuracy: critAcc,
        time: mean(critTime),
      },
      quiet: { moves: quietMoves, accuracy: quietAcc, time: mean(quietTime) },
      criticalityGap: isNum(critAcc) && isNum(quietAcc) ? quietAcc - critAcc : null,
      scrambleMoves,
      scrambleLoss,
      byHour,
      castling,
      rows,
    };
  }

  // ── Charts ──────────────────────────────────────────────────────────────

  const C = {
    green: "#57e83f",
    greenDim: "rgba(87, 232, 63, 0.16)",
    warn: "#e8a53d",
    bad: "#e0443a",
    info: "#3db8c5",
    mute: "#5d6877",
    dim: "#98a0aa",
    grid: "rgba(255,255,255,0.06)",
    ink: "#14171b",
  };

  const CLASS_COLORS = {
    brilliant: "#1BACA6", great: "#5C8BB0", best: "#7DB249", excellent: "#96BC4B",
    good: "#A4BA65", book: "#A88865", inaccuracy: "#E3AF35", mistake: "#CA6830",
    miss: "#FF7769", blunder: "#B33430", unclassified: "#3a4048",
  };

  function baseOptions(opts) {
    const o = opts || {};
    return {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: o.indexAxis || "x",
      // Short enough that switching sections never shows a half-grown chart.
      animation: { duration: 380 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: o.legend
          ? { position: "top", labels: { color: C.dim, boxWidth: 10, font: { size: 11 } } }
          : { display: false },
        tooltip: {
          backgroundColor: "#0f1114",
          borderColor: "rgba(255,255,255,0.12)",
          borderWidth: 1,
          titleColor: "#eef1f3",
          bodyColor: "#98a0aa",
          padding: 10,
          callbacks: o.tooltip || {},
        },
      },
      // With indexAxis "y" the category axis *is* y, so the value formatting has
      // to move to x — otherwise the tick callback prints bar indices as labels.
      scales: o.scales || (o.indexAxis === "y"
        ? {
            x: {
              min: o.xMin ?? 0,
              grid: { color: C.grid },
              ticks: { color: C.mute, font: { size: 10 } },
            },
            y: { grid: { display: false }, ticks: { color: C.dim, font: { size: 11 } } },
          }
        : {
            x: { grid: { color: C.grid }, ticks: { color: C.mute, font: { size: 10 } } },
            y: {
              min: o.yMin,
              max: o.yMax,
              grid: { color: C.grid },
              ticks: {
                color: C.mute,
                font: { size: 10 },
                callback: (v) => `${v}${o.ySuffix || ""}`,
              },
            },
          }),
    };
  }

  function renderChart(id, config) {
    const canvas = $(id);
    if (!canvas || !window.Chart) return;
    if (charts[id]) charts[id].destroy();
    charts[id] = new window.Chart(canvas, config);
  }

  function emptyChart(id, message) {
    const canvas = $(id);
    if (!canvas) return true;
    if (charts[id]) { charts[id].destroy(); delete charts[id]; }
    const holder = canvas.parentElement;
    if (holder && !holder.querySelector(".dash-empty")) {
      const p = document.createElement("p");
      p.className = "dash-empty";
      p.textContent = message;
      holder.appendChild(p);
    }
    return true;
  }

  function clearEmpty(id) {
    const canvas = $(id);
    const holder = canvas && canvas.parentElement;
    if (holder) holder.querySelectorAll(".dash-empty").forEach((n) => n.remove());
  }

  function setHtml(id, html) {
    const el = $(id);
    if (el) el.innerHTML = html;
  }

  const emptyBlock = (msg) => `<p class="dash-empty">${escapeHtml(msg)}</p>`;

  /** Labelled progress row: "Kingside … 58% n=12" over a filled track. */
  function barRow(label, rate, n, { bad = false } = {}) {
    return (
      `<div class="bar-row">` +
      `<div class="bar-row-head">` +
        `<span>${escapeHtml(label)}</span>` +
        `<span class="bar-row-value"><b>${pct(rate)}</b><small>n=${n}</small></span>` +
      `</div>` +
      `<div class="bar-cell"><div class="track${bad ? " is-bad" : ""}">` +
      `<i style="width:${Math.round(Math.max(0, Math.min(1, rate || 0)) * 100)}%"></i></div></div>` +
      `</div>`
    );
  }

  // ── Dashboard shell ─────────────────────────────────────────────────────

  function openDashboard() {
    if (!dashboard || !currentMetrics) return;
    if (isStale(currentMetrics)) { rebuildRun(); return; }
    if (window.__postmortemClose) window.__postmortemClose({ silent: true });
    dashboard.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    // Force layout so Chart.js measures real container boxes, then render
    // synchronously — a rAF here never fires in a backgrounded tab, which left
    // the report blank until the next interaction.
    void dashboard.offsetWidth;
    renderDashboard();
  }

  function openStory(tab) {
    if (!currentMetrics) return;
    if (isStale(currentMetrics)) { rebuildRun(); return; }
    closeDashboard();
    if (window.__postmortemAdopt) window.__postmortemAdopt(currentMetrics, currentRunMeta);
    if (window.__postmortemOpen) window.__postmortemOpen(tab || "verdict");
  }

  function closeDashboard() {
    if (!dashboard) return;
    dashboard.classList.add("hidden");
    document.body.style.overflow = "";
  }

  function selectSection(section) {
    activeSection = section;
    dashRail.querySelectorAll(".dash-rail-btn").forEach((b) => {
      const on = b.dataset.section === section;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll(".dash-section").forEach((s) => {
      s.classList.toggle("hidden", s.id !== `dash-section-${section}`);
    });
    if (dashContent) dashContent.scrollTop = 0;
    renderSection(section);
  }

  if (dashRail) {
    dashRail.addEventListener("click", (e) => {
      const btn = e.target.closest(".dash-rail-btn");
      if (btn && btn.dataset.section !== activeSection) selectSection(btn.dataset.section);
    });
  }

  // The handler is assigned per-run (open vs rebuild) in updateLauncher.
  if (openBtn) openBtn.onclick = () => openStory("verdict");
  if (deepBtn) deepBtn.onclick = openDashboard;
  if (dashClose) dashClose.addEventListener("click", closeDashboard);

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (modalOverlay && !modalOverlay.classList.contains("hidden")) hideModal();
    else if (dashboard && !dashboard.classList.contains("hidden")) closeDashboard();
  });

  // Filters
  document.querySelectorAll("[data-filter-color], [data-filter-outcome], [data-filter-band]")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        const group = btn.parentElement;
        const key = btn.dataset.filterColor !== undefined ? "color"
          : btn.dataset.filterOutcome !== undefined ? "outcome" : "band";
        const value = btn.dataset.filterColor ?? btn.dataset.filterOutcome ?? btn.dataset.filterBand;
        group.querySelectorAll(".dash-chip").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        filters[key] = value;
        renderDashboard();
      });
    });

  document.querySelectorAll("[data-opening-color]").forEach((btn) => {
    btn.addEventListener("click", () => {
      openingColor = btn.dataset.openingColor;
      btn.parentElement.querySelectorAll(".dash-seg-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderOpenings();
    });
  });

  const gamesSearchEl = $("dash-games-search");
  if (gamesSearchEl) {
    gamesSearchEl.addEventListener("input", () => {
      gamesSearch = gamesSearchEl.value.trim().toLowerCase();
      renderGames();
    });
  }

  const gamesTable = $("dash-games-table");
  if (gamesTable) {
    gamesTable.querySelectorAll("th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        gamesSort = key === gamesSort.key
          ? { key, dir: gamesSort.dir === "asc" ? "desc" : "asc" }
          : { key, dir: "desc" };
        renderGames();
      });
    });
  }

  if (dashPractice) dashPractice.addEventListener("click", () => goPractice("mistakes"));
  const practiceAllBtn = $("dash-practice-all");
  if (practiceAllBtn) practiceAllBtn.addEventListener("click", () => goPractice("mistakes"));

  // ── Render orchestration ────────────────────────────────────────────────

  function renderDashboard() {
    if (!currentMetrics) return;
    const rows = filteredFacts();
    if (dashFilterCount) {
      dashFilterCount.textContent = filtersActive()
        ? `${rows.length} of ${facts().length} games`
        : "";
    }
    renderSection(activeSection);
  }

  function renderSection(section) {
    // The rail is reachable before a run has loaded (and every renderer below
    // reads `currentMetrics` directly), so this guard is what keeps a stray
    // click from throwing and leaving the report half-drawn.
    if (!currentMetrics) return;
    const rows = filteredFacts();
    const agg = aggregate(rows);
    if (section === "overview") renderOverview(agg);
    else if (section === "quality") renderQuality(agg);
    else if (section === "critical") renderCritical(agg);
    else if (section === "openings") renderOpenings(agg);
    else if (section === "time") renderTime(agg);
    else if (section === "games") renderGames();
    else if (section === "practice") renderPractice();
  }

  const pro = () => (currentMetrics && currentMetrics.pro) || {};

  // ── Section: Overview ───────────────────────────────────────────────────

  // `basis` is the server's enum for which model produced the number. Shown
  // raw it read "recoverable via mixed", which means nothing to a player.
  function eloBasisSub(basis) {
    if (basis === "findability") return "from misses you could realistically have found";
    if (basis === "blunders") return "from outright blunders";
    if (basis === "mixed") return "from your blunders and findable misses";
    return "needs a completed run";
  }

  function kpi(label, value, sub, cls) {
    return (
      `<div class="kpi${cls ? ` ${cls.card || ""}` : ""}">` +
      `<span class="kpi-label">${escapeHtml(label)}</span>` +
      `<span class="kpi-value${cls && cls.value ? ` ${cls.value}` : ""}">${value}</span>` +
      (sub ? `<span class="kpi-sub">${sub}</span>` : "") +
      `</div>`
    );
  }

  function renderOverview(agg) {
    const elo = eloOnTable(agg.rows) || {};

    const recordSub =
      `${agg.wins}W · ${agg.draws}D · ${agg.losses}L`;
    const expectDelta = agg.hasExpected ? agg.score - agg.expectedScore : null;
    const perfSub = isNum(agg.rating.meanOpponent)
      ? `vs ${num(agg.rating.meanOpponent)} avg opposition`
      : "";
    const ratingSub = isNum(agg.rating.start) && isNum(agg.rating.end)
      ? `${agg.rating.start} → ${agg.rating.end}`
      : "no rating data";

    setHtml("dash-kpis",
      kpi("Games", agg.games, recordSub) +
      kpi("Score", pct(agg.scorePct, 1),
        expectDelta === null ? "" :
          `<span class="${expectDelta >= 0 ? "up" : "down"}">${signed(expectDelta, 1)} pts vs Elo expectation</span>`,
        { value: agg.scorePct >= 0.5 ? "accent" : "" }) +
      kpi("Performance rating", agg.rating.perf ?? "—", perfSub) +
      kpi("Rating change", isNum(agg.rating.delta) ? signed(agg.rating.delta) : "—", ratingSub,
        { value: isNum(agg.rating.delta) ? (agg.rating.delta >= 0 ? "accent" : "bad") : "" }) +
      kpi("Accuracy", isNum(agg.accuracy.mean) ? `${num(agg.accuracy.mean, 1)}%` : "—",
        isNum(agg.accuracy.stdev)
          ? `±${num(agg.accuracy.stdev, 1)} · ${escapeHtml(consistencyLabel(agg.accuracy.stdev))}`
          : "") +
      kpi("Blunders per 100 moves", num(100 * (agg.blunders / (agg.userMoves || 1)), 1),
        `${agg.blunders} across ${agg.userMoves} moves`,
        { value: "bad", card: "is-bad" }) +
      kpi("Rating on the table", isNum(elo.points) ? `+${elo.points}` : "—",
        eloBasisSub(elo.basis), { value: "accent" })
    );

    renderLeaks();
    renderStrengths();
    renderElo(agg);
    renderTimelineChart(agg);
    renderTrend();
  }

  function renderLeaks() {
    const leaks = pro().leaks || [];
    if (!leaks.length) {
      setHtml("body-leaks", emptyBlock(
        "No leak clears the measurement threshold in this window. Analyze more games for a sharper read."
      ));
      return;
    }
    const top = Math.max(...leaks.map((l) => l.impact_win_pct_per_game)) || 1;
    setHtml("body-leaks", leaks.slice(0, 6).map((l, i) => (
      `<div class="leak sev-${escapeHtml(l.severity)}">` +
        `<div class="leak-rank">${i + 1}</div>` +
        `<div>` +
          `<p class="leak-title">${escapeHtml(l.title)}</p>` +
          `<p class="leak-detail">${escapeHtml(l.detail)}</p>` +
        `</div>` +
        `<div class="leak-impact">` +
          `<b>${num(l.impact_win_pct_per_game, 1)}</b>` +
          `<small>win% / game</small>` +
          `<div class="leak-bar"><i style="width:${Math.round(100 * l.impact_win_pct_per_game / top)}%"></i></div>` +
          `<button type="button" class="leak-practice" data-leak-practice="${escapeHtml(l.practice || "mistakes")}" data-leak-section="${escapeHtml(l.section || "")}">Fix this →</button>` +
        `</div>` +
      `</div>`
    )).join("") +
    `<p style="margin-top:12px;font-size:0.79rem;color:var(--ins-mute);line-height:1.5">` +
    `Impact is the excess win% each leak costs per game, capped at the win% you actually lost. ` +
    `Leaks overlap — one blundered move can be a critical endgame move played in a scramble — so they are ` +
    `ranked, not summed.</p>`);

    $("body-leaks").querySelectorAll("[data-leak-practice]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const section = btn.dataset.leakSection;
        // Jump to the evidence first; the practice route is one more click away.
        if (section && $(`dash-section-${section}`)) selectSection(section);
        else goPractice(btn.dataset.leakPractice);
      });
    });
  }

  function renderStrengths() {
    const strengths = pro().strengths || [];
    if (!strengths.length) {
      setHtml("body-strengths", emptyBlock("Nothing has separated itself as a strength yet."));
      return;
    }
    setHtml("body-strengths", strengths.map((s) => (
      `<div class="strength">` +
        `<span class="strength-mark">✓</span>` +
        `<div><b>${escapeHtml(s.title)}</b><span>${escapeHtml(s.detail)}</span></div>` +
      `</div>`
    )).join(""));
  }

  function renderElo(agg) {
    const elo = eloOnTable(agg.rows) || {};
    const model = ((pro().headline || {}).elo_left_on_board || {}).model || "";
    if (!isNum(elo.points)) {
      setHtml("body-elo", emptyBlock("Needs a completed run with decided games."));
      return;
    }
    const basisCopy = {
      findability: "moves a player at your level would realistically have found",
      mixed: `moves you could realistically have found (${elo.full_tier_games || 0} of ` +
        `${elo.games || 0} games at full tier; the rest counted by outright blunders)`,
      blunders: "outright blunders",
    }[elo.basis] || "recoverable mistakes";

    setHtml("body-elo",
      `<div class="stat-row">` +
        `<div class="stat"><b>+${elo.points}</b><span>Elo</span></div>` +
        `<div class="stat"><b class="neutral">${pct(elo.actual_score_pct, 1)}</b><span>Actual</span></div>` +
        `<div class="stat"><b class="neutral">${pct(elo.potential_score_pct, 1)}</b><span>Potential</span></div>` +
      `</div>` +
      `<p>Recovering the win% you lost on ${basisCopy} would have been worth ` +
      `<strong>${num(elo.recoverable_score, 1)}</strong> extra game points across ${elo.games || 0} games.</p>` +
      `<p style="font-size:0.8rem;color:var(--ins-mute)">${escapeHtml(model)}</p>`
    );
  }

  function renderTimelineChart(agg) {
    const rows = agg.rows.slice().reverse();
    if (rows.length < 2) {
      emptyChart("chart-timeline", "At least two games are needed to draw a trend.");
      return;
    }
    clearEmpty("chart-timeline");
    const hasRating = rows.some((r) => isNum(r.user_rating));
    const datasets = [
      {
        label: "Accuracy %",
        data: rows.map((r) => (isNum(r.accuracy) ? Number(r.accuracy.toFixed(1)) : null)),
        borderColor: C.green,
        backgroundColor: C.greenDim,
        fill: true,
        tension: 0.32,
        pointRadius: 2,
        pointHoverRadius: 5,
        yAxisID: "y",
        spanGaps: true,
      },
    ];
    if (hasRating) {
      datasets.push({
        label: "Rating",
        data: rows.map((r) => (isNum(r.user_rating) ? r.user_rating : null)),
        borderColor: C.info,
        backgroundColor: "transparent",
        borderDash: [5, 4],
        tension: 0.25,
        pointRadius: 0,
        yAxisID: "y1",
        spanGaps: true,
      });
    }
    renderChart("chart-timeline", {
      type: "line",
      data: {
        labels: rows.map((r, i) => dateShort(r.played_at) || `#${i + 1}`),
        datasets,
      },
      options: {
        ...baseOptions({ legend: true }),
        scales: {
          x: { grid: { color: C.grid }, ticks: { color: C.mute, maxTicksLimit: 12, font: { size: 10 } } },
          y: {
            position: "left",
            grid: { color: C.grid },
            ticks: { color: C.mute, font: { size: 10 }, callback: (v) => `${v}%` },
            suggestedMin: 40,
            suggestedMax: 100,
          },
          y1: {
            position: "right",
            display: hasRating,
            grid: { display: false },
            ticks: { color: C.info, font: { size: 10 } },
          },
        },
      },
    });
  }

  function renderTrend() {
    const trend = currentMetrics.trend;
    if (!trend) {
      setHtml("body-trend", emptyBlock(
        "Run this window again later and this card compares the two automatically."
      ));
      return;
    }
    const rowFor = (label, d, invert) => {
      if (!d || !isNum(d.pct)) return "";
      const better = invert ? d.delta < 0 : d.delta > 0;
      return (
        `<div class="meta-row" style="display:flex;justify-content:space-between;gap:12px;font-size:0.86rem;padding:4px 0">` +
        `<span style="color:var(--ins-dim)">${escapeHtml(label)}</span>` +
        `<strong style="color:${better ? "var(--ins-green-hi)" : "var(--ins-bad)"}">${signed(d.pct, 0)}%</strong>` +
        `</div>`
      );
    };
    const phases = trend.phase_delta_w_per_move || {};
    setHtml("body-trend",
      (trend.highlights || []).map((h) => `<p><strong>${escapeHtml(h)}</strong></p>`).join("") +
      `<div style="margin-top:10px">` +
      rowFor("Fixable loss", trend.fixable_loss, true) +
      rowFor("Total loss", trend.total_loss, true) +
      Object.entries(phases).map(([p, d]) => rowFor(`${titleCase(p)} Δw/move`, d, true)).join("") +
      `</div>` +
      `<p style="margin-top:10px;font-size:0.8rem;color:var(--ins-mute)">` +
      `Compared with your run from ${escapeHtml(dateShort(trend.previous_created_at))} ` +
      `(${trend.previous_games_analyzed || 0} games).</p>`
    );
  }

  // ── Section: Move quality ───────────────────────────────────────────────

  const QUALITY_ORDER = [
    "brilliant", "great", "best", "excellent", "good", "book",
    "inaccuracy", "mistake", "miss", "blunder", "unclassified",
  ];

  function renderQuality(agg) {
    const entries = QUALITY_ORDER
      .map((k) => [k, agg.counts[k] || 0])
      .filter(([, v]) => v > 0);
    const total = entries.reduce((a, [, v]) => a + v, 0);

    if (!total) {
      setHtml("body-quality-mix", emptyBlock("No classified moves in this selection."));
    } else {
      setHtml("body-quality-mix",
        `<div class="quality-bar">` +
        entries.map(([k, v]) =>
          `<i style="width:${(100 * v / total).toFixed(2)}%;background:${CLASS_COLORS[k] || C.mute}" title="${escapeHtml(titleCase(k))}: ${v}"></i>`
        ).join("") +
        `</div>` +
        `<div class="quality-legend">` +
        entries.map(([k, v]) =>
          `<span class="ql"><i style="background:${CLASS_COLORS[k] || C.mute}"></i>` +
          `${escapeHtml(titleCase(k))} <b>${v} · ${((100 * v) / total).toFixed(1)}%</b></span>`
        ).join("") +
        `</div>`
      );
    }

    // Accuracy and Δw/move are the same signal through different transforms, so
    // one chart carries both: bars for accuracy, the per-move loss in the
    // tooltip and the line beneath.
    const phaseKeys = ["opening", "middlegame", "endgame"].filter((p) => agg.phases[p]);
    if (!phaseKeys.length) {
      emptyChart("chart-phase-accuracy", "No phase data.");
      setHtml("body-phase-detail", "");
    } else {
      clearEmpty("chart-phase-accuracy");
      const perMove = phaseKeys.map((p) => {
        const b = agg.phases[p];
        return b.moves ? b.loss / b.moves : 0;
      });
      renderChart("chart-phase-accuracy", {
        type: "bar",
        data: {
          labels: phaseKeys.map(titleCase),
          datasets: [{
            data: phaseKeys.map((p) => {
              const b = agg.phases[p];
              return b.moves ? Number((b.accSum / b.moves).toFixed(1)) : 0;
            }),
            backgroundColor: [C.info, C.warn, C.green],
            borderRadius: 6,
          }],
        },
        options: baseOptions({
          yMin: 0,
          yMax: 100,
          ySuffix: "%",
          tooltip: {
            afterLabel: (ctx) => `${perMove[ctx.dataIndex].toFixed(2)} win% lost per move`,
          },
        }),
      });
      setHtml("body-phase-detail",
        `<div class="phase-detail">` +
        phaseKeys.map((p, i) =>
          `<span><b>${num(perMove[i], 2)}</b><small>${escapeHtml(titleCase(p))} Δw/move</small></span>`
        ).join("") +
        `</div>`
      );
    }

    const perBlunder = agg.blunders ? agg.userMoves / agg.blunders : null;
    setHtml("body-error-rates",
      `<div class="stat-row">` +
        `<div class="stat"><b class="bad">${num(100 * agg.blunders / (agg.userMoves || 1), 1)}</b><span>Blunders/100</span></div>` +
        `<div class="stat"><b class="warn">${num(100 * agg.mistakes / (agg.userMoves || 1), 1)}</b><span>Mistakes/100</span></div>` +
        `<div class="stat"><b class="neutral">${num(100 * agg.inaccuracies / (agg.userMoves || 1), 1)}</b><span>Inacc./100</span></div>` +
      `</div>` +
      `<p>One blunder every <strong>${perBlunder ? num(perBlunder) : "—"}</strong> moves. ` +
      `<strong>${pct(agg.games ? agg.cleanGames / agg.games : null)}</strong> of games contain no blunder at all.</p>` +
      // It is a sum over moves, so it runs past 100 per game — say so, or it
      // reads as an impossible percentage.
      `<p>Win% given away, summed over every move you played: ` +
      `<strong>${num(agg.totalDeltaW)}</strong> ` +
      `(${num(agg.games ? agg.totalDeltaW / agg.games : null, 1)} per game, across all its moves).</p>`
    );

    const timing = (pro().blunder_timing || {}).buckets || [];
    if (!timing.some((b) => b.moves)) {
      emptyChart("chart-blunder-timing", "No move-number data.");
    } else {
      clearEmpty("chart-blunder-timing");
      renderChart("chart-blunder-timing", {
        type: "bar",
        data: {
          labels: timing.map((b) => b.key),
          datasets: [
            {
              type: "bar",
              label: "Blunder rate %",
              data: timing.map((b) => Number((b.blunder_rate * 100).toFixed(2))),
              backgroundColor: "rgba(224, 68, 58, 0.7)",
              borderRadius: 5,
              yAxisID: "y",
            },
            {
              type: "line",
              label: "Δw per move",
              data: timing.map((b) => Number(b.delta_w_per_move.toFixed(2))),
              borderColor: C.warn,
              backgroundColor: "transparent",
              tension: 0.3,
              pointRadius: 3,
              yAxisID: "y1",
            },
          ],
        },
        options: {
          ...baseOptions({ legend: true }),
          scales: {
            x: { grid: { color: C.grid }, ticks: { color: C.mute, font: { size: 10 } } },
            y: { position: "left", min: 0, grid: { color: C.grid }, ticks: { color: C.mute, font: { size: 10 }, callback: (v) => `${v}%` } },
            y1: { position: "right", min: 0, grid: { display: false }, ticks: { color: C.warn, font: { size: 10 } } },
          },
        },
      });
      const first = pro().blunder_timing || {};
      if (isNum(first.mean_first_error_move)) {
        const holder = $("chart-blunder-timing").closest(".dash-card");
        const head = holder && holder.querySelector(".dash-card-head .eyebrow");
        if (head) {
          head.textContent = `First serious error lands on move ${num(first.mean_first_error_move, 1)} on average`;
        }
      }
    }

    const tax = (currentMetrics.loss_taxonomy || {}).counts || {};
    const taxKeys = Object.keys(tax);
    if (!taxKeys.length) {
      emptyChart("chart-loss-taxonomy", "No taxonomy data.");
    } else {
      clearEmpty("chart-loss-taxonomy");
      renderChart("chart-loss-taxonomy", {
        type: "doughnut",
        data: {
          labels: taxKeys.map(titleCase),
          datasets: [{
            data: taxKeys.map((k) => tax[k]),
            backgroundColor: [C.bad, C.warn, C.info, C.mute, C.green, "#8a6fd1"],
            borderWidth: 1,
            borderColor: C.ink,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: "58%",
          plugins: {
            legend: { position: "right", labels: { color: C.dim, boxWidth: 10, font: { size: 11 } } },
          },
        },
      });
    }
  }

  // ── Section: Critical moments ───────────────────────────────────────────

  function renderCritical(agg) {
    const crit = pro().critical_moments || {};
    const callout = $("critical-callout");
    if (callout) {
      const text = crit.note || crit.time_note || "";
      callout.textContent = text;
      callout.classList.toggle("is-warn", Boolean(crit.note && crit.criticality_gap > 10));
    }

    const buckets = crit.buckets || [];
    if (!buckets.some((b) => b.moves)) {
      setHtml("body-critical-table", emptyBlock("No volatility data — reviews need the vol engine."));
    } else {
      setHtml("body-critical-table",
        `<div class="table-scroll"><table class="dash-table">` +
        `<thead><tr><th>Position type</th><th class="num">Moves</th><th class="num">Accuracy</th>` +
        `<th class="num">Δw / move</th><th class="num">Handled cleanly</th><th class="num">Blunder rate</th>` +
        `<th class="num">Avg time</th></tr></thead><tbody>` +
        buckets.map((b) => (
          `<tr>` +
          `<td class="strong">${escapeHtml(b.label)}</td>` +
          `<td class="num">${b.moves}</td>` +
          `<td class="num">${isNum(b.accuracy) ? `${num(b.accuracy, 1)}%` : "—"}</td>` +
          `<td class="num">${num(b.delta_w_per_move, 2)}</td>` +
          `<td class="num">${pct(b.handled_rate)}</td>` +
          `<td class="num">${pct(b.blunder_rate, 1)}</td>` +
          `<td class="num">${isNum(b.mean_time) ? `${num(b.mean_time, 1)}s` : "—"}</td>` +
          `</tr>`
        )).join("") +
        `</tbody></table></div>` +
        // The filtered restatement is only worth a sentence when a filter is
        // actually on; unfiltered it just repeated the same number back.
        (isNum(crit.criticality_gap)
          ? `<p style="margin-top:12px">Criticality gap: <strong>${num(crit.criticality_gap, 1)} accuracy points</strong> ` +
            `between quiet and critical positions.` +
            (filtersActive()
              ? ` Under the current filter that gap is ` +
                `<strong>${isNum(agg.criticalityGap) ? num(agg.criticalityGap, 1) : "—"}</strong>.`
              : "") +
            `</p>`
          : "")
      );
    }

    // Think time per bucket is already a column in the table above — a second
    // chart of the same three numbers was pure noise.

    const fixable = currentMetrics.fixable_loss;
    const sample = currentMetrics.fixable_sample_size || 0;
    setHtml("body-fixable-loss",
      `<div class="stat-row">` +
        `<div class="stat"><b class="neutral">${num(currentMetrics.total_loss)}</b><span>Total Δw</span></div>` +
        `<div class="stat"><b>${isNum(fixable) ? num(fixable) : "N/A"}</b><span>Fixable</span></div>` +
      `</div>` +
      `<p>Win% you dropped on moves a player at your level would realistically have found. ` +
      (sample
        ? `Based on <strong>${sample}</strong> full-tier reviews.`
        : `Full-tier analysis is queued in the background — reopen this run shortly.`) +
      `</p>` +
      (isNum(fixable) && currentMetrics.total_loss
        ? `<p><strong>${pct(fixable / currentMetrics.total_loss)}</strong> of your total loss was realistically avoidable; ` +
          `the rest was engine-only.</p>`
        : "")
    );

    const nat = currentMetrics.maia_naturalness || {};
    setHtml("body-maia-intuition",
      `<div class="stat-row">` +
        `<div class="stat"><b>${isNum(nat.naturalness_rate) ? pct(nat.naturalness_rate) : "N/A"}</b><span>Findable share</span></div>` +
      `</div>` +
      `<p><strong>${escapeHtml(nat.label || "Needs full-tier review")}</strong>` +
      (nat.sample_size ? ` · ${nat.sample_size} scored positions.` : "") + `</p>` +
      `<p style="font-size:0.82rem;color:var(--ins-mute)">Share of your positions where the right move was one a ` +
      `human policy at your rating would plausibly pick.</p>`
    );

    const steer = currentMetrics.volatility_steering || {};
    if (!steer.n_best && !steer.n_alt) {
      setHtml("body-steering", emptyBlock("No steering data yet."));
    } else {
      setHtml("body-steering",
        `<div class="stat-row">` +
          `<div class="stat"><b class="neutral">${num(steer.mean_vol_played_best, 1)}</b><span>Played best</span></div>` +
          `<div class="stat"><b class="neutral">${num(steer.mean_vol_played_alt, 1)}</b><span>Left the line</span></div>` +
        `</div>` +
        `<p>${escapeHtml(steer.note || "Your deviations do not systematically change how sharp the position becomes.")}</p>` +
        `<p style="font-size:0.82rem;color:var(--ins-mute)">n = ${steer.n_best || 0} best / ${steer.n_alt || 0} alternatives.</p>`
      );
    }

    const tags = (currentMetrics.missed_tactics || {}).tags || [];
    if (!tags.length) {
      emptyChart("chart-missed-tactics", "No tagged tactical misses in this window.");
    } else {
      clearEmpty("chart-missed-tactics");
      const top = tags.slice(0, 8);
      renderChart("chart-missed-tactics", {
        type: "bar",
        data: {
          labels: top.map((t) => titleCase(t.tag)),
          datasets: [
            {
              label: "Missed",
              data: top.map((t) => t.n),
              backgroundColor: "rgba(224, 68, 58, 0.72)",
              borderRadius: 5,
            },
            {
              label: "Findability > 60",
              data: top.map((t) => t.high_findability_n || 0),
              backgroundColor: "rgba(87, 232, 63, 0.6)",
              borderRadius: 5,
            },
          ],
        },
        options: baseOptions({ indexAxis: "y", legend: true }),
      });
    }

    const profile = (currentMetrics.volatility_profile || {}).buckets || [];
    if (!profile.some((b) => b.n)) {
      setHtml("body-vol-profile", emptyBlock("No volatility profile yet."));
    } else {
      setHtml("body-vol-profile",
        profile.map((b) => barRow(b.label, b.win_rate, b.n)).join("") +
        `<p style="font-size:0.82rem;color:var(--ins-mute)">Win rate by how sharp your games are on average.</p>`
      );
    }
  }

  // ── Section: Openings & endgames ────────────────────────────────────────

  function renderOpenings(aggIn) {
    const agg = aggIn || aggregate(filteredFacts());
    let rows = (pro().openings || {}).rows || [];
    if (openingColor !== "all") rows = rows.filter((r) => r.color === openingColor);

    if (!rows.length) {
      setHtml("body-opening-tree", emptyBlock("No opening data in this selection."));
    } else {
      setHtml("body-opening-tree",
        `<div class="table-scroll"><table class="dash-table">` +
        `<thead><tr><th>Opening</th><th>ECO</th><th>Side</th><th class="num">Games</th>` +
        `<th>Score</th><th class="num">Accuracy</th><th class="num">Opening Δw/move</th>` +
        `<th class="num">Leaves book</th><th class="num">Blunders/100</th></tr></thead><tbody>` +
        rows.slice(0, 40).map((r) => {
          const scorePct = isNum(r.score_pct) ? r.score_pct : null;
          return (
            `<tr>` +
            `<td class="strong">${escapeHtml(r.opening)}</td>` +
            `<td class="dim">${escapeHtml(r.eco || "—")}</td>` +
            `<td class="dim">${escapeHtml(titleCase(r.color))}</td>` +
            `<td class="num">${r.n}</td>` +
            `<td><div class="bar-cell"><div class="track${scorePct !== null && scorePct < 0.45 ? " is-bad" : ""}">` +
            `<i style="width:${Math.round((scorePct || 0) * 100)}%"></i></div><b>${pct(scorePct)}</b></div></td>` +
            `<td class="num">${isNum(r.mean_accuracy) ? `${num(r.mean_accuracy, 1)}%` : "—"}</td>` +
            `<td class="num">${num(r.opening_delta_w_per_move, 2)}</td>` +
            `<td class="num">${isNum(r.mean_deviation_ply) ? `ply ${num(r.mean_deviation_ply, 1)}` : "—"}</td>` +
            `<td class="num">${num(r.blunders_per_100, 1)}</td>` +
            `</tr>`
          );
        }).join("") +
        `</tbody></table></div>` +
        `<p style="margin-top:10px;font-size:0.82rem;color:var(--ins-mute)">` +
        `Score is your points per game in that opening. "Leaves book" is the average ply at which you play the first ` +
        `non-book move — the boundary between preparation and understanding.</p>`
      );
    }

    const rep = (currentMetrics.tier2 || {}).repertoire_depth || {};
    setHtml("body-repertoire-depth",
      `<div class="stat-row">` +
        `<div class="stat"><b class="neutral">${isNum(rep.mean_leave_book_ply) ? num(rep.mean_leave_book_ply) : "—"}</b><span>Leave book (ply)</span></div>` +
        `<div class="stat"><b class="${(rep.mean_delta_w_next_5 || 0) > 5 ? "bad" : ""}">${num(rep.mean_delta_w_next_5, 1)}</b><span>Next-5 Δw</span></div>` +
      `</div>` +
      `<p>How deep your preparation runs, and what the five moves right after it cost you. ` +
      `A high next-5 number means the opening choice is fine but the resulting plans are not.</p>` +
      (rep.n ? `<p style="font-size:0.82rem;color:var(--ins-mute)">n = ${rep.n} games.</p>` : "")
    );

    const castling = agg.castling || {};
    const order = ["kingside", "queenside", "never", "same_side", "opposite_side"];
    const castleRows = order.filter((k) => castling[k] && castling[k].n);
    setHtml("body-castling",
      castleRows.length
        ? castleRows.map((k) => {
            const c = castling[k];
            return barRow(titleCase(k), c.decided ? c.score / c.decided : null, c.n);
          }).join("") +
          `<p style="font-size:0.82rem;color:var(--ins-mute)">Points per game by where your king went — ` +
          `and whether the kings ended up on the same side.</p>`
        : emptyBlock("No castling data.")
    );

    const eg = pro().endgame || {};
    setHtml("body-endgame",
      `<div class="stat-row">` +
        `<div class="stat"><b class="neutral">${pct(eg.reach_rate)}</b><span>Reach endgame</span></div>` +
        `<div class="stat"><b class="${(eg.delta_w_per_move || 0) > 4 ? "bad" : ""}">${num(eg.delta_w_per_move, 2)}</b><span>Δw / move</span></div>` +
      `</div>` +
      ((eg.entry || []).some((e) => e.n)
        ? (eg.entry || []).filter((e) => e.n).map((e) => (
            `<div style="display:flex;justify-content:space-between;font-size:0.84rem;padding:3px 0">` +
            `<span style="color:var(--ins-dim)">${escapeHtml(e.label)}</span>` +
            `<strong>${pct(e.score_pct)} <span style="color:var(--ins-mute);font-weight:600">n=${e.n}</span></strong></div>`
          )).join("")
        : `<p>${eg.reached ? "Not enough decided endgames to split by entry evaluation." : "No endgames reached in this window."}</p>`)
    );

    const res = pro().resilience || {};
    const conv = res.conversion || {};
    const come = res.comeback || {};
    setHtml("body-resilience",
      `<div class="stat-row">` +
        `<div class="stat"><b class="${(conv.score_pct || 0) < 0.8 ? "bad" : ""}">${pct(conv.score_pct)}</b><span>From winning</span></div>` +
        `<div class="stat"><b class="neutral">${num(conv.points_dropped, 1)}</b><span>Points dropped</span></div>` +
        `<div class="stat"><b>${pct(come.score_pct)}</b><span>From losing</span></div>` +
        `<div class="stat"><b class="neutral">${num(come.points_rescued, 1)}</b><span>Points rescued</span></div>` +
      `</div>` +
      `<p>You reached a winning position (>${pct(conv.threshold)} win chance) in <strong>${conv.n || 0}</strong> games ` +
      `and a losing one (<${pct(come.threshold)}) in <strong>${come.n || 0}</strong>. ` +
      `Conversion and comeback are the measurable version of "do I close games out".</p>`
    );

    const missed = res.missed_wins || {};
    setHtml("body-missed-wins",
      missed.n
        ? `<div class="stat-row"><div class="stat"><b class="bad">${missed.n}</b><span>of ${missed.of} won positions</span></div></div>` +
          (missed.games || []).slice(0, 5).map((g) => (
            `<div style="display:flex;justify-content:space-between;gap:10px;font-size:0.82rem;padding:5px 0;border-top:1px solid var(--ins-line)">` +
            `<span style="color:var(--ins-dim)">vs ${escapeHtml(g.opponent || "—")}` +
            (g.biggest_miss && g.biggest_miss.san ? ` · ${escapeHtml(g.biggest_miss.san)}` : "") + `</span>` +
            `<button type="button" class="row-btn" data-missed-game="${escapeHtml(g.game_id)}">Review</button>` +
            `</div>`
          )).join("")
        : emptyBlock("No wins slipped away in this window.")
    );
    $("body-missed-wins").querySelectorAll("[data-missed-game]").forEach((b) => {
      b.addEventListener("click", () => {
        const g = (missed.games || []).find((x) => x.game_id === b.dataset.missedGame);
        goReview(b.dataset.missedGame, g && g.biggest_miss && g.biggest_miss.ply);
      });
    });
  }

  // ── Section: Time & mind ────────────────────────────────────────────────

  function renderTime(agg) {
    const scramble = (currentMetrics.time_scramble_decay || {}).buckets || [];
    if (!scramble.some((b) => b.moves)) {
      emptyChart("chart-scramble-decay", "These games carry no clock annotations.");
    } else {
      clearEmpty("chart-scramble-decay");
      renderChart("chart-scramble-decay", {
        type: "line",
        data: {
          labels: scramble.map((b) => b.label),
          datasets: [
            {
              label: "Δw per move",
              data: scramble.map((b) => Number(b.delta_w_per_move.toFixed(2))),
              borderColor: C.bad,
              backgroundColor: "rgba(224, 68, 58, 0.16)",
              fill: true,
              tension: 0.3,
              pointRadius: 4,
              yAxisID: "y",
            },
            {
              label: "Blunder rate %",
              data: scramble.map((b) => Number((b.blunder_rate * 100).toFixed(2))),
              borderColor: C.warn,
              backgroundColor: "transparent",
              borderDash: [5, 4],
              tension: 0.3,
              pointRadius: 3,
              yAxisID: "y1",
            },
          ],
        },
        options: {
          ...baseOptions({ legend: true }),
          scales: {
            x: { grid: { color: C.grid }, ticks: { color: C.mute, font: { size: 10 } } },
            y: { position: "left", min: 0, grid: { color: C.grid }, ticks: { color: C.mute, font: { size: 10 } } },
            y1: { position: "right", min: 0, grid: { display: false }, ticks: { color: C.warn, font: { size: 10 }, callback: (v) => `${v}%` } },
          },
        },
      });
    }

    const tier3 = currentMetrics.tier3 || {};
    const sessionIdx = (tier3.by_session_index || []).slice(0, 8);
    if (!sessionIdx.length) {
      emptyChart("chart-session-tilt", "Not enough timestamped games to split sessions.");
    } else {
      clearEmpty("chart-session-tilt");
      renderChart("chart-session-tilt", {
        type: "bar",
        data: {
          labels: sessionIdx.map((s) => `#${s.game_index}`),
          datasets: [{
            data: sessionIdx.map((s) => Math.round((s.win_rate || 0) * 100)),
            backgroundColor: sessionIdx.map((_, i) =>
              i === 0 ? C.green : `rgba(87, 232, 63, ${Math.max(0.25, 0.8 - i * 0.1)})`),
            borderRadius: 5,
          }],
        },
        options: baseOptions({ yMin: 0, yMax: 100, ySuffix: "%" }),
      });
    }

    const after = tier3.after_loss || {};
    const overallWin = agg.games ? agg.wins / agg.games : null;
    setHtml("body-after-loss",
      after.n
        ? `<div class="stat-row">` +
            `<div class="stat"><b class="${after.win_rate < (overallWin || 0) - 0.1 ? "bad" : ""}">${pct(after.win_rate)}</b><span>After a loss</span></div>` +
            `<div class="stat"><b class="neutral">${pct(overallWin)}</b><span>Overall</span></div>` +
          `</div>` +
          `<p>${after.n} games played immediately after a defeat` +
          (isNum(after.mean_accuracy) ? `, averaging ${num(after.mean_accuracy, 1)}% accuracy` : "") + `. ` +
          `${escapeHtml(tier3.note || "No clear tilt signal.")}</p>` +
          `<p style="font-size:0.82rem;color:var(--ins-mute)">${tier3.sessions || 0} sessions detected (split on a 2h gap).</p>`
        : emptyBlock("No back-to-back games detected in this window.")
    );

    const hours = Object.keys(agg.byHour).map(Number).sort((a, b) => a - b);
    if (!hours.length) {
      emptyChart("chart-by-hour", "No timestamps on these games.");
    } else {
      clearEmpty("chart-by-hour");
      renderChart("chart-by-hour", {
        type: "bar",
        data: {
          labels: hours.map((h) => `${String(h).padStart(2, "0")}:00`),
          datasets: [
            {
              type: "bar",
              label: "Games",
              data: hours.map((h) => agg.byHour[h].n),
              backgroundColor: "rgba(93, 104, 119, 0.5)",
              borderRadius: 4,
              yAxisID: "y1",
            },
            {
              type: "line",
              label: "Accuracy %",
              data: hours.map((h) => {
                const b = agg.byHour[h];
                return b.accN ? Number((b.accSum / b.accN).toFixed(1)) : null;
              }),
              borderColor: C.green,
              backgroundColor: "transparent",
              tension: 0.3,
              pointRadius: 3,
              spanGaps: true,
              yAxisID: "y",
            },
          ],
        },
        options: {
          ...baseOptions({ legend: true }),
          scales: {
            x: { grid: { color: C.grid }, ticks: { color: C.mute, font: { size: 10 } } },
            y: { position: "left", grid: { color: C.grid }, ticks: { color: C.mute, font: { size: 10 }, callback: (v) => `${v}%` }, suggestedMin: 40, suggestedMax: 100 },
            y1: { position: "right", min: 0, grid: { display: false }, ticks: { color: C.mute, font: { size: 10 }, precision: 0 } },
          },
        },
      });
    }

    const lengths = tier3.by_session_length || {};
    const lengthKeys = Object.keys(lengths).filter((k) => lengths[k].n);
    setHtml("body-session-length",
      lengthKeys.length
        ? lengthKeys.map((k) => {
            const b = lengths[k];
            return barRow(b.label || titleCase(k), b.win_rate, b.n);
          }).join("") +
          `<p style="font-size:0.82rem;color:var(--ins-mute)">Win rate by how many games you played in one sitting.</p>`
        : emptyBlock("No session data.")
    );

    const bands = (currentMetrics.tier2 || {}).opponent_relative || [];
    const shown = bands.filter((b) => b.n);
    setHtml("body-opponent-bands",
      shown.length
        ? `<div class="table-scroll"><table class="dash-table">` +
          `<thead><tr><th>Opponent band</th><th class="num">Games</th><th>Win rate</th>` +
          `<th class="num">Opening Δw/move</th><th class="num">Middlegame Δw/move</th><th class="num">Endgame Δw/move</th></tr></thead><tbody>` +
          shown.map((b) => {
            const byPhase = Object.fromEntries((b.phase_attribution || []).map((p) => [p.phase, p]));
            const cell = (p) => {
              const row = byPhase[p];
              if (!row || !row.moves) return `<td class="num dim">—</td>`;
              const v = row.delta_w_per_move;
              return `<td class="num" style="color:${v > 5 ? "var(--ins-bad)" : v > 3 ? "var(--ins-warn)" : "var(--ins-dim)"}">${num(v, 2)}</td>`;
            };
            return (
              `<tr><td class="strong">${escapeHtml(titleCase(b.band))} rated</td>` +
              `<td class="num">${b.n}</td>` +
              `<td><div class="bar-cell"><div class="track"><i style="width:${Math.round((b.win_rate || 0) * 100)}%"></i></div><b>${pct(b.win_rate)}</b></div></td>` +
              cell("opening") + cell("middlegame") + cell("endgame") +
              `</tr>`
            );
          }).join("") +
          `</tbody></table></div>` +
          `<p style="margin-top:10px;font-size:0.82rem;color:var(--ins-mute)">Bands are ±100 rating points around your own rating in each game. ` +
          `The classic pattern to look for: crushing lower-rated players tactically while collapsing against stronger ones in the endgame.</p>`
        : emptyBlock("No rated opponents in this window.")
    );
  }

  // ── Section: Games ──────────────────────────────────────────────────────

  function renderGames() {
    const tbody = $("dash-games-tbody");
    if (!tbody) return;
    let rows = filteredFacts();

    if (gamesSearch) {
      rows = rows.filter((g) =>
        [g.opponent, g.eco, g.opening_name].some((v) =>
          String(v || "").toLowerCase().includes(gamesSearch))
      );
    }

    const { key, dir } = gamesSort;
    const mul = dir === "asc" ? 1 : -1;
    rows = rows.slice().sort((a, b) => {
      const av = a[key], bv = b[key];
      if (av === bv) return 0;
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      return av > bv ? mul : -mul;
    });

    const title = $("games-title");
    if (title) title.textContent = `${rows.length} game${rows.length === 1 ? "" : "s"}`;

    gamesTable.querySelectorAll("th[data-sort]").forEach((th) => {
      th.classList.remove("sorted-asc", "sorted-desc");
      if (th.dataset.sort === key) th.classList.add(dir === "asc" ? "sorted-asc" : "sorted-desc");
    });

    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="11" class="dash-empty">No games match this selection.</td></tr>`;
      return;
    }

    tbody.innerHTML = rows.slice(0, 300).map((g) => {
      const outcomeCls = g.outcome === "win" ? "insights-pill--ok"
        : g.outcome === "loss" ? "insights-pill--err" : "insights-pill--busy";
      const miss = g.biggest_miss;
      return (
        `<tr>` +
        `<td class="dim">${escapeHtml(dateShort(g.played_at))}</td>` +
        `<td><span class="insights-pill">${escapeHtml(g.user_color === "white" ? "W" : "B")}</span></td>` +
        `<td><span class="strong">${escapeHtml(g.opponent)}</span> ` +
          `<span class="dim">${g.opponent_rating || "?"}${g.rating_band && g.rating_band !== "unknown" ? ` · ${escapeHtml(g.rating_band)}` : ""}</span></td>` +
        `<td class="dim">${escapeHtml(g.opening_name)}${g.eco && g.eco !== "Unknown" ? ` <span class="dim">(${escapeHtml(g.eco)})</span>` : ""}</td>` +
        `<td><span class="insights-pill ${outcomeCls}">${escapeHtml(g.result || "*")}</span></td>` +
        `<td class="num strong">${isNum(g.accuracy) ? `${num(g.accuracy, 1)}%` : "—"}</td>` +
        `<td class="num">${num(g.total_delta_w)}</td>` +
        `<td class="num" style="color:${g.blunders ? "var(--ins-bad)" : "var(--ins-mute)"}">${g.blunders}</td>` +
        `<td>${sparkline(g.sparkline, g.user_color)}</td>` +
        `<td>${miss && miss.san
            ? `<button type="button" class="row-btn" data-game-miss="${escapeHtml(g.game_id)}">${escapeHtml(miss.san)} · −${num(miss.delta_w)}</button>`
            : `<span class="dim">—</span>`}</td>` +
        `<td><button type="button" class="row-btn" data-review-game="${escapeHtml(g.game_id)}">Review</button></td>` +
        `</tr>`
      );
    }).join("");

    tbody.querySelectorAll("[data-review-game]").forEach((b) => {
      b.addEventListener("click", () => goReview(b.dataset.reviewGame));
    });
    tbody.querySelectorAll("[data-game-miss]").forEach((b) => {
      b.addEventListener("click", () => {
        const g = rows.find((r) => r.game_id === b.dataset.gameMiss);
        if (g && g.biggest_miss) {
          showPositionModal({
            ...g.biggest_miss,
            game_id: g.game_id,
            opponent: g.opponent,
            user_color: g.user_color,
          });
        }
      });
    });
  }

  /** Win% trajectory from the user's point of view, with a 50% midline. */
  function sparkline(points, color) {
    if (!points || points.length < 2) return `<span class="dim">—</span>`;
    const w = 92;
    const h = 22;
    const step = w / (points.length - 1);
    const flip = color === "black";
    const d = points.map((v, i) => {
      const value = flip ? 1 - v : v;
      const x = i * step;
      const y = h - Math.max(0, Math.min(1, value)) * h;
      return `${i === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(" ");
    return (
      `<svg class="sparkline-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">` +
      `<line class="sparkline-mid" x1="0" y1="${h / 2}" x2="${w}" y2="${h / 2}" />` +
      `<path class="sparkline-path" d="${d}" /></svg>`
    );
  }

  // ── Section: Practice set ───────────────────────────────────────────────

  function renderPractice() {
    const flags = currentMetrics.practice_flags || {};
    const items = flags.items || [];
    const callout = $("practice-callout");
    if (callout) {
      // Most cards say "findability pending", so claiming the whole set is
      // findable contradicts the cards right underneath it.
      const known = items.filter((i) => isNum(i.findability)).length;
      callout.textContent = items.length
        ? `${items.length} positions from your own games where the miss was expensive ` +
          `(Δw ≥ ${flags.delta_w_threshold || 15}). ` +
          (known
            ? `${known} of them are confirmed findable at your level; the rest are still on the ` +
              `shallow pass. `
            : "Findability needs the deeper pass, which is still pending on this snapshot. ") +
          `This is the set worth drilling — not engine-move trivia.`
        : "";
    }

    const grid = $("dash-practice-grid");
    if (!grid) return;
    if (!items.length) {
      grid.innerHTML = emptyBlock("No flagged misses in this snapshot — a good sign.");
      return;
    }

    grid.innerHTML = items.slice(0, 24).map((it, idx) => {
      const findable = isNum(it.findability);
      return (
        `<button type="button" class="practice-card" data-practice-idx="${idx}">` +
        `<div class="practice-card-head">` +
          `<span>${escapeHtml(it.san || it.move_uci || "?")}</span>` +
          `<span class="practice-card-badge">−${num(it.delta_w)} win%</span>` +
        `</div>` +
        `<div class="practice-card-meta">` +
          `${it.opponent ? `vs ${escapeHtml(it.opponent)} · ` : ""}move ${Math.ceil((it.ply || 0) / 2)}` +
          (isNum(it.volatility) ? ` · V ${num(it.volatility)}` : "") +
        `</div>` +
        `<span class="practice-card-find${findable ? "" : " is-shallow"}">` +
          (findable ? `◉ findability ${it.findability}` : "◌ shallow tier — findability pending") +
        `</span>` +
        `</button>`
      );
    }).join("");

    grid.querySelectorAll("[data-practice-idx]").forEach((card) => {
      card.addEventListener("click", () => {
        const item = items[Number(card.dataset.practiceIdx)];
        if (item) showPositionModal(item);
      });
    });
  }

  // ── Position modal ──────────────────────────────────────────────────────

  function showPositionModal(item) {
    if (!modalOverlay) return;
    const set = (id, text) => { const el = $(id); if (el) el.textContent = text; };

    set("modal-eyebrow", `Flagged miss · move ${Math.ceil((item.ply || 0) / 2)}`);
    set("modal-title", `${item.san || item.move_uci || "Position"} — ${num(item.delta_w)} win% lost`);
    set("modal-move-played", item.san || item.move_uci || "—");
    set("modal-move-best", item.best_san || item.best_uci || "—");
    set("modal-delta-w", `${num(item.delta_w)} win% points`);
    set("modal-findability", isNum(item.findability) ? `${item.findability} / 100` : "Shallow tier");
    set("modal-volatility", isNum(item.volatility) ? num(item.volatility) : "—");
    set("modal-caption", isNum(item.findability) && item.findability > 60
      ? "Costly and highly findable at your level — exactly the kind of miss that repeats until it is drilled."
      : "A costly miss. Open the full review to see the line the engine wanted.");

    const practiceBtn = $("modal-btn-practice");
    if (practiceBtn) practiceBtn.onclick = () => { hideModal(); goPractice("mistakes"); };
    const reviewBtn = $("modal-btn-review");
    if (reviewBtn) reviewBtn.onclick = () => { hideModal(); goReview(item.game_id, item.ply); };

    modalOverlay.classList.remove("hidden");
    modalOverlay.setAttribute("aria-hidden", "false");

    const boardEl = $("modal-board");
    if (boardEl && window.Chessground && item.fen) {
      const orientation = item.user_color === "black" ? "black" : "white";
      const config = { fen: item.fen, orientation, viewOnly: true, coordinates: false };
      if (item.best_uci && item.best_uci.length >= 4) {
        config.drawable = {
          autoShapes: [{
            orig: item.best_uci.slice(0, 2),
            dest: item.best_uci.slice(2, 4),
            brush: "green",
          }],
        };
      }
      if (!modalGround) modalGround = window.Chessground(boardEl, config);
      else modalGround.set(config);
    }
  }

  function hideModal() {
    if (!modalOverlay) return;
    modalOverlay.classList.add("hidden");
    modalOverlay.setAttribute("aria-hidden", "true");
  }

  const modalCloseBtn = $("modal-close");
  if (modalCloseBtn) modalCloseBtn.addEventListener("click", hideModal);
  if (modalOverlay) {
    modalOverlay.addEventListener("click", (e) => {
      if (e.target === modalOverlay) hideModal();
    });
  }

  // ── Loading a run into state ────────────────────────────────────────────

  /** A run stored before the current metric set exists — rebuildable for free. */
  function isStale(metrics) {
    return !metrics || !metrics.pro || !Array.isArray(metrics.game_explorer);
  }

  async function rebuildRun() {
    if (!activeRunId) return;
    setStatus("Rebuilding report from stored analysis…");
    if (openBtn) openBtn.disabled = true;
    try {
      const resp = await api(`/api/insights/${activeRunId}/recompute`, { method: "POST" });
      const data = await resp.json();
      if (data.metrics) {
        adoptMetrics(data.metrics, currentRunMeta);
        setStatus("Report rebuilt. Open it below.");
      } else {
        setStatus("Nothing to rebuild — re-run Generate for this window.");
      }
    } catch (err) {
      setStatus(`Rebuild failed: ${err.message}`);
    } finally {
      if (openBtn) openBtn.disabled = false;
    }
  }

  function adoptMetrics(metrics, meta) {
    currentMetrics = metrics;
    currentRunMeta = meta || {};
    syncFormToRun(currentRunMeta);
    updateLauncher(metrics, meta);
    if (window.__postmortemAdopt) window.__postmortemAdopt(metrics, currentRunMeta);
    if (dashboard && !dashboard.classList.contains("hidden")) renderDashboard();
  }

  // The form is also the controls for the report on screen: "Refresh" reruns
  // whatever the fields say. Leaving it on the defaults while a 7-day rapid
  // report is loaded means Refresh quietly produces a different report.
  function syncFormToRun(meta) {
    if (!meta) return;
    const handle = meta.handle || meta.chesscom_handle || "";
    if (usernameEl && handle) usernameEl.value = handle;
    if (sourceEl && meta.source) sourceEl.value = meta.source;
    const days = String(meta.window_days ?? "");
    if (windowEl && days && [...windowEl.options].some((o) => o.value === days)) {
      windowEl.value = days;
    }
    const tc = String(meta.time_class || "").toLowerCase();
    if (timeClassEl && tc && [...timeClassEl.options].some((o) => o.value === tc)) {
      timeClassEl.value = tc;
    }
  }

  function updateLauncher(metrics, meta) {
    const agg = aggregate(facts());
    // Prefer the fact table: it is what every panel, and the story, counts.
    // `games_analyzed` is the ingest tally and can lag it after a refresh,
    // which used to put "99 games analyzed" next to a 105-game W–D–L.
    const games = agg.games || (meta && isNum(meta.games_analyzed) ? meta.games_analyzed : 0);

    if (statGames) statGames.textContent = games ? String(games) : "—";
    if (statFixable) {
      statFixable.textContent = isNum(metrics.fixable_loss)
        ? String(Math.round(metrics.fixable_loss)) : "—";
    }
    if (statFixableHint) {
      statFixableHint.textContent = isNum(metrics.fixable_loss) && games
        ? `win% you could have kept · ${num(metrics.fixable_loss / games, 1)} per game`
        : "win% you could have kept";
    }
    const flagCount = (metrics.practice_flags && metrics.practice_flags.count) || 0;
    if (statPractice) statPractice.textContent = flagCount ? String(flagCount) : "—";
    if (dashPractice) dashPractice.disabled = flagCount <= 0;
    if (refreshBtn) refreshBtn.disabled = generating || !activeRunId;

    if (!readyCard) return;
    if (!games) { readyCard.classList.add("hidden"); return; }
    readyCard.classList.remove("hidden");

    const handle = (meta && (meta.handle || meta.chesscom_handle)) || "Your games";
    if (readyTitle) readyTitle.textContent = handle;

    // A run saved by an older build has none of the panels' inputs. Say so and
    // offer the rebuild rather than opening a report full of dashes — it is
    // pure arithmetic over stored moves, so it costs nothing.
    if (isStale(metrics)) {
      if (readySub) {
        readySub.textContent =
          `${games} games analyzed — this report predates the current metrics and needs rebuilding.`;
      }
      if (readyStats) readyStats.innerHTML = "";
      if (readyLeak) {
        readyLeak.innerHTML =
          "<strong>No re-analysis needed.</strong> Rebuilding recomputes every metric " +
          "from the moves already stored for these games.";
      }
      if (openBtn) {
        openBtn.querySelector("span").textContent = "Rebuild report";
        openBtn.onclick = rebuildRun;
      }
      if (deepBtn) deepBtn.classList.add("hidden");
      return;
    }

    if (openBtn) {
      openBtn.querySelector("span").textContent = "Why you lose";
      openBtn.onclick = () => openStory("verdict");
    }
    if (deepBtn) {
      deepBtn.classList.remove("hidden");
      deepBtn.onclick = openDashboard;
    }
    if (readySub) {
      readySub.textContent =
        `${sourceLabel(meta && meta.source)} · last ${meta && meta.window_days ? meta.window_days : "?"} days · ` +
        `${meta && meta.time_class ? meta.time_class : "—"} · ${games} games analyzed`;
    }
    // Say what the buttons above will do now, instead of leaving the initial
    // "pick a source and handle" prompt sitting under a finished report.
    if (!generating) {
      setStatus(
        "Generate runs the settings above. Refresh re-runs this report " +
        `(${handle} · last ${(meta && meta.window_days) || "?"} days · ` +
        `${(meta && meta.time_class) || "—"}), analyzing only new games.`,
      );
    }

    const head = (metrics.pro && metrics.pro.headline) || {};
    const elo = head.elo_left_on_board || {};
    if (readyStats) {
      readyStats.innerHTML =
        `<div class="rs"><b>${agg.wins}–${agg.draws}–${agg.losses}</b><span>W–D–L</span></div>` +
        `<div class="rs"><b>${isNum(agg.accuracy.mean) ? `${num(agg.accuracy.mean, 1)}%` : "—"}</b><span>Accuracy</span></div>` +
        `<div class="rs"><b>${agg.rating.perf ?? "—"}</b><span>Performance</span></div>` +
        `<div class="rs"><b style="color:var(--ins-green-hi)">${isNum(elo.points) ? `+${elo.points}` : "—"}</b><span>Elo on table</span></div>`;
    }

    const diagnosis = metrics.narrative && metrics.narrative.verdict
      && metrics.narrative.verdict.diagnosis;
    const leaks = (metrics.pro && metrics.pro.leaks) || [];
    if (readyLeak) {
      if (diagnosis) {
        readyLeak.innerHTML = `<strong>The pattern:</strong> ${escapeHtml(diagnosis)}`;
      } else if (leaks.length) {
        readyLeak.innerHTML = `<strong>Top leak:</strong> ${escapeHtml(leaks[0].title)} — ` +
          `${escapeHtml(leaks[0].detail)}`;
      } else {
        readyLeak.innerHTML = "";
      }
    }
  }

  // ── Runs list ───────────────────────────────────────────────────────────

  // Failures are stored as whatever the worker raised. A raw Python repr is
  // not a message to a player — keep it on hover, show a sentence.
  function runErrorText(detail) {
    const raw = String(detail || "").trim();
    if (!raw) return "This run failed before it finished.";
    if (/^[A-Za-z_.]*(Error|Exception)\b/.test(raw) || raw.includes("Traceback")) {
      return "This run failed before it finished — press Generate to try again.";
    }
    return raw.length > 160 ? `${raw.slice(0, 157)}…` : raw;
  }

  async function loadRuns() {
    try {
      const resp = await api("/api/insights?limit=10");
      const data = await resp.json();
      const runs = data.runs || [];
      // A bare "4" in the panel corner reads as a badge for nothing.
      if (runCountEl) {
        runCountEl.textContent = runs.length === 1 ? "1 snapshot" : `${runs.length} snapshots`;
        runCountEl.title = "The ten most recent runs are kept";
      }
      if (!runList) return;
      if (!runs.length) {
        runList.innerHTML = '<li class="insights-empty">No runs yet — generate your first snapshot.</li>';
        return;
      }
      const byId = new Map(runs.map((r) => [r.run_id, r]));
      runList.innerHTML = runs.map((r) => {
        const handle = r.handle || r.chesscom_handle || "—";
        const failed = String(r.status || "").toLowerCase() === "error";
        const fixable = r.metrics && isNum(r.metrics.fixable_loss)
          ? ` · fixable ${Math.round(r.metrics.fixable_loss)}` : "";
        const active = activeRunId === r.run_id ? " is-active" : "";
        // A failed run used to be a dead row: no reason, and clicking it only
        // said "no metrics yet". Show why it failed and offer the rerun.
        const why = failed && r.detail
          ? `<div class="run-error" title="${escapeHtml(String(r.detail))}">` +
            `${escapeHtml(runErrorText(r.detail))}</div>` : "";
        return (
          `<li class="${(active + (failed ? " is-error" : "")).trim()}" ` +
          `data-run-id="${escapeHtml(r.run_id || "")}" ` +
          `title="${escapeHtml(failed ? "Load these settings into the form" : "Open this report")}">` +
          `<div><div class="run-title">${escapeHtml(handle)}</div>` +
          `<div class="meta">${escapeHtml(sourceLabel(r.source))} · ${escapeHtml(String(r.window_days ?? "—"))}d ` +
          `${escapeHtml(r.time_class || "—")} · ${r.games_analyzed ?? 0} games${fixable}</div>${why}</div>` +
          statusPill(r.status) + `</li>`
        );
      }).join("");

      runList.querySelectorAll("li[data-run-id]").forEach((li) => {
        li.addEventListener("click", () => {
          const run = byId.get(li.dataset.runId);
          if (run && String(run.status || "").toLowerCase() === "error") {
            syncFormToRun(run);
            setStatus(
              `${runErrorText(run.detail)} Its settings are now in the form above — ` +
              "press Generate to try again.",
            );
            if (usernameEl) usernameEl.focus();
            return;
          }
          loadRun(li.dataset.runId);
        });
      });
    } catch (err) {
      if (runList) runList.innerHTML = `<li class="insights-empty">${escapeHtml(err.message)}</li>`;
    }
  }

  async function loadRun(runId) {
    if (!runId) return null;
    try {
      const resp = await api(`/api/insights/${runId}`);
      const data = await resp.json();
      if (!data.metrics) {
        setStatus(`Run is ${data.status || "pending"} — no metrics yet.`);
        return data;
      }
      activeRunId = runId;
      runList.querySelectorAll("li").forEach((el) =>
        el.classList.toggle("is-active", el.dataset.runId === runId));
      adoptMetrics(data.metrics, data);
      setDashHeader(data);
      setStatus(`Loaded ${data.games_analyzed || 0} games.`);
      return data;
    } catch (err) {
      setStatus(err.message);
      return null;
    }
  }

  function setDashHeader(meta) {
    if (dashHandle) dashHandle.textContent = (meta && (meta.handle || meta.chesscom_handle)) || "Insights";
    if (dashChips) {
      dashChips.innerHTML = [
        sourceLabel(meta && meta.source),
        `${(meta && meta.window_days) || "?"} days`,
        (meta && meta.time_class) || "—",
        // Same count the KPI row and the story use — see updateLauncher.
        `${facts().length || (meta && meta.games_analyzed) || 0} games`,
      ].map((c) => `<span>${escapeHtml(c)}</span>`).join("");
    }
  }

  async function loadLatest() {
    try {
      const resp = await api("/api/insights?limit=1");
      const data = await resp.json();
      const run = (data.runs || [])[0];
      if (!run || !run.metrics) {
        if (readyCard) readyCard.classList.add("hidden");
        return;
      }
      const metrics = typeof run.metrics === "string" ? JSON.parse(run.metrics) : run.metrics;
      activeRunId = run.run_id || activeRunId;
      adoptMetrics(metrics, run);
      setDashHeader(run);
    } catch {
      if (readyCard) readyCard.classList.add("hidden");
    }
  }

  // ── Generating ──────────────────────────────────────────────────────────

  async function generate() {
    if (generating) return;
    const username = (usernameEl && usernameEl.value.trim()) || "";
    if (!username) {
      setStatus("Enter a username first.");
      if (usernameEl) usernameEl.focus();
      return;
    }
    setBusy(true);
    if (capNote) capNote.classList.add("hidden");
    setStatus("Starting…");
    try {
      const runId = await startRun(
        username,
        Number((windowEl && windowEl.value) || 30),
        (timeClassEl && timeClassEl.value) || "blitz",
        (sourceEl && sourceEl.value) || "chesscom",
      );
      if (runId) {
        await pollRun(runId);
        await loadRuns();
      }
    } catch (err) {
      setStatus(err.message);
    } finally {
      setBusy(false);
    }
  }

  // Refresh used to be Generate with a different status string — two buttons,
  // one behaviour. It now re-runs the *loaded report's* settings through the
  // endpoint built for exactly that, so editing the form no longer changes
  // what "refresh this report" means.
  async function refreshRun() {
    if (generating) return;
    if (!activeRunId) {
      setStatus("Open a report first — Refresh re-runs the one you are looking at.");
      return;
    }
    setBusy(true);
    if (capNote) capNote.classList.add("hidden");
    setStatus("Refreshing this report — only new games are analyzed…");
    try {
      const resp = await api(`/api/insights/${activeRunId}/refresh`, { method: "POST" });
      const data = await resp.json();
      if (data.run_id) {
        await pollRun(data.run_id);
        await loadRuns();
      }
    } catch (err) {
      setStatus(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function startRun(username, windowDays, timeClass, source) {
    const resp = await api("/api/insights", {
      method: "POST",
      body: JSON.stringify({
        handle: username,
        chesscom_handle: username,
        source,
        window_days: windowDays,
        time_class: timeClass,
      }),
    });
    const data = await resp.json();
    return data.run_id;
  }

  async function pollRun(runId) {
    setStatus("Analyzing games…");
    activeRunId = runId;
    for (let i = 0; i < 7200; i++) {
      const resp = await api(`/api/insights/${runId}`);
      const data = await resp.json();
      if (data.games_capped && capNote) capNote.classList.remove("hidden");
      const progress = Math.round((data.progress || 0) * 100);
      setProgress(Math.max(8, progress));
      setStatus(`${data.status || "running"} — ${data.games_analyzed || 0} games (${progress}%)`);

      if (data.status === "complete" || data.status === "done") {
        setProgress(100);
        if (data.metrics) {
          adoptMetrics(data.metrics, data);
          setDashHeader(data);
        }
        setStatus(`Done — ${data.games_analyzed || 0} games analyzed. Open the report below.`);
        setBusy(false);
        scheduleRecompute(runId);
        return;
      }
      if (data.status === "error") {
        throw new Error(data.detail || data.message || "Insights run failed");
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    setBusy(false);
  }

  /** Full-tier upgrades land after the run completes; fold them in when they do. */
  function scheduleRecompute(runId) {
    const queued = currentMetrics
      && currentMetrics.practice_flags
      && currentMetrics.practice_flags.full_tier_queued;
    if (!queued) return;
    setTimeout(async () => {
      try {
        const resp = await api(`/api/insights/${runId}/recompute`, { method: "POST" });
        const data = await resp.json();
        if (data.metrics) adoptMetrics(data.metrics, currentRunMeta);
      } catch {
        /* best effort */
      }
    }, 12000);
  }

  // ── Init ────────────────────────────────────────────────────────────────

  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      generate();
    });
  }
  if (refreshBtn) refreshBtn.addEventListener("click", refreshRun);

  // The shell calls this on every route change; closing the overlay on exit
  // keeps the body scroll lock from leaking into the other tabs.
  window.__insightsSetActive = (active, path) => {
    if (active) {
      loadRuns();
      if (window.__postmortemRoute) window.__postmortemRoute(path || window.location.pathname);
    } else {
      closeDashboard();
      if (window.__postmortemClose) window.__postmortemClose({ silent: true });
    }
  };

  window.__insightsCloseDeepDive = closeDashboard;
  window.__insightsLoadRun = loadRun;
  window.__insightsActiveRunId = () => activeRunId;
  window.__insightsOpenDeepDive = openDashboard;
  window.__insightsGoPractice = goPractice;
  window.__insightsGoReview = goReview;

  loadRuns();
  loadLatest();
})();
