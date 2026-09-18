/* Insights study plan — the overlay that turns the report into a schedule.
 *
 * The server owns the plan (server/study_plan.py); this file renders it and
 * wires the hand-offs. It never decides what to study, how long for, or what a
 * block is worth — if a number looks wrong, it is wrong in Python.
 *
 * Two things it does own:
 *
 * - **The budget controls.** Hours-per-week and weeks re-fetch the plan from
 *   `/api/insights/:id/study-plan`, which is pure arithmetic over the stored
 *   metrics. No re-analysis, so the controls can be live.
 * - **The empty state.** A run too thin for a plan says so, in the server's
 *   words, rather than rendering an empty page.
 */
(function () {
  "use strict";

  const overlay = document.getElementById("studyplan");
  if (!overlay) return;

  const HOURS = [2, 5, 8, 12];
  const WEEKS = [2, 4, 8, 12];
  const STORE = "chessmax.studyplan.budget";

  const KIND_LABEL = { review: "Game", trainer: "Drill", study: "Read", rule: "Rule" };

  let metrics = null;
  let meta = null;
  let plan = null;
  let budget = { hours_per_week: 5, weeks: 4 };
  let pending = 0;

  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || "null");
    if (saved && saved.hours_per_week && saved.weeks) budget = saved;
  } catch (_) { /* a private window has no storage; the default is fine. */ }

  // ── Helpers ─────────────────────────────────────────────────────────────

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  // Hours are allocated in quarters, so go through minutes rather than
  // formatting the fraction — the direct version rendered 0.75 as "0h 45m".
  function hrs(v) {
    return isNum(v) && v > 0 ? mins(Math.round(v * 60)) : "—";
  }

  function mins(v) {
    if (!isNum(v)) return "";
    return v >= 60 && v % 60 === 0 ? `${v / 60}h` : v >= 60
      ? `${Math.floor(v / 60)}h ${v % 60}m` : `${v}m`;
  }

  function runId() {
    return (meta && (meta.run_id || meta.runId))
      || (window.__insightsActiveRunId && window.__insightsActiveRunId())
      || "";
  }

  // ── Routing ─────────────────────────────────────────────────────────────

  function planPath() {
    const id = runId();
    return id ? `/insights/${id}/study-plan` : "/insights";
  }

  function parsePath(pathname) {
    const path = String(pathname || "").replace(/\/+$/, "") || "/";
    const m = path.match(/^\/insights\/([^/]+)\/study-plan$/);
    return m ? { runId: m[1] } : null;
  }

  function pinToHeader() {
    const header = document.querySelector(".shell-header");
    const top = header ? Math.round(header.getBoundingClientRect().height) : 0;
    overlay.style.top = `${top}px`;
    document.documentElement.style.setProperty("--shell-header-h", `${top}px`);
  }

  // ── Open / close ────────────────────────────────────────────────────────

  function open({ push = true } = {}) {
    if (!metrics) return;
    // The other two full-screen surfaces own the scroll lock; close them first
    // or two overlays fight over document.body.style.overflow.
    if (window.__postmortemClose) window.__postmortemClose({ silent: true });
    if (window.__insightsCloseDeepDive) window.__insightsCloseDeepDive();
    pinToHeader();
    overlay.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    render();
    const scroll = overlay.querySelector(".sp-scroll");
    if (scroll) scroll.scrollTop = 0;
    const path = planPath();
    if (push && window.location.pathname !== path) {
      if (window.__shellNavigate) window.__shellNavigate(path);
      else history.pushState({ chessmax: true, path }, "", path);
    }
    // Fetch the plan for the stored budget if it is not the default one.
    if (budget.hours_per_week !== 5 || budget.weeks !== 4) refetch();
  }

  function close({ silent } = {}) {
    overlay.classList.add("hidden");
    const dash = document.getElementById("insights-dashboard");
    if (!dash || dash.classList.contains("hidden")) document.body.style.overflow = "";
    if (!silent && window.location.pathname.includes("/study-plan") && window.__shellNavigate) {
      window.__shellNavigate("/insights");
    }
  }

  function adopt(nextMetrics, nextMeta) {
    metrics = nextMetrics || null;
    meta = nextMeta || {};
    plan = (metrics && metrics.study_plan) || null;
    if (!overlay.classList.contains("hidden")) render();
  }

  // ── Budget ──────────────────────────────────────────────────────────────

  /** Re-ask the server for the same plan at a different budget. */
  async function refetch() {
    const id = runId();
    if (!id) return;
    const token = ++pending;
    try {
      const resp = await fetch(
        `/api/insights/${encodeURIComponent(id)}/study-plan`
        + `?hours_per_week=${budget.hours_per_week}&weeks=${budget.weeks}`,
        { credentials: "same-origin" },
      );
      if (!resp.ok) return;
      const body = await resp.json();
      // Newest wins: a slow answer must not overwrite a newer selection.
      if (token !== pending || !body.study_plan) return;
      plan = body.study_plan;
      render();
    } catch (_) { /* keep the plan already on screen */ }
  }

  function setBudget(patch) {
    budget = { ...budget, ...patch };
    try { localStorage.setItem(STORE, JSON.stringify(budget)); } catch (_) { /* no-op */ }
    render();   // paint the new selection immediately
    refetch();  // then swap in the real numbers
  }

  // ── Render ──────────────────────────────────────────────────────────────

  function render() {
    overlay.innerHTML = chromeHtml() + `<div class="sp-scroll">${bodyHtml()}</div>`;
    bind();
  }

  function chromeHtml() {
    const handle = (meta && (meta.handle || meta.chesscom_handle)) || "Your games";
    const bits = [handle, meta && meta.time_class, meta && meta.window_days
      ? `${meta.window_days}d` : ""].filter(Boolean).join(" · ");
    const seg = (name, values, current, suffix) => values.map((v) =>
      `<button type="button" data-${name}="${v}" class="${v === current ? "is-on" : ""}">`
      + `${v}${suffix}</button>`).join("");
    return `
      <header class="sp-top">
        <button type="button" class="sp-back" data-sp="close" aria-label="Back">
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <path d="M19 12H6M12 5l-7 7 7 7" fill="none" stroke="currentColor"
                  stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
        <div class="sp-title"><b>Your study plan</b><span>${escapeHtml(bits)}</span></div>
        <div class="sp-controls">
          <div class="sp-ctl">
            <label>Per week</label>
            <div class="sp-seg">${seg("hours", HOURS, budget.hours_per_week, "h")}</div>
          </div>
          <div class="sp-ctl">
            <label>For</label>
            <div class="sp-seg">${seg("weeks", WEEKS, budget.weeks, "w")}</div>
          </div>
          <button type="button" class="sp-print" data-sp="print">Print</button>
        </div>
      </header>`;
  }

  function bodyHtml() {
    if (!plan) {
      return `<div class="sp-empty"><h2>No plan yet</h2>
        <p>Generate a report first — the plan is built from the games in it.</p></div>`;
    }
    if (!plan.available) {
      return `<div class="sp-empty"><h2>Not enough to plan from</h2>
        <p>${escapeHtml(plan.reason || "This run is too small to say anything specific.")}</p></div>`;
    }
    const study = plan.blocks || [];
    const habits = plan.habits || [];
    return `<div class="sp-wrap">
      ${heroHtml()}
      <section>
        <div class="sp-head"><h2>What to work on</h2>
          <span>${study.length} block${study.length === 1 ? "" : "s"} ·
          ${hrs(plan.hours_per_week)} a week · ranked by what it is costing you</span></div>
        <div class="sp-blocks">${study.map((b) => blockHtml(b)).join("")}</div>
      </section>
      ${habits.length ? `<section>
        <div class="sp-head"><h2>Rules to play by</h2>
          <span>No study time — these are habits, applied while you play</span></div>
        <div class="sp-blocks">${habits.map((b) => blockHtml(b)).join("")}</div>
      </section>` : ""}
      <div class="sp-cols">${scheduleHtml()}${arcHtml()}</div>
    </div>`;
  }

  function heroHtml() {
    const p = plan.projection || {};
    const gain = isNum(p.rating_gain) ? p.rating_gain : null;
    const from = isNum(p.current_rating) ? Math.round(p.current_rating) : null;
    const to = isNum(p.projected_rating) ? p.projected_rating : null;
    const conf = { high: "Strong sample", medium: "Fair sample", low: "Small sample" }[p.confidence];

    const arrow = gain === null || from === null ? "" : `
      <div class="sp-arrow">
        <div class="sp-rating"><b>${from}</b><span>Now</span></div>
        <div class="sp-gain">
          <b>+${gain}</b>
          <svg viewBox="0 0 40 12" width="40" height="12" aria-hidden="true">
            <path d="M0 6h32M27 1l6 5-6 5" fill="none" stroke="currentColor"
                  stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
        <div class="sp-rating is-target"><b>${to}</b><span>Target</span></div>
      </div>`;

    const chips = [
      isNum(plan.total_hours) ? `<span class="sp-chip"><b>${hrs(plan.total_hours)}</b> total</span>` : "",
      `<span class="sp-chip"><b>${plan.weeks}</b> weeks</span>`,
      isNum(plan.games) ? `<span class="sp-chip">from <b>${plan.games}</b> games</span>` : "",
      conf ? `<span class="sp-chip">${escapeHtml(conf)}</span>` : "",
      isNum(p.ceiling) && p.limited_by === "pace"
        ? `<span class="sp-chip"><b>${p.ceiling}</b> on the table long-term</span>` : "",
    ].filter(Boolean).join("");

    const headline = gain === null
      ? "Here is what to work on"
      : `Worth about <b style="color:var(--sp-green-hi)">${gain} rating points</b>`;

    return `<section class="sp-hero">
      <div>
        <p class="eyebrow">${plan.weeks}-week plan · ${hrs(plan.hours_per_week)} a week</p>
        <h1>${headline}</h1>
        <p>Every block below is built from your own games — the openings you lose in,
           the patterns you miss, the moves where the clock beats you. Nothing here is
           generic advice.</p>
        <div class="sp-meta">${chips}</div>
      </div>
      ${arrow}
      ${p.note ? `<p class="sp-note">${escapeHtml(p.note)} ${escapeHtml(plan.model || "")}</p>` : ""}
    </section>`;
  }

  function blockHtml(b) {
    const isHabit = b.kind !== "study";
    const tags = [
      isNum(b.rating_gain) && b.rating_gain > 0
        ? `<span class="sp-tag is-gain">+${b.rating_gain}</span>` : "",
      isHabit
        ? `<span class="sp-tag">No study time</span>`
        : `<span class="sp-tag is-time">${hrs(b.hours_per_week)}/wk</span>`,
    ].filter(Boolean).join("");

    return `<details class="sp-block" data-kind="${escapeHtml(b.kind)}" ${b.priority <= 2 ? "open" : ""}>
      <summary>
        <span class="sp-rank">${isHabit ? "!" : b.priority}</span>
        <span class="sp-sum">
          <span>${escapeHtml(b.chapter)}</span>
          <b>${escapeHtml(b.title)}</b>
        </span>
        <span class="sp-tags">${tags}</span>
      </summary>
      <div class="sp-body">
        <p class="sp-why">${escapeHtml(b.why)}</p>
        ${evidenceHtml(b)}
        <div class="sp-drills">${(b.drills || []).map(drillHtml).join("")}</div>
        ${b.measure ? `<p class="sp-measure"><b>How you will know it worked:</b>
          ${escapeHtml(b.measure)}</p>` : ""}
      </div>
    </details>`;
  }

  const pctOf = (v) => (isNum(v) ? `${Math.round(v * 100)}%` : "—");

  function ev(label, value, tone) {
    if (value == null || value === "" || value === "—") return "";
    return `<span class="sp-ev ${tone ? `is-${tone}` : ""}">`
      + `<i>${escapeHtml(label)}</i><b>${escapeHtml(value)}</b></span>`;
  }

  /**
   * Each block's evidence has its own shape, so this is a switch rather than a
   * generic walker: the point of these chips is that the number is labelled
   * with what it actually means.
   */
  function evidenceHtml(b) {
    const e = b.evidence || {};
    let out = "";
    if (b.id === "openings") {
      out = (e.lines || []).map((l) => ev(
        `${l.opening} · ${l.color}`,
        `${pctOf(l.score_pct)} over ${l.games}`,
        l.score_pct < 0.4 ? "bad" : null,
      )).join("") + ev("Out of book by move", e.leave_book_move);
    } else if (b.id === "middlegame") {
      out = (e.structures || []).map((s) => ev(
        s.label, `${pctOf(s.score_pct)} over ${s.games}`,
        s.score_pct < 0.4 ? "bad" : s.score_pct > 0.6 ? "good" : null,
      )).join("");
      if (e.drift) out += ev(`${e.drift.opening} · middlegame`, `${pctOf(e.drift.score_pct)} scored`, "bad");
    } else if (b.id === "tactics") {
      out = (e.motifs || []).map((m) => ev(m.label, `missed ${m.n}×`, "bad")).join("")
        + ev("Positions ready to drill", e.practice_positions);
    } else if (b.id === "mistakes") {
      out = ev("Positions", e.positions)
        + ev("One every", isNum(e.blunders_per_100) && e.blunders_per_100 > 0
          ? `${Math.round(100 / e.blunders_per_100)} moves` : null, "bad");
    } else if (b.id === "endgames") {
      out = (e.by_type || []).map((t) => ev(
        t.label, `${pctOf(t.score_pct)} over ${t.games}`,
        t.score_pct < 0.4 ? "bad" : null,
      )).join("")
        + (e.entry || []).filter((r) => r.n).map((r) => ev(r.label, pctOf(r.score_pct))).join("")
        + ev("Reached in", pctOf(e.reach_rate));
    } else if (b.id === "calculation") {
      out = ev("Accuracy drop when sharp", isNum(e.gap) ? `${Math.round(e.gap)} pts` : null, "bad")
        + ev("Handled cleanly", pctOf(e.handled_rate))
        + ev("Moves like that", e.critical_moves);
    } else if (b.id === "blunders") {
      out = ev("Per 100 moves", isNum(e.per_100) ? e.per_100.toFixed(1) : null, "bad")
        + ev("Worst stretch", e.worst_window ? `moves ${e.worst_window}` : null)
        + ev("Clean games", pctOf(e.clean_game_rate));
    } else if (b.id === "clock") {
      out = ev("On sharp moves", isNum(e.time_high) ? `${Math.round(e.time_high)}s` : null, e.inverted ? "bad" : null)
        + ev("On quiet moves", isNum(e.time_low) ? `${Math.round(e.time_low)}s` : null)
        + (e.scramble ? ev("Under 10s", `${e.scramble.moves} moves`) : "");
    } else if (b.id === "mental") {
      const a = e.after_loss || {};
      const lengths = e.by_session_length || {};
      out = ev("After a loss", isNum(a.win_rate) ? `${pctOf(a.win_rate)} over ${a.n}` : null, "bad")
        + Object.values(lengths).filter((l) => l && l.n).map((l) =>
          ev(l.label, pctOf(l.win_rate))).join("");
    }
    return out ? `<div class="sp-evidence">${out}</div>` : "";
  }

  function drillHtml(d) {
    const tag = KIND_LABEL[d.kind] || "Do";
    const clickable = d.kind === "review" ? !!d.game_id : d.kind === "trainer" ? !!d.route : false;
    const el = clickable ? "button" : "div";
    const attrs = clickable
      ? ` type="button" data-drill="${escapeHtml(d.kind)}"`
        + (d.game_id ? ` data-game="${escapeHtml(d.game_id)}"` : "")
        + (isNum(d.ply) ? ` data-ply="${d.ply}"` : "")
        + (d.route ? ` data-route="${escapeHtml(d.route)}"` : "")
      : "";
    return `<${el} class="sp-drill" data-kind="${escapeHtml(d.kind)}"${attrs}>
      <span class="sp-kind">${escapeHtml(tag)}</span>
      <span class="sp-drill-txt">
        <b>${escapeHtml(d.label)}</b>
        <span>${escapeHtml(d.detail)}</span>
      </span>
      <span class="sp-mins">${d.minutes ? mins(d.minutes) : ""}</span>
    </${el}>`;
  }

  function titleFor(blockId) {
    const all = (plan.blocks || []).concat(plan.habits || []);
    const found = all.find((b) => b.id === blockId);
    return found ? found.title : blockId;
  }

  function scheduleHtml() {
    const sessions = plan.schedule || [];
    if (!sessions.length) return "";
    const longest = Math.max(...sessions.map((s) => s.minutes || 0), 1);
    return `<section class="sp-card">
      <div class="sp-head"><h2>A week of it</h2>
        <span>${sessions.length} sessions · ${hrs(plan.hours_per_week)}</span></div>
      <div class="sp-sessions">${sessions.map((s) => `
        <div class="sp-session">
          <div class="sp-session-head"><b>${escapeHtml(s.label)}</b><span>${mins(s.minutes)}</span></div>
          <div class="sp-bar">${(s.items || []).map((i) =>
            `<i style="flex:${i.minutes}"></i>`).join("")}
            <i style="flex:${Math.max(0, longest - s.minutes)};background:transparent"></i>
          </div>
          <ul>${(s.items || []).map((i) =>
            `<li>${escapeHtml(titleFor(i.block))}<span>${mins(i.minutes)}</span></li>`).join("")}</ul>
        </div>`).join("")}</div>
    </section>`;
  }

  function arcHtml() {
    const weeks = plan.arc || [];
    if (!weeks.length) return "";
    return `<section class="sp-card">
      <div class="sp-head"><h2>The ${plan.weeks} weeks</h2>
        <span>What leads, and what you check at the end of it</span></div>
      <div class="sp-weeks">${weeks.map((w) => `
        <div class="sp-week">
          <span class="sp-week-no">W${w.week}</span>
          <span class="sp-week-txt">
            <b>${escapeHtml(w.focus)}</b>
            <span>${escapeHtml(w.checkpoint)}</span>
          </span>
        </div>`).join("")}</div>
    </section>`;
  }

  // ── Wiring ──────────────────────────────────────────────────────────────

  function bind() {
    overlay.querySelectorAll("[data-sp]").forEach((btn) => {
      const what = btn.dataset.sp;
      btn.onclick = () => (what === "close" ? close() : window.print());
    });
    overlay.querySelectorAll("[data-hours]").forEach((btn) => {
      btn.onclick = () => setBudget({ hours_per_week: Number(btn.dataset.hours) });
    });
    overlay.querySelectorAll("[data-weeks]").forEach((btn) => {
      btn.onclick = () => setBudget({ weeks: Number(btn.dataset.weeks) });
    });
    overlay.querySelectorAll("button[data-drill]").forEach((btn) => {
      btn.onclick = () => {
        const { drill, game, ply, route } = btn.dataset;
        close({ silent: true });
        if (drill === "review" && game && window.__insightsGoReview) {
          window.__insightsGoReview(game, ply ? Number(ply) : undefined);
        } else if (route && window.__shellNavigate) {
          window.__shellNavigate(route);
        }
      };
    });
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !overlay.classList.contains("hidden")) close();
  });

  // ── Public surface ──────────────────────────────────────────────────────

  window.__studyPlanOpen = open;
  window.__studyPlanClose = close;
  window.__studyPlanAdopt = adopt;
  window.__studyPlanRoute = parsePath;
})();
