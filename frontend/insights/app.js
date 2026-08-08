/* Insights tab: chess.com window → Mistakes generate → practice deep link. */
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
  const metricsBody = document.getElementById("insights-metrics-body");
  const progressBar = document.getElementById("insights-progress-bar");
  const statGames = document.getElementById("insights-stat-games");
  const statFixable = document.getElementById("insights-stat-fixable");
  const statPractice = document.getElementById("insights-stat-practice");

  let generating = false;
  let lastUnsolved = 0;
  let activeRunId = null;

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
        (metrics && metrics.practice_flags && metrics.practice_flags.count) ||
        lastUnsolved ||
        0;
      statPractice.textContent = n ? String(n) : "—";
    }
  }

  function practiceBtnHtml(kind, label) {
    return (
      `<div class="insights-card-foot">` +
      `<button type="button" class="insights-ghost insights-practice-link" data-practice="${kind}">` +
      `${escapeHtml(label)}</button></div>`
    );
  }

  function metricCard(title, bodyHtml, opts) {
    const o = opts || {};
    const cls = ["insights-metric-card"];
    if (o.trend) cls.push("insights-metric-card--trend");
    if (o.wide) cls.push("insights-metric-card--wide");
    return (
      `<article class="${cls.join(" ")}">` +
      (o.eyebrow ? `<p class="eyebrow">${escapeHtml(o.eyebrow)}</p>` : "") +
      `<h3>${escapeHtml(title)}</h3>${bodyHtml}</article>`
    );
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

  async function consumeEventStream(body, onEvent) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const chunk of parts) {
        let event = "message";
        let data = "";
        for (const line of chunk.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        let parsed;
        try {
          parsed = JSON.parse(data);
        } catch {
          parsed = { message: data };
        }
        onEvent(event, parsed);
      }
    }
  }

  function sourceLabel(source) {
    if (source === "lichess") return "Lichess";
    return "Chess.com";
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
    else if (window.__shellSwitchTab) window.__shellSwitchTab(kind === "mistakes" ? "mistakes" : kind);
  }

  async function loadRuns() {
    try {
      const resp = await api("/api/insights?limit=10");
      const data = await resp.json();
      const runs = data.runs || [];
      if (runCountEl) runCountEl.textContent = String(runs.length);
      if (!runList) return;
      if (!runs.length) {
        runList.innerHTML =
          '<li class="insights-empty" style="cursor:default;border:none;background:transparent;box-shadow:none">No runs yet — generate your first snapshot.</li>';
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
          const fixable =
            r.metrics && r.metrics.fixable_loss != null
              ? ` · fixable ${Math.round(r.metrics.fixable_loss)}`
              : "";
          const active = activeRunId && activeRunId === r.run_id ? " is-active" : "";
          return (
            `<li class="${active.trim()}" data-run-id="${escapeHtml(r.run_id || "")}">` +
            `<div><div class="run-title">${escapeHtml(handle)}</div>` +
            `<div class="meta">${escapeHtml(source)} · ${escapeHtml(String(windowDays))}d ` +
            `${escapeHtml(tc)} · ${games} games${fixable}</div></div>` +
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
            setStatus(
              `Loaded run — ${data.games_analyzed || 0} games` +
                (data.games_capped ? " (capped)." : "."),
            );
          } catch (err) {
            setStatus(err.message);
          }
        });
      });
    } catch (err) {
      if (runList) {
        runList.innerHTML = `<li class="insights-empty">${escapeHtml(err.message)}</li>`;
      }
    }
  }

  async function loadMetrics() {
    if (!metricsSection || !metricsBody) return;
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
      // Insights API may not exist until Phase 2 — ignore.
      metricsSection.classList.add("hidden");
    }
  }

  function renderMetrics(metrics, gamesAnalyzed) {
    if (!metricsBody) return;
    updateHeroStats(metrics, gamesAnalyzed);
    const parts = [];
    if (metrics.trend && (metrics.trend.highlights || []).length) {
      const rows = metrics.trend.highlights
        .map((h) => `<div class="meta">${escapeHtml(h)}</div>`)
        .join("");
      const prevN = metrics.trend.previous_games_analyzed;
      const priorLabel =
        prevN != null ? `previous ${prevN}-game snapshot` : "previous snapshot";
      parts.push(
        metricCard(
          "Since last run",
          `<p>Compared to your ${escapeHtml(priorLabel)} for these filters.</p>${rows}`,
          { eyebrow: "Progress", trend: true },
        ),
      );
    }
    if (metrics.total_loss != null || metrics.fixable_loss != null) {
      const total = metrics.total_loss != null ? Math.round(metrics.total_loss) : "—";
      const fixable =
        metrics.fixable_loss != null ? Math.round(metrics.fixable_loss) : "N/A";
      const sample = metrics.fixable_sample_size ?? 0;
      parts.push(
        metricCard(
          "Fixable loss",
          `<div class="stat-row">` +
            `<div class="stat"><b>${total}</b><span>Total Δw</span></div>` +
            `<div class="stat"><b>${fixable}</b><span>Fixable</span></div>` +
            `</div>` +
            `<p>Win% points you dropped` +
            (sample ? ` · full-tier n=${sample}` : " · needs full review for findability") +
            `.</p>` +
            practiceBtnHtml("mistakes", "Practice this → Your Mistakes"),
          { eyebrow: "Tier 1" },
        ),
      );
    }
    if (metrics.loss_taxonomy) {
      const tax = metrics.loss_taxonomy;
      const colors = {
        converted_then_lost: "#e0443a",
        cliff: "#e8a53d",
        scramble: "#3db8c5",
        never_in_it: "#5d6877",
        bleed: "#44d62c",
      };
      const entries = Object.entries(tax.counts || tax);
      const total = entries.reduce((s, [, n]) => s + Number(n || 0), 0) || 1;
      const bars = entries
        .map(
          ([k, n]) =>
            `<span style="width:${(100 * Number(n)) / total}%;background:${colors[k] || "#99a0aa"}" title="${k}: ${n}"></span>`,
        )
        .join("");
      const legend = entries
        .map(
          ([k, n]) =>
            `<span><i style="background:${colors[k] || "#99a0aa"}"></i>${k.replaceAll("_", " ")} (${n})</span>`,
        )
        .join("");
      parts.push(
        metricCard(
          "Loss taxonomy",
          `<p>How your games slip — cliffs vs slow bleeds in one stacked view.</p>` +
            `<div class="insights-bars">${bars}</div>` +
            `<div class="insights-legend">${legend}</div>` +
            practiceBtnHtml("mistakes", "Practice this → Your Mistakes"),
          { eyebrow: "Tier 1", wide: true },
        ),
      );
    }
    if (metrics.time_vs_criticality) {
      const t = metrics.time_vs_criticality;
      parts.push(
        metricCard(
          "Time vs criticality",
          `<div class="stat-row">` +
            `<div class="stat"><b>${fmtSec(t.avg_time_high_vol)}</b><span>High vol</span></div>` +
            `<div class="stat"><b>${fmtSec(t.avg_time_low_vol)}</b><span>Quiet</span></div>` +
            `</div>` +
            `<p>` +
            (t.note ? escapeHtml(t.note) : "Average think time on sharp vs quiet positions.") +
            `</p>` +
            practiceBtnHtml("forced", "Practice this → Forced Lines"),
          { eyebrow: "Tier 1" },
        ),
      );
    }
    if (metrics.volatility_profile) {
      const rows = (metrics.volatility_profile.buckets || [])
        .map(
          (b) =>
            `<div class="meta">${escapeHtml(b.label)}: ` +
            `${Math.round((b.win_rate || 0) * 100)}% win` +
            ` (n=${b.n || 0})</div>`,
        )
        .join("");
      parts.push(
        metricCard(
          "Volatility profile",
          `${rows || "<p>No data yet.</p>"}` +
            practiceBtnHtml("defense", "Practice this → Defense Gym"),
          { eyebrow: "Tier 1" },
        ),
      );
    }

    const t2 = metrics.tier2 || {};
    if (t2.phase_attribution && t2.phase_attribution.length) {
      const rows = t2.phase_attribution
        .map(
          (p) =>
            `<div class="meta">${escapeHtml(p.phase)}: ` +
            `${(p.delta_w_per_move || 0).toFixed(1)} Δw/move ` +
            `(${Math.round(p.total_delta_w || 0)} total, n=${p.moves || 0})</div>`,
        )
        .join("");
      parts.push(
        metricCard(
          "Phase attribution",
          `<p>Eval swing per move by phase — not confounded by how you entered.</p>${rows}`,
          { eyebrow: "Tier 2" },
        ),
      );
    }
    if (t2.repertoire_depth && t2.repertoire_depth.n) {
      const r = t2.repertoire_depth;
      parts.push(
        metricCard(
          "Repertoire depth",
          `<div class="stat-row">` +
            `<div class="stat"><b>${r.mean_leave_book_ply != null ? Math.round(r.mean_leave_book_ply) : "—"}</b><span>Leave book</span></div>` +
            `<div class="stat"><b>${r.mean_delta_w_next_5 != null ? r.mean_delta_w_next_5.toFixed(1) : "—"}</b><span>Next-5 Δw</span></div>` +
            `</div>` +
            `<p>Separates bad opening choice from fine openings with weak follow-ups.</p>`,
          { eyebrow: "Tier 2" },
        ),
      );
    }
    if (t2.conversion || t2.comeback) {
      const c = t2.conversion || {};
      const b = t2.comeback || {};
      parts.push(
        metricCard(
          "Conversion vs comeback",
          `<div class="stat-row">` +
            `<div class="stat"><b>${pct(c.win_rate)}</b><span>Convert (&gt;70%)</span></div>` +
            `<div class="stat"><b>${pct(b.win_rate)}</b><span>Comeback (&lt;30%)</span></div>` +
            `</div>` +
            `<p>n=${c.n || 0} converting · n=${b.n || 0} scrambling back.</p>`,
          { eyebrow: "Tier 2" },
        ),
      );
    }
    if (t2.castling) {
      const rows = Object.entries(t2.castling)
        .filter(([, v]) => (v.n || 0) > 0)
        .map(
          ([k, v]) =>
            `<div class="meta">${escapeHtml(k.replaceAll("_", " "))}: ${pct(v.win_rate)} (n=${v.n})</div>`,
        )
        .join("");
      if (rows) {
        parts.push(metricCard("Castling", rows, { eyebrow: "Tier 2" }));
      }
    }
    if (t2.opponent_relative && t2.opponent_relative.length) {
      const rows = t2.opponent_relative
        .map((b) => {
          const phases = (b.phase_attribution || [])
            .map((p) => `${p.phase} ${(p.delta_w_per_move || 0).toFixed(1)}`)
            .join(", ");
          return (
            `<div class="meta"><strong>${escapeHtml(b.band)}</strong>: ${pct(b.win_rate)} ` +
            `(n=${b.n}) · Δw/move ${escapeHtml(phases)}</div>`
          );
        })
        .join("");
      parts.push(
        metricCard(
          "Opponent-relative",
          `<p>Win rate and phase loss vs lower / similar / higher-rated opponents.</p>${rows}`,
          { eyebrow: "Tier 2", wide: true },
        ),
      );
    }
    if (t2.missed_wins) {
      const mw = t2.missed_wins;
      const examples = (mw.examples || [])
        .slice(0, 5)
        .map(
          (ex) =>
            `<div class="meta">vs ${escapeHtml(ex.opponent || "?")}: peak ${(ex.peak_win_prob * 100).toFixed(0)}% ` +
            `→ slipped ${escapeHtml(ex.slip_san || "?")} (ply ${ex.slip_ply || "—"})</div>`,
        )
        .join("");
      parts.push(
        metricCard(
          "Missed wins",
          `<div class="stat-row"><div class="stat"><b>${mw.count || 0}</b><span>Slipped</span></div></div>` +
            `<p>Games that reached &gt;85% win chance and weren’t won.</p>${examples}`,
          { eyebrow: "Tier 2" },
        ),
      );
    }
    if (t2.standard) {
      const s = t2.standard;
      const color = Object.entries(s.by_color || {})
        .map(([k, v]) => `${k} ${pct(v.win_rate)} (n=${v.n})`)
        .join(" · ");
      const eco = (s.by_eco || [])
        .slice(0, 6)
        .map((e) => `<div class="meta">${escapeHtml(e.eco)}: ${pct(e.win_rate)} (n=${e.n})</div>`)
        .join("");
      const lengths = (s.game_length || [])
        .map((g) => `${escapeHtml(g.label)} ${pct(g.win_rate)} (n=${g.n})`)
        .join(" · ");
      const hours = (s.by_hour || [])
        .slice(0, 8)
        .map((h) => `${String(h.hour).padStart(2, "0")}:00 ${pct(h.win_rate)}`)
        .join(" · ");
      parts.push(
        metricCard(
          "Standard set",
          `<p>${escapeHtml(color || "No color split yet.")}</p>` +
            (eco ? `<p class="meta">By ECO</p>${eco}` : "") +
            (lengths ? `<p class="meta">Length: ${escapeHtml(lengths)}</p>` : "") +
            (hours ? `<p class="meta">Time of day: ${escapeHtml(hours)}</p>` : ""),
          { eyebrow: "Tier 2", wide: true },
        ),
      );
    }

    if (metrics.volatility_steering) {
      const s = metrics.volatility_steering;
      parts.push(
        metricCard(
          "Volatility steering",
          `<div class="stat-row">` +
            `<div class="stat"><b>${fmtNum(s.mean_vol_played_best)}</b><span>Played best</span></div>` +
            `<div class="stat"><b>${fmtNum(s.mean_vol_played_alt)}</b><span>Deviated</span></div>` +
            `</div>` +
            `<p>` +
            (s.note ? escapeHtml(s.note) : "Sharpness of positions you chose vs engine alternatives.") +
            `</p>`,
          { eyebrow: "Advanced" },
        ),
      );
    }

    const t3 = metrics.tier3 || {};
    if (t3.sessions || (t3.by_session_index && t3.by_session_index.length)) {
      const idx = (t3.by_session_index || [])
        .slice(0, 6)
        .map(
          (g) =>
            `<div class="meta">Game #${g.game_index}: ${pct(g.win_rate)}` +
            (g.mean_accuracy != null ? ` · acc ${Math.round(g.mean_accuracy)}` : "") +
            ` (n=${g.n})</div>`,
        )
        .join("");
      const after = t3.after_loss || {};
      const lens = Object.values(t3.by_session_length || {})
        .filter((v) => v.n)
        .map((v) => `${escapeHtml(v.label)} ${pct(v.win_rate)} (n=${v.n})`)
        .join(" · ");
      parts.push(
        metricCard(
          "Tilt & sessions",
          `<p><strong>${t3.sessions || 0}</strong> sessions` +
            (t3.note ? ` — ${escapeHtml(t3.note)}` : ".") +
            `</p>${idx}` +
            `<p class="meta">After a loss: ${pct(after.win_rate)} (n=${after.n || 0})` +
            (after.mean_accuracy != null ? ` · acc ${Math.round(after.mean_accuracy)}` : "") +
            `</p>` +
            (lens ? `<p class="meta">By session length: ${lens}</p>` : ""),
          { eyebrow: "Tier 3", wide: true },
        ),
      );
    }

    if (metrics.missed_tactics && (metrics.missed_tactics.tags || []).length) {
      const mt = metrics.missed_tactics;
      const rows = mt.tags
        .slice(0, 8)
        .map(
          (t) =>
            `<div class="meta"><strong>${escapeHtml(String(t.tag || "").replaceAll("_", " "))}</strong>: ` +
            `n=${t.n || 0} · mean Δw ${(t.mean_delta_w || 0).toFixed(1)}` +
            (t.high_findability_n
              ? ` · ${t.high_findability_n} with findability &gt; 60`
              : "") +
            `</div>`,
        )
        .join("");
      const sample = mt.full_tier_sample_size ?? 0;
      parts.push(
        metricCard(
          "Missed tactics",
          `<p>${escapeHtml(mt.note || "Heuristic tags on costly misses, crossed with findability.")}` +
            (sample
              ? ` <span class="meta">(full-tier n=${sample})</span>`
              : ' <span class="meta">(needs full review for findability cross)</span>') +
            `</p>${rows}` +
            practiceBtnHtml("mistakes", "Practice this → Your Mistakes"),
          { eyebrow: "Tier 1", wide: true },
        ),
      );
    }

    const pf = metrics.practice_flags || {};
    if (pf.count) {
      const items = (pf.items || [])
        .slice(0, 8)
        .map(
          (it) =>
            `<div class="meta">${escapeHtml(it.san || it.move_uci || "?")} ` +
            `Δw ${Math.round(it.delta_w || 0)}` +
            (it.findability != null ? ` · find ${it.findability}` : " · needs full") +
            (it.opponent ? ` · vs ${escapeHtml(it.opponent)}` : "") +
            `</div>`,
        )
        .join("");
      parts.push(
        metricCard(
          "Practice from your games",
          `<div class="stat-row"><div class="stat"><b>${pf.count}</b><span>Flagged</span></div></div>` +
            `<p>` +
            (pf.full_tier_sample
              ? `${pf.full_tier_sample} had findability scores`
              : "Shallow run — full review upgrades findability") +
            `. Threshold Δw ≥ ${pf.delta_w_threshold || 15}` +
            (pf.findability_min ? `, findability &gt; ${pf.findability_min} when known` : "") +
            `.</p>${items}` +
            practiceBtnHtml("mistakes", "Practice this → Your Mistakes"),
          { eyebrow: "Action", wide: true },
        ),
      );
    }

    metricsBody.innerHTML =
      parts.join("") ||
      '<p class="insights-empty">No metrics yet — generate a run to illuminate your patterns.</p>';
    metricsBody.querySelectorAll(".insights-practice-link").forEach((btn) => {
      btn.addEventListener("click", () => goPractice(btn.getAttribute("data-practice") || "mistakes"));
    });
    if (practiceBtn && pf.count) practiceBtn.disabled = false;
  }

  function fmtNum(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    return Number(v).toFixed(0);
  }

  function pct(rate) {
    if (rate == null || Number.isNaN(Number(rate))) return "—";
    return `${Math.round(Number(rate) * 100)}%`;
  }

  function fmtSec(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    return `${Math.round(Number(v))}s`;
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

    // Prefer Phase 2 Insights API; fall back to Mistakes generate SSE (chess.com only).
    try {
      const started = await tryStartInsightsRun(username, windowDays, timeClass, source);
      if (started) {
        await pollInsightsRun(started);
        await loadRuns();
        await loadMetrics();
        await refreshPracticeButton();
        return;
      }
    } catch (err) {
      // Fall through to Mistakes if Insights endpoint missing or failed early.
      if (!String(err.message || "").includes("404")) {
        setStatus(err.message);
        setBusy(false);
        return;
      }
    }

    if (source === "lichess") {
      setStatus("Lichess ingest requires the Insights API.");
      setBusy(false);
      return;
    }

    try {
      const resp = await api("/api/mistakes/generate", {
        method: "POST",
        body: JSON.stringify({
          chesscom_username: username,
          since_days: windowDays,
          time_class: timeClass,
          max_games: 300,
        }),
      });
      await consumeEventStream(resp.body, (event, data) => {
        if (event === "start") {
          setStatus(`Scanning ${username}'s ${timeClass} games (last ${windowDays} days)…`);
        } else if (event === "progress") {
          if (data.games_capped && capNote) capNote.classList.remove("hidden");
          setStatus(
            `Scanned ${data.games_scanned || 0} games · ${data.puzzles_created || 0} puzzles found…`,
          );
        } else if (event === "done") {
          if (data.games_capped && capNote) capNote.classList.remove("hidden");
          lastUnsolved = data.total_unsolved || 0;
          const created = data.puzzles_created || 0;
          setStatus(
            created > 0
              ? `Done — ${created} new puzzles (${lastUnsolved} unsolved total).`
              : `Done — no new puzzles (${lastUnsolved} unsolved waiting).`,
          );
          if (practiceBtn) practiceBtn.disabled = lastUnsolved <= 0;
        } else if (event === "error") {
          setStatus(data.message || "Generation failed.");
        }
      });
      await loadRuns();
      await refreshPracticeButton();
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
    if (resp.status === 404) return null;
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
    let lastProgress = -1;
    let stallTicks = 0;
    // Keep polling while progress advances; only give up after a long stall.
    for (let i = 0; i < 7200; i++) {
      const resp = await api(`/api/insights/${runId}`);
      const data = await resp.json();
      if (data.games_capped && capNote) capNote.classList.remove("hidden");
      const progress = Math.round((data.progress || 0) * 100);
      setProgress(Math.max(8, progress));
      setStatus(
        `${data.status || "running"} — ${data.games_analyzed || 0} games (${progress}%)`,
      );
      if (progress > lastProgress || (data.games_analyzed || 0) > 0) {
        if (progress !== lastProgress) stallTicks = 0;
        lastProgress = progress;
      } else {
        stallTicks += 1;
      }
      if (data.status === "complete" || data.status === "done") {
        setProgress(100);
        if (data.metrics) renderMetrics(data.metrics, data.games_analyzed);
        if (metricsSection) metricsSection.classList.remove("hidden");
        setStatus(
          `Done — ${data.games_analyzed || 0} games analyzed` +
            (data.games_capped ? " (capped at 300)." : "."),
        );
        setBusy(false);
        return;
      }
      if (data.status === "error") {
        throw new Error(data.detail || data.message || "Insights run failed");
      }
      // 5 minutes with zero progress change → likely stuck
      if (stallTicks >= 300 && progress === lastProgress) {
        setBusy(false);
        setStatus(
          `Still running in the background (${progress}% · ${data.games_analyzed || 0} games). ` +
            `Click the run under Prior runs when it finishes.`,
        );
        await loadRuns();
        return;
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    setBusy(false);
    setStatus(
      "Analysis is still running on the server. Refresh Prior runs in a few minutes.",
    );
    await loadRuns();
  }

  async function refreshPracticeButton() {
    try {
      const resp = await api("/api/mistakes/run");
      const data = await resp.json();
      lastUnsolved = data.unsolved_puzzles || 0;
      if (practiceBtn) practiceBtn.disabled = lastUnsolved <= 0;
    } catch {
      /* ignore */
    }
  }

  async function prefillUsername() {
    try {
      const resp = await api("/api/mistakes/username");
      const data = await resp.json();
      if (data.chesscom_username && usernameEl && !usernameEl.value) {
        usernameEl.value = data.chesscom_username;
      }
    } catch {
      /* ignore */
    }
  }

  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      void generate({ refresh: false });
    });
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => void generate({ refresh: true }));
  }
  if (practiceBtn) {
    practiceBtn.addEventListener("click", () => {
      if (window.__shellNavigate) window.__shellNavigate("/training/mistakes");
      else if (window.__shellSwitchTab) window.__shellSwitchTab("mistakes");
    });
  }

  window.__insightsSetActive = (active) => {
    if (!active) return;
    void prefillUsername();
    void loadRuns();
    void loadMetrics();
    void refreshPracticeButton();
  };
})();
