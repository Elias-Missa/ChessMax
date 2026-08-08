/* Insights Tab — ChessMax Performance Intelligence Dashboard */
(function () {
  const root = document.getElementById("insights-root");
  if (!root) return;

  const form = document.getElementById("insights-form");
  const sourceEl = document.getElementById("insights-source");
  const usernameEl = document.getElementById("insights-username");
  const windowEl = document.getElementById("insights-window");
  const timeClassEl = document.getElementById("insights-time-class");
  const generateBtn = document.getElementById("insights-generate");
  const refreshBtn = document.getElementById("insights-refresh");
  const statusEl = document.getElementById("insights-status");
  const capNote = document.getElementById("insights-cap-note");
  const practiceBtn = document.getElementById("insights-practice");
  const runList = document.getElementById("insights-run-list");
  const runCountEl = document.getElementById("insights-run-count");
  const metricsSection = document.getElementById("insights-metrics");
  const progressBar = document.getElementById("insights-progress-bar");
  const statGames = document.getElementById("insights-stat-games");
  const statFixable = document.getElementById("insights-stat-fixable");
  const statPractice = document.getElementById("insights-stat-practice");

  // Modal elements
  const modalOverlay = document.getElementById("insights-modal-overlay");
  const modalCloseBtn = document.getElementById("modal-close");
  const modalTitle = document.getElementById("modal-title");
  const modalEyebrow = document.getElementById("modal-eyebrow");
  const modalMovePlayed = document.getElementById("modal-move-played");
  const modalMoveBest = document.getElementById("modal-move-best");
  const modalDeltaW = document.getElementById("modal-delta-w");
  const modalFindability = document.getElementById("modal-findability");
  const modalVolatility = document.getElementById("modal-volatility");
  const modalCaption = document.getElementById("modal-caption");
  const modalBtnPractice = document.getElementById("modal-btn-practice");
  const modalBtnReview = document.getElementById("modal-btn-review");

  let generating = false;
  let activeRunId = null;
  let currentMetrics = null;
  let activeSubtab = "overview";
  let activeFilters = { color: "all", outcome: "all" };
  let modalGround = null;

  // Chart.js instances dictionary
  const chartInstances = {};

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  function setProgress(pct) {
    if (!progressBar) return;
    const p = Math.max(4, Math.min(100, Number(pct) || 0));
    progressBar.style.setProperty("--p", `${p}%`);
    progressBar.style.width = `${p}%`;
  }

  function setBusy(busy) {
    generating = busy;
    if (generateBtn) generateBtn.disabled = busy;
    if (refreshBtn) refreshBtn.disabled = busy;
    root.classList.toggle("is-busy", !!busy);
    if (busy) setProgress(8);
    else setProgress(100);
  }

  function updateHeroStats(metrics, gamesAnalyzed) {
    if (statGames) {
      statGames.textContent =
        gamesAnalyzed != null
          ? String(gamesAnalyzed)
          : metrics && metrics.games != null
            ? String(metrics.games)
            : "—";
    }
    if (statFixable) {
      statFixable.textContent =
        metrics && metrics.fixable_loss != null
          ? String(Math.round(metrics.fixable_loss))
          : "—";
    }
    if (statPractice) {
      const n =
        (metrics && metrics.practice_flags && metrics.practice_flags.count) || 0;
      statPractice.textContent = n ? String(n) : "—";
    }
    if (practiceBtn) {
      const n = (metrics && metrics.practice_flags && metrics.practice_flags.count) || 0;
      practiceBtn.disabled = n <= 0;
    }
  }

  function statusPill(status) {
    const s = String(status || "—").toLowerCase();
    let cls = "insights-pill";
    if (s === "complete" || s === "done") cls += " insights-pill--ok";
    else if (s === "error") cls += " insights-pill--err";
    else if (s === "running" || s === "pending") cls += " insights-pill--busy";
    return `<span class="${cls}">${escapeHtml(status || "—")}</span>`;
  }

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

  function sourceLabel(source) {
    return source === "lichess" ? "Lichess" : "Chess.com";
  }

  function practiceHref(kind) {
    if (kind === "forced") return "/training/forced";
    if (kind === "defense") return "/training/defense";
    if (kind === "guess") return "/training/guess-eval";
    return "/training/mistakes";
  }

  function goPractice(kind) {
    const path = practiceHref(kind);
    if (window.__shellNavigate) window.__shellNavigate(path);
    else if (window.__shellSwitchTab) window.__shellSwitchTab("train");
  }

  // ── Sub-Tab Navigation & Filtering ─────────────────────────────────────

  function initSubTabs() {
    const subnav = document.getElementById("insights-subnav");
    if (!subnav) return;
    subnav.querySelectorAll(".insights-subtab").forEach((btn) => {
      btn.addEventListener("click", () => {
        const subtab = btn.getAttribute("data-subtab");
        if (!subtab || subtab === activeSubtab) return;
        activeSubtab = subtab;
        subnav.querySelectorAll(".insights-subtab").forEach((b) => {
          const isActive = b.getAttribute("data-subtab") === subtab;
          b.classList.toggle("active", isActive);
          b.setAttribute("aria-selected", isActive ? "true" : "false");
        });
        document.querySelectorAll(".insights-subview").forEach((view) => {
          view.classList.toggle("hidden", view.id !== `insights-view-${subtab}`);
        });
        if (currentMetrics) {
          renderActiveSubtab(currentMetrics);
        }
      });
    });

    // Filter buttons
    document.querySelectorAll(".filter-btn[data-filter-color]").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".filter-btn[data-filter-color]").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        activeFilters.color = btn.getAttribute("data-filter-color") || "all";
        if (currentMetrics) renderActiveSubtab(currentMetrics);
      });
    });

    document.querySelectorAll(".filter-btn[data-filter-outcome]").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".filter-btn[data-filter-outcome]").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        activeFilters.outcome = btn.getAttribute("data-filter-outcome") || "all";
        if (currentMetrics) renderActiveSubtab(currentMetrics);
      });
    });

    if (practiceBtn) {
      practiceBtn.addEventListener("click", () => goPractice("mistakes"));
    }

    // Modal close
    if (modalCloseBtn) modalCloseBtn.addEventListener("click", hideModal);
    if (modalOverlay) {
      modalOverlay.addEventListener("click", (e) => {
        if (e.target === modalOverlay) hideModal();
      });
    }
  }

  // ── Render Dashboard Views & Chart.js ───────────────────────────────────

  function renderMetrics(metrics, gamesAnalyzed) {
    currentMetrics = metrics;
    updateHeroStats(metrics, gamesAnalyzed);
    renderAICoachTakeaways(metrics.ai_coach_takeaways || []);
    renderActiveSubtab(metrics);

    // Auto re-computation trigger if full-tier features were queued
    if (metrics.practice_flags && metrics.practice_flags.full_tier_queued > 0 && activeRunId) {
      setTimeout(async () => {
        try {
          const resp = await api(`/api/insights/${activeRunId}/recompute`, { method: "POST" });
          const data = await resp.json();
          if (data.metrics) {
            currentMetrics = data.metrics;
            updateHeroStats(data.metrics, data.metrics.games);
            renderActiveSubtab(data.metrics);
          }
        } catch {
          /* best effort */
        }
      }, 12000);
    }
  }

  function renderAICoachTakeaways(takeaways) {
    const el = document.getElementById("insights-coach-takeaways");
    if (!el) return;
    if (!takeaways || !takeaways.length) {
      el.innerHTML = "<li>Consistent overall play. Practice tactical misses to sharpen precision.</li>";
      return;
    }
    el.innerHTML = takeaways
      .map((t) => `<li>${escapeHtml(t)}</li>`)
      .join("");
  }

  function renderActiveSubtab(m) {
    if (activeSubtab === "overview") renderOverview(m);
    else if (activeSubtab === "tactics") renderTactics(m);
    else if (activeSubtab === "openings") renderOpenings(m);
    else if (activeSubtab === "psychology") renderPsychology(m);
    else if (activeSubtab === "explorer") renderExplorer(m);
  }

  // ── Sub-Tab 1: Overview ────────────────────────────────────────────────

  function renderOverview(m) {
    // 1. Accuracy Timeline Chart
    const accData = (m.tier2 && m.tier2.standard && m.tier2.standard.accuracy_over_time) || [];
    renderChart("chart-accuracy-timeline", {
      type: "line",
      data: {
        labels: accData.map((d, i) => `#${i + 1}`),
        datasets: [
          {
            label: "Game Accuracy %",
            data: accData.map((d) => Math.round(d.accuracy)),
            borderColor: "#57e83f",
            backgroundColor: "rgba(68, 214, 44, 0.12)",
            fill: true,
            tension: 0.35,
            pointRadius: 4,
            pointHoverRadius: 6,
          },
        ],
      },
      options: chartOptions({ yMin: 40, yMax: 100, ySuffix: "%" }),
    });

    // 2. Fixable Loss Card
    const fixCard = document.getElementById("body-fixable-loss");
    if (fixCard) {
      const total = m.total_loss != null ? Math.round(m.total_loss) : "—";
      const fixable = m.fixable_loss != null ? Math.round(m.fixable_loss) : "N/A";
      const sample = m.fixable_sample_size || 0;
      fixCard.innerHTML =
        `<div class="stat-row">` +
        `<div class="stat"><b>${total}</b><span>Total Δw</span></div>` +
        `<div class="stat"><b>${fixable}</b><span>Fixable</span></div>` +
        `</div>` +
        `<p style="margin-top:8px">Win% dropped on humanly findable moves` +
        (sample ? ` · n=${sample} full reviews.` : ` · background full tier queued.`) +
        `</p>`;
    }

    // 3. Maia Intuition Card
    const maiaCard = document.getElementById("body-maia-intuition");
    if (maiaCard) {
      const nat = m.maia_naturalness || {};
      const rateStr = nat.naturalness_rate != null ? `${Math.round(nat.naturalness_rate * 100)}%` : "N/A";
      maiaCard.innerHTML =
        `<div class="stat-row">` +
        `<div class="stat"><b>${rateStr}</b><span>Match Rate</span></div>` +
        `</div>` +
        `<p style="margin-top:8px"><strong>${escapeHtml(nat.label || "Maia Intuition")}</strong>` +
        (nat.sample_size ? ` · based on ${nat.sample_size} findability points.` : "") +
        `</p>`;
    }
  }

  // ── Sub-Tab 2: Tactics & Blunders ──────────────────────────────────────

  function renderTactics(m) {
    // 1. Loss Taxonomy Donut Chart
    const tax = m.loss_taxonomy && m.loss_taxonomy.counts ? m.loss_taxonomy.counts : {};
    const taxKeys = Object.keys(tax);
    const taxVals = Object.values(tax);
    renderChart("chart-loss-taxonomy", {
      type: "doughnut",
      data: {
        labels: taxKeys.map((k) => k.replaceAll("_", " ")),
        datasets: [
          {
            data: taxVals,
            backgroundColor: ["#e0443a", "#e8a53d", "#3db8c5", "#5d6877", "#44d62c"],
            borderWidth: 1,
            borderColor: "#1d2026",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "right", labels: { color: "#98a0aa" } } },
      },
    });

    // 2. Missed Tactics Chart
    const mt = (m.missed_tactics && m.missed_tactics.tags) || [];
    renderChart("chart-missed-tactics", {
      type: "bar",
      data: {
        labels: mt.slice(0, 6).map((t) => (t.tag || "").replaceAll("_", " ")),
        datasets: [
          {
            label: "Missed Count",
            data: mt.slice(0, 6).map((t) => t.n || 0),
            backgroundColor: "rgba(224, 68, 58, 0.75)",
            borderColor: "#e0443a",
            borderWidth: 1,
          },
        ],
      },
      options: chartOptions({ indexAxis: "y", xMin: 0 }),
    });

    // 3. Practice Cards Grid
    const grid = document.getElementById("insights-practice-grid");
    if (grid) {
      const items = (m.practice_flags && m.practice_flags.items) || [];
      if (!items.length) {
        grid.innerHTML = '<p class="insights-empty">No flagged mistakes found in this snapshot.</p>';
        return;
      }
      grid.innerHTML = items
        .slice(0, 8)
        .map((it, idx) => {
          const moveText = escapeHtml(it.san || it.move_uci || "?");
          const deltaText = `Δw ${Math.round(it.delta_w || 0)}`;
          const findText = it.findability != null ? `find ${it.findability}` : "shallow";
          const oppText = it.opponent ? `vs ${escapeHtml(it.opponent)}` : "";
          return (
            `<div class="practice-card" data-practice-idx="${idx}">` +
            `<div class="practice-card-head"><span>${moveText}</span><span class="practice-card-badge">${deltaText}</span></div>` +
            `<div class="practice-card-meta">${findText} ${oppText}</div>` +
            `</div>`
          );
        })
        .join("");

      grid.querySelectorAll(".practice-card[data-practice-idx]").forEach((card) => {
        card.addEventListener("click", () => {
          const idx = Number(card.getAttribute("data-practice-idx"));
          if (items[idx]) showPositionModal(items[idx]);
        });
      });
    }
  }

  // ── Sub-Tab 3: Openings & Phases ───────────────────────────────────────

  function renderOpenings(m) {
    const t2 = m.tier2 || {};

    // 1. Phase Attribution Grouped Bar Chart
    const phases = t2.phase_attribution || [];
    renderChart("chart-phase-attribution", {
      type: "bar",
      data: {
        labels: phases.map((p) => (p.phase || "").toUpperCase()),
        datasets: [
          {
            label: "Δw per move lost",
            data: phases.map((p) => p.delta_w_per_move || 0),
            backgroundColor: ["#3db8c5", "#e8a53d", "#e0443a"],
            borderRadius: 6,
          },
        ],
      },
      options: chartOptions({ yMin: 0 }),
    });

    // 2. Repertoire Depth Card
    const repCard = document.getElementById("body-repertoire-depth");
    if (repCard) {
      const r = t2.repertoire_depth || {};
      repCard.innerHTML =
        `<div class="stat-row">` +
        `<div class="stat"><b>${r.mean_leave_book_ply != null ? Math.round(r.mean_leave_book_ply) : "—"}</b><span>Leave book</span></div>` +
        `<div class="stat"><b>${r.mean_delta_w_next_5 != null ? r.mean_delta_w_next_5.toFixed(1) : "—"}</b><span>Next-5 Δw</span></div>` +
        `</div>`;
    }

    // 3. Castling Card
    const castleCard = document.getElementById("body-castling-stats");
    if (castleCard) {
      const c = t2.castling || {};
      const rows = Object.entries(c)
        .filter(([, v]) => (v.n || 0) > 0)
        .map(([k, v]) => `<div class="meta"><strong>${escapeHtml(k.replaceAll("_", " "))}</strong>: ${pct(v.win_rate)} (n=${v.n})</div>`)
        .join("");
      castleCard.innerHTML = rows || '<p class="insights-empty">No castling data.</p>';
    }

    // 4. ECO Openings Table
    const ecoCard = document.getElementById("body-eco-table");
    if (ecoCard) {
      const ecoRows = (t2.standard && t2.standard.by_eco) || [];
      if (!ecoRows.length) {
        ecoCard.innerHTML = '<p class="insights-empty">No ECO data.</p>';
        return;
      }
      const tableHtml =
        `<table class="insights-table">` +
        `<thead><tr><th>ECO</th><th>Games</th><th>Win Rate</th></tr></thead>` +
        `<tbody>` +
        ecoRows.map((e) => `<tr><td><strong>${escapeHtml(e.eco)}</strong></td><td>${e.n}</td><td>${pct(e.win_rate)}</td></tr>`).join("") +
        `</tbody></table>`;
      ecoCard.innerHTML = tableHtml;
    }
  }

  // ── Sub-Tab 4: Speed & Psychology ──────────────────────────────────────

  function renderPsychology(m) {
    // 1. Scramble Decay Chart
    const scramble = (m.time_scramble_decay && m.time_scramble_decay.buckets) || [];
    renderChart("chart-scramble-decay", {
      type: "line",
      data: {
        labels: scramble.map((b) => b.label),
        datasets: [
          {
            label: "Δw per move",
            data: scramble.map((b) => b.delta_w_per_move),
            borderColor: "#e0443a",
            backgroundColor: "rgba(224, 68, 58, 0.15)",
            fill: true,
            tension: 0.3,
          },
        ],
      },
      options: chartOptions({ yMin: 0 }),
    });

    // 2. Time vs Criticality Chart
    const tc = m.time_vs_criticality || {};
    renderChart("chart-time-criticality", {
      type: "bar",
      data: {
        labels: ["High Volatility (>60)", "Quiet Position (<30)"],
        datasets: [
          {
            label: "Avg Think Time (seconds)",
            data: [tc.avg_time_high_vol || 0, tc.avg_time_low_vol || 0],
            backgroundColor: ["#e8a53d", "#3db8c5"],
            borderRadius: 6,
          },
        ],
      },
      options: chartOptions({ yMin: 0 }),
    });

    // 3. Session & Tilt Chart
    const t3 = m.tier3 || {};
    const sessionIdx = t3.by_session_index || [];
    renderChart("chart-session-tilt", {
      type: "bar",
      data: {
        labels: sessionIdx.slice(0, 6).map((s) => `Game #${s.game_index}`),
        datasets: [
          {
            label: "Win Rate %",
            data: sessionIdx.slice(0, 6).map((s) => Math.round((s.win_rate || 0) * 100)),
            backgroundColor: "#57e83f",
            borderRadius: 6,
          },
        ],
      },
      options: chartOptions({ yMin: 0, yMax: 100, ySuffix: "%" }),
    });
  }

  // ── Sub-Tab 5: Game Explorer ───────────────────────────────────────────

  function renderExplorer(m) {
    const tbody = document.getElementById("insights-explorer-tbody");
    const searchInput = document.getElementById("insights-explorer-search");
    if (!tbody) return;

    let games = m.game_explorer || [];
    const query = searchInput ? searchInput.value.trim().toLowerCase() : "";

    // Apply active filters
    games = games.filter((g) => {
      if (activeFilters.color !== "all" && g.user_color !== activeFilters.color) return false;
      if (activeFilters.outcome !== "all" && g.outcome !== activeFilters.outcome) return false;
      if (query) {
        const matchOpp = (g.opponent || "").toLowerCase().includes(query);
        const matchEco = (g.eco || "").toLowerCase().includes(query);
        if (!matchOpp && !matchEco) return false;
      }
      return true;
    });

    if (!games.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="insights-empty">No matching games.</td></tr>';
      return;
    }

    tbody.innerHTML = games
      .slice(0, 50)
      .map((g) => {
        const dateStr = g.played_at ? String(g.played_at).slice(0, 10) : "—";
        const colorBadge = `<span class="insights-pill">${escapeHtml(g.user_color)}</span>`;
        const outcomeClass = g.outcome === "win" ? "insights-pill--ok" : g.outcome === "loss" ? "insights-pill--err" : "insights-pill--busy";
        const outcomeBadge = `<span class="insights-pill ${outcomeClass}">${escapeHtml(g.result)}</span>`;
        const accStr = g.accuracy != null ? `${Math.round(g.accuracy)}%` : "—";
        const sparkSvg = renderSparklineSvg(g.sparkline || []);

        return (
          `<tr>` +
          `<td>${escapeHtml(dateStr)}</td>` +
          `<td>${colorBadge}</td>` +
          `<td><strong>${escapeHtml(g.opponent)}</strong> <span class="meta">(${g.opponent_rating || "?"})</span></td>` +
          `<td>${escapeHtml(g.eco)}</td>` +
          `<td>${outcomeBadge}</td>` +
          `<td>${accStr}</td>` +
          `<td>${sparkSvg}</td>` +
          `<td><button type="button" class="insights-ghost" data-explorer-game="${escapeHtml(g.game_id)}">Review</button></td>` +
          `</tr>`
        );
      })
      .join("");

    tbody.querySelectorAll("button[data-explorer-game]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const gid = btn.getAttribute("data-explorer-game");
        if (gid && window.__shellNavigate) window.__shellNavigate(`/game-review?game=${gid}`);
      });
    });

    if (searchInput) {
      searchInput.oninput = () => renderExplorer(m);
    }
  }

  function renderSparklineSvg(points) {
    if (!points || !points.length) return "—";
    const w = 80;
    const h = 20;
    const step = w / Math.max(1, points.length - 1);
    const pathD = points
      .map((val, idx) => {
        const x = idx * step;
        const y = h - val * h;
        return `${idx === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
      })
      .join(" ");
    return `<svg class="sparkline-svg"><path class="sparkline-path" d="${pathD}" /></svg>`;
  }

  // ── Position Inspector Modal (Chessground) ─────────────────────────────

  function showPositionModal(item) {
    if (!modalOverlay) return;

    if (modalEyebrow) modalEyebrow.textContent = `Flagged Miss (Ply ${item.ply || 0})`;
    if (modalTitle) modalTitle.textContent = `${item.san || item.move_uci || "Position"} — Δw ${Math.round(item.delta_w || 0)}`;
    if (modalMovePlayed) modalMovePlayed.textContent = item.san || item.move_uci || "—";
    if (modalMoveBest) modalMoveBest.textContent = item.best_uci || "Engine top move";
    if (modalDeltaW) modalDeltaW.textContent = `${Math.round(item.delta_w || 0)} win% points`;
    if (modalFindability) modalFindability.textContent = item.findability != null ? `${item.findability} / 100` : "Shallow tier";
    if (modalVolatility) modalVolatility.textContent = item.volatility != null ? Math.round(item.volatility) : "—";
    if (modalCaption) {
      modalCaption.textContent = item.findability != null && item.findability > 60
        ? "This move was costly but highly findable for a human player at your rating level."
        : "Costly position miss. Practice in Trainer to build pattern recognition.";
    }

    if (modalBtnPractice) {
      modalBtnPractice.onclick = () => {
        hideModal();
        goPractice("mistakes");
      };
    }
    if (modalBtnReview) {
      modalBtnReview.onclick = () => {
        hideModal();
        if (item.game_id && window.__shellNavigate) window.__shellNavigate(`/game-review?game=${item.game_id}`);
      };
    }

    modalOverlay.classList.remove("hidden");
    modalOverlay.setAttribute("aria-hidden", "false");

    // Init Chessground board inside modal
    const boardEl = document.getElementById("modal-board");
    if (boardEl && window.Chessground) {
      const fen = item.fen || "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
      const orientation = item.user_color === "black" ? "black" : "white";
      if (!modalGround) {
        modalGround = window.Chessground(boardEl, {
          fen: fen,
          orientation: orientation,
          viewOnly: true,
        });
      } else {
        modalGround.set({ fen: fen, orientation: orientation });
      }
    }
  }

  function hideModal() {
    if (modalOverlay) {
      modalOverlay.classList.add("hidden");
      modalOverlay.setAttribute("aria-hidden", "true");
    }
  }

  // ── Chart.js Helper ────────────────────────────────────────────────────

  function renderChart(id, config) {
    const canvas = document.getElementById(id);
    if (!canvas || !window.Chart) return;
    if (chartInstances[id]) {
      chartInstances[id].destroy();
    }
    chartInstances[id] = new window.Chart(canvas, config);
  }

  function chartOptions(opts) {
    const o = opts || {};
    return {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: o.indexAxis || "x",
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: "rgba(255,255,255,0.06)" }, ticks: { color: "#98a0aa" } },
        y: {
          min: o.yMin,
          max: o.yMax,
          grid: { color: "rgba(255,255,255,0.06)" },
          ticks: { color: "#98a0aa", callback: (v) => `${v}${o.ySuffix || ""}` },
        },
      },
    };
  }

  // ── Run Loading & Management ────────────────────────────────────────────

  async function loadRuns() {
    try {
      const resp = await api("/api/insights?limit=10");
      const data = await resp.json();
      const runs = data.runs || [];
      if (runCountEl) runCountEl.textContent = String(runs.length);
      if (!runList) return;
      if (!runs.length) {
        runList.innerHTML = '<li class="insights-empty">No runs yet — generate your first snapshot.</li>';
        return;
      }
      runList.innerHTML = runs
        .map((r) => {
          const handle = r.handle || r.chesscom_handle || "—";
          const source = sourceLabel(r.source || "chesscom");
          const status = r.status || "—";
          const games = r.games_analyzed ?? 0;
          const windowDays = r.window_days ?? "—";
          const tc = r.time_class || "—";
          const fixable = r.metrics && r.metrics.fixable_loss != null ? ` · fixable ${Math.round(r.metrics.fixable_loss)}` : "";
          const active = activeRunId && activeRunId === r.run_id ? " is-active" : "";
          return (
            `<li class="${active.trim()}" data-run-id="${escapeHtml(r.run_id || "")}">` +
            `<div><div class="run-title">${escapeHtml(handle)}</div>` +
            `<div class="meta">${escapeHtml(source)} · ${escapeHtml(String(windowDays))}d ${escapeHtml(tc)} · ${games} games${fixable}</div></div>` +
            statusPill(status) +
            `</li>`
          );
        })
        .join("");

      runList.querySelectorAll("li[data-run-id]").forEach((li) => {
        li.addEventListener("click", async () => {
          const id = li.getAttribute("data-run-id");
          if (!id) return;
          try {
            const resp = await api(`/api/insights/${id}`);
            const data = await resp.json();
            activeRunId = id;
            runList.querySelectorAll("li").forEach((el) => el.classList.remove("is-active"));
            li.classList.add("is-active");
            if (data.metrics) {
              renderMetrics(data.metrics, data.games_analyzed);
              if (metricsSection) metricsSection.classList.remove("hidden");
            }
            setStatus(`Loaded run — ${data.games_analyzed || 0} games.`);
          } catch (err) {
            setStatus(err.message);
          }
        });
      });
    } catch (err) {
      if (runList) runList.innerHTML = `<li class="insights-empty">${escapeHtml(err.message)}</li>`;
    }
  }

  async function loadMetrics() {
    if (!metricsSection) return;
    try {
      const resp = await api("/api/insights?limit=1");
      const data = await resp.json();
      const run = (data.runs || [])[0];
      if (!run || !run.metrics) {
        metricsSection.classList.add("hidden");
        return;
      }
      const m = typeof run.metrics === "string" ? JSON.parse(run.metrics) : run.metrics;
      activeRunId = run.run_id || activeRunId;
      renderMetrics(m, run.games_analyzed);
      metricsSection.classList.remove("hidden");
    } catch {
      metricsSection.classList.add("hidden");
    }
  }

  function pct(rate) {
    if (rate == null || Number.isNaN(Number(rate))) return "—";
    return `${Math.round(Number(rate) * 100)}%`;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function generate({ refresh = false } = {}) {
    if (generating) return;
    const username = (usernameEl && usernameEl.value.trim()) || "";
    if (!username) {
      setStatus("Enter a username first.");
      return;
    }
    const source = (sourceEl && sourceEl.value) || "chesscom";
    const windowDays = Number((windowEl && windowEl.value) || 30);
    const timeClass = (timeClassEl && timeClassEl.value) || "blitz";
    setBusy(true);
    if (capNote) capNote.classList.add("hidden");
    setStatus(refresh ? "Refreshing…" : "Starting…");

    try {
      const runId = await tryStartInsightsRun(username, windowDays, timeClass, source);
      if (runId) {
        await pollInsightsRun(runId);
        await loadRuns();
        await loadMetrics();
        return;
      }
    } catch (err) {
      setStatus(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function tryStartInsightsRun(username, windowDays, timeClass, source) {
    const resp = await fetch("/api/insights", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        handle: username,
        chesscom_handle: username,
        source: source || "chesscom",
        window_days: windowDays,
        time_class: timeClass,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "Insights start failed");
    }
    const data = await resp.json();
    return data.run_id;
  }

  async function pollInsightsRun(runId) {
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
        if (data.metrics) renderMetrics(data.metrics, data.games_analyzed);
        if (metricsSection) metricsSection.classList.remove("hidden");
        setStatus(`Done — ${data.games_analyzed || 0} games analyzed.`);
        setBusy(false);
        return;
      }
      if (data.status === "error") {
        throw new Error(data.detail || data.message || "Insights run failed");
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    setBusy(false);
  }

  // ── Init ───────────────────────────────────────────────────────────────

  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      generate();
    });
  }

  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => generate({ refresh: true }));
  }

  initSubTabs();
  loadRuns();
  loadMetrics();
})();
