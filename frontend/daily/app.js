// Daily check-in — five timed phases, a monthly calendar, and the repertoire
// drill that is phase one.
//
// The tab is a **conductor**. Four of its five phases hand off to modes that
// already exist (`/puzzles`, `/training/endgame`, `/training/mistakes`,
// `/game-review`), so the only thing that has to survive leaving this tab is
// the clock — which is why the HUD is appended to <body> rather than living
// inside `#daily-root`, and why the tick loop runs whether or not this app is
// the active one.
//
// Three things are load-bearing and easy to break:
//
//   * **The clock only counts a visible tab.** Ticking on a hidden tab turns
//     "left it open overnight" into eight hours of practice; the server caps
//     each heartbeat for the same reason.
//   * **Time is flushed, not inferred.** The client owns the second-by-second
//     count and posts deltas; the server never guesses from timestamps, so a
//     closed laptop costs the seconds it was closed for and nothing more.
//   * **The review phase's game is picked once** and stored on the phase row.
//     Re-entering the phase must reopen the same game, or "one loss, reviewed"
//     becomes a slot machine.
/* eslint-disable no-undef */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const api = async (path, opts) => {
    const res = await fetch(
      path,
      Object.assign(
        { headers: { "Content-Type": "application/json" }, credentials: "same-origin" },
        opts || {},
      ),
    );
    if (!res.ok) {
      let detail = `${res.status}`;
      try {
        const body = await res.json();
        detail = detailText(body && body.detail) || detail;
      } catch (_) {
        /* non-JSON error body */
      }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  };

  // FastAPI reports 422 as a list of error objects and everything else as a
  // string; a bare `detail` renders "[object Object]" on the path users hit.
  function detailText(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((d) => (d && d.msg ? d.msg : "")).filter(Boolean).join(" · ");
    }
    return "";
  }

  function setText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
    );
  }

  // ── Time ──────────────────────────────────────────────────────────────── //

  const FLUSH_MS = 15000;
  const RING_CIRCUMFERENCE = 2 * Math.PI * 52;
  const MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];
  const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  // The player's day, not the server's — only the browser knows the timezone.
  function localDay(date) {
    const d = date || new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function fmtClock(seconds) {
    const s = Math.max(0, Math.round(seconds || 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  function fmtMinutes(seconds) {
    const m = Math.round((seconds || 0) / 60);
    return m < 1 ? "under a minute" : `${m} min`;
  }

  function parseDay(day) {
    const [y, m, d] = String(day).split("-").map(Number);
    return new Date(y, (m || 1) - 1, d || 1);
  }

  // ── State ─────────────────────────────────────────────────────────────── //

  const state = {
    booted: false,
    active: false,
    path: "/daily",
    view: "home",
    day: localDay(),
    session: null,
    month: null,
    calendar: null,
    selectedDay: null,
  };

  // One clock, one phase. `pending` is what this client has counted and not yet
  // posted; `base` is what the server last confirmed.
  const clock = {
    phase: null,
    base: 0,
    target: 300,
    pending: 0,
    running: false,
    lastFlush: 0,
  };

  const drill = {
    loaded: false,
    cards: [],
    index: 0,
    current: null,
    answered: false,
    revealed: false,
    cg: null,
    repertoire: null,
  };

  function phaseByKey(key) {
    const phases = (state.session && state.session.phases) || [];
    return phases.find((p) => p.key === key) || null;
  }

  // ── Session ───────────────────────────────────────────────────────────── //

  async function loadToday() {
    state.day = localDay();
    const data = await api(`/api/daily/today?day=${state.day}`);
    applySession(data);
    return data;
  }

  function applySession(data) {
    if (!data) return;
    state.session = data;
    const active = data.active_phase ? phaseByKey(data.active_phase) : null;
    if (active) {
      // Adopt the server's count, but for the phase already on this clock never
      // go below what has been shown — a flush in flight must not make the
      // number jump backwards.
      const resuming = clock.phase === active.key;
      clock.phase = active.key;
      clock.base = resuming
        ? Math.max(clock.base, active.seconds_spent || 0)
        : active.seconds_spent || 0;
      clock.target = active.target_seconds || 300;
      if (!resuming) clock.pending = 0;
    } else if (clock.phase && !data.active_phase) {
      clock.phase = null;
      clock.pending = 0;
      clock.base = 0;
      clock.running = false;
    }
    renderHome();
    renderHud();
  }

  async function startPhase(key) {
    await flush();
    const data = await api(`/api/daily/phase/${key}/start`, {
      method: "POST",
      body: JSON.stringify({ day: state.day }),
    });
    clock.phase = key;
    clock.pending = 0;
    clock.base = (phaseOf(data, key) || {}).seconds_spent || 0;
    clock.target = (phaseOf(data, key) || {}).target_seconds || 300;
    clock.running = true;
    clock.lastFlush = Date.now();
    applySession(data);
    goToPhase(key);
  }

  function phaseOf(data, key) {
    return ((data && data.phases) || []).find((p) => p.key === key) || null;
  }

  async function finishPhase(key, status) {
    const seconds = clock.phase === key ? clock.pending : 0;
    clock.pending = 0;
    const data = await api(`/api/daily/phase/${key}/finish`, {
      method: "POST",
      body: JSON.stringify({ day: state.day, seconds, status: status || "done" }),
    });
    clock.phase = null;
    clock.running = false;
    clock.base = 0;
    applySession(data);
    void refreshCalendar();
    return data;
  }

  async function reopenPhase(key) {
    const data = await api(`/api/daily/phase/${key}/reopen`, {
      method: "POST",
      body: JSON.stringify({ day: state.day }),
    });
    applySession(data);
    void refreshCalendar();
  }

  async function flush() {
    if (!clock.phase || clock.pending <= 0) return;
    const seconds = clock.pending;
    const phase = clock.phase;
    clock.pending = 0;
    clock.lastFlush = Date.now();
    try {
      const res = await api(`/api/daily/phase/${phase}/heartbeat`, {
        method: "POST",
        body: JSON.stringify({ day: state.day, seconds }),
      });
      if (res && clock.phase === phase) clock.base = res.seconds_spent || clock.base;
    } catch (_) {
      // Put the seconds back so a dropped request is not lost time.
      if (clock.phase === phase) clock.pending += seconds;
    }
  }

  // ── Handing off to the mode a phase belongs to ────────────────────────── //

  async function goToPhase(key) {
    const phase = phaseByKey(key);
    if (!phase) return;
    if (key === "openings") {
      navigate("/daily/repertoire");
      return;
    }
    navigate(phase.route);
    if (key === "review") await openReviewGame(phase.payload);
  }

  function navigate(path) {
    if (window.__shellNavigate) window.__shellNavigate(path);
    else window.location.href = path;
  }

  // The review tab owns the loader; we only ask it to open the picked game at
  // the ply the review already identified as the costliest move.
  async function openReviewGame(payload) {
    if (!payload || !payload.game_id) return;
    if (typeof window.__volOpenGameById !== "function") return;
    // The vol app was hidden a moment ago; let the shell finish showing it so
    // Chessground measures a non-zero container.
    await new Promise((resolve) => requestAnimationFrame(() => resolve()));
    try {
      await window.__volOpenGameById(payload.game_id, { ply: payload.ply || 1 });
    } catch (_) {
      /* the review tab reports its own failure in-place */
    }
  }

  // ── The HUD ───────────────────────────────────────────────────────────── //

  let hud = null;

  function buildHud() {
    if (hud) return hud;
    const el = document.createElement("div");
    el.id = "daily-hud";
    el.className = "dy-hud hidden";
    el.innerHTML = `
      <button type="button" class="dy-hud-open" id="dyHudOpen" title="Back to the check-in">
        <svg class="dy-hud-ring" viewBox="0 0 44 44" aria-hidden="true">
          <circle class="dy-hud-track" cx="22" cy="22" r="18" />
          <circle class="dy-hud-fill" id="dyHudFill" cx="22" cy="22" r="18" />
        </svg>
        <span class="dy-hud-step" id="dyHudStep">1/5</span>
      </button>
      <div class="dy-hud-body">
        <strong class="dy-hud-title" id="dyHudTitle">—</strong>
        <span class="dy-hud-time" id="dyHudTime">0:00</span>
      </div>
      <div class="dy-hud-actions">
        <button type="button" class="dy-hud-btn" id="dyHudPause" title="Pause the clock">Pause</button>
        <button type="button" class="dy-hud-btn dy-hud-btn--go" id="dyHudDone">Done →</button>
      </div>`;
    document.body.appendChild(el);
    $("dyHudOpen").addEventListener("click", () => navigate("/daily"));
    $("dyHudPause").addEventListener("click", togglePause);
    $("dyHudDone").addEventListener("click", onHudDone);
    hud = el;
    return el;
  }

  function togglePause() {
    clock.running = !clock.running;
    if (!clock.running) void flush();
    renderHud();
  }

  async function onHudDone() {
    const key = clock.phase;
    if (!key) return;
    const data = await finishPhase(key, "done");
    const next = (data.phases || []).find((p) => p.status === "pending");
    if (next) await startPhase(next.key);
    else navigate("/daily");
  }

  function renderHud() {
    const el = buildHud();
    const phase = clock.phase ? phaseByKey(clock.phase) : null;
    // On the check-in page the phase cards already carry the clock; a second
    // copy floating over them is just noise.
    const onHome = state.active && state.view === "home";
    const show = Boolean(phase) && !onHome;
    el.classList.toggle("hidden", !show);
    if (!show || !phase) return;

    const index = ((state.session && state.session.phases) || []).findIndex(
      (p) => p.key === phase.key,
    );
    const total = (state.session && state.session.phases_total) || 5;
    const seconds = clock.base + clock.pending;
    const pct = Math.max(0, Math.min(1, seconds / (clock.target || 300)));
    const ring = 2 * Math.PI * 18;
    $("dyHudStep").textContent = `${index + 1}/${total}`;
    $("dyHudTitle").textContent = phase.title;
    $("dyHudTime").textContent = `${fmtClock(seconds)} / ${fmtClock(clock.target)}`;
    const fill = $("dyHudFill");
    fill.style.strokeDasharray = `${ring}`;
    fill.style.strokeDashoffset = `${ring * (1 - pct)}`;
    el.classList.toggle("is-full", seconds >= clock.target);
    el.classList.toggle("is-paused", !clock.running);
    $("dyHudPause").textContent = clock.running ? "Pause" : "Resume";
  }

  function tick() {
    if (clock.phase && clock.running && document.visibilityState === "visible") {
      clock.pending += 1;
      renderHud();
      if (state.active && state.view === "home") renderPhases();
      if (Date.now() - clock.lastFlush >= FLUSH_MS) void flush();
    }
  }

  // ── Check-in home ─────────────────────────────────────────────────────── //

  function renderHome() {
    if (!state.session) return;
    const s = state.session;
    const d = parseDay(s.day);
    $("dyDate").textContent = `${WEEKDAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
    $("dyRingDone").textContent = String(s.phases_done);
    const fill = $("dyRingFill");
    fill.style.strokeDasharray = `${RING_CIRCUMFERENCE}`;
    fill.style.strokeDashoffset = `${RING_CIRCUMFERENCE * (1 - s.phases_done / s.phases_total)}`;
    const streak = s.streak || { current: 0, best: 0, total_days: 0 };
    $("dyStreak").textContent = String(streak.current);
    $("dyBest").textContent = String(streak.best);
    $("dyTotalDays").textContent = String(streak.total_days);

    const primary = $("dyPrimary");
    if (s.complete) {
      primary.textContent = "Done for today";
      primary.disabled = true;
      $("dySub").textContent =
        `All five phases in ${fmtMinutes(s.seconds_total)}. Come back tomorrow.`;
      $("dyHeroNote").textContent = streak.current
        ? `${streak.current}-day streak.`
        : "";
    } else {
      const next = s.phases.find((p) => p.status === "active")
        || s.phases.find((p) => p.status === "pending");
      primary.disabled = !next;
      primary.textContent = !next
        ? "Nothing left"
        : s.phases_done || s.seconds_total
          ? `Continue — ${next.title}`
          : "Start check-in";
      primary.dataset.phase = next ? next.key : "";
      $("dySub").textContent =
        "Five phases, five minutes each. Stay as long as you like.";
      $("dyHeroNote").textContent = s.seconds_total
        ? `${fmtMinutes(s.seconds_total)} logged today.`
        : "";
    }
    renderPhases();
  }

  const PHASE_STATUS_LABEL = {
    pending: "Not started",
    active: "In progress",
    done: "Done",
    skipped: "Skipped",
  };

  function renderPhases() {
    const list = $("dyPhases");
    if (!list || !state.session) return;
    const phases = state.session.phases || [];
    list.innerHTML = phases
      .map((phase, i) => {
        const live = clock.phase === phase.key;
        const seconds = live ? clock.base + clock.pending : phase.seconds_spent;
        const pct = Math.max(0, Math.min(1, seconds / (phase.target_seconds || 300)));
        const status = phase.status === "active" && !live ? "pending" : phase.status;
        return `
        <li class="dy-phase is-${escapeHtml(status)}${live ? " is-live" : ""}" data-phase="${escapeHtml(phase.key)}">
          <span class="dy-phase-num">${i + 1}</span>
          <div class="dy-phase-main">
            <h3>${escapeHtml(phase.title)}</h3>
            <p>${escapeHtml(phase.blurb)}</p>
            ${phaseNote(phase)}
            <div class="dy-phase-bar"><i style="transform: scaleX(${pct.toFixed(3)})"></i></div>
          </div>
          <div class="dy-phase-side">
            <span class="dy-phase-time">${fmtClock(seconds)}<small> / ${fmtClock(phase.target_seconds || 300)}</small></span>
            <span class="dy-phase-status">${escapeHtml(PHASE_STATUS_LABEL[status] || status)}</span>
            <div class="dy-phase-actions">${phaseActions(phase, live)}</div>
          </div>
        </li>`;
      })
      .join("");
  }

  function phaseNote(phase) {
    if (phase.key !== "review" || !phase.payload) return "";
    const p = phase.payload;
    const white = escapeHtml(p.white_name || "White");
    const black = escapeHtml(p.black_name || "Black");
    const at = p.ply ? ` · opens at ${escapeHtml(p.san || "the miss")}` : "";
    const opening = p.opening ? ` · ${escapeHtml(p.opening)}` : "";
    return `<p class="dy-phase-pick">${white} vs ${black}${opening}${at}</p>`;
  }

  function phaseActions(phase, live) {
    if (phase.status === "done" || phase.status === "skipped") {
      return `<button type="button" class="dy-mini" data-act="reopen">Reopen</button>
              <button type="button" class="dy-mini" data-act="revisit">Revisit</button>`;
    }
    if (live) {
      return `<button type="button" class="dy-mini dy-mini--go" data-act="resume">Resume</button>
              <button type="button" class="dy-mini" data-act="done">Mark done</button>`;
    }
    return `<button type="button" class="dy-mini dy-mini--go" data-act="start">Start</button>
            <button type="button" class="dy-mini" data-act="skip">Skip</button>`;
  }

  async function onPhaseClick(event) {
    const button = event.target.closest("button[data-act]");
    if (!button) return;
    const key = button.closest("[data-phase]").dataset.phase;
    const act = button.dataset.act;
    button.disabled = true;
    try {
      if (act === "start" || act === "resume") await startPhase(key);
      else if (act === "done") await finishPhase(key, "done");
      else if (act === "skip") await finishPhase(key, "skipped");
      else if (act === "reopen") await reopenPhase(key);
      else if (act === "revisit") await goToPhase(key);
    } catch (err) {
      $("dyHeroNote").textContent = err.message || String(err);
    } finally {
      button.disabled = false;
    }
  }

  // ── Calendar ──────────────────────────────────────────────────────────── //

  function monthOf(day) {
    return String(day).slice(0, 7);
  }

  function shiftMonth(month, delta) {
    const [y, m] = month.split("-").map(Number);
    const d = new Date(y, m - 1 + delta, 1);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  }

  async function loadCalendar(month) {
    state.month = month || monthOf(state.day);
    const data = await api(
      `/api/daily/calendar?month=${state.month}&day=${state.day}`,
    );
    state.calendar = data;
    renderCalendar();
  }

  async function refreshCalendar() {
    if (!state.calendar) return;
    try {
      await loadCalendar(state.month);
    } catch (_) {
      /* the calendar is a summary; a failed refresh is not worth a banner */
    }
  }

  function renderCalendar() {
    const grid = $("dyGrid");
    if (!grid || !state.calendar) return;
    const cal = state.calendar;
    const [year, month] = cal.month.split("-").map(Number);
    $("dyMonthLabel").textContent = `${MONTHS[month - 1]} ${year}`;

    const byDay = new Map((cal.days || []).map((d) => [d.day, d]));
    const first = new Date(year, month - 1, 1);
    const daysInMonth = new Date(year, month, 0).getDate();
    // Monday-first: JS gives 0 for Sunday, which would start the week in the
    // wrong column against the weekday header above the grid.
    const lead = (first.getDay() + 6) % 7;
    const cells = [];
    for (let i = 0; i < lead; i += 1) cells.push('<span class="dy-day is-blank"></span>');
    for (let day = 1; day <= daysInMonth; day += 1) {
      const iso = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
      cells.push(dayCell(iso, day, byDay.get(iso)));
    }
    while (cells.length % 7) cells.push('<span class="dy-day is-blank"></span>');
    grid.innerHTML = cells.join("");
  }

  function dayCell(iso, day, record) {
    const done = record ? record.phases_done : 0;
    const total = record ? record.phases_total : 5;
    const classes = ["dy-day"];
    if (iso === state.day) classes.push("is-today");
    if (iso > state.day) classes.push("is-future");
    if (done >= total) classes.push("is-full");
    else if (done > 0) classes.push("is-part");
    if (iso === state.selectedDay) classes.push("is-selected");
    const dots = Array.from({ length: total }, (_, i) =>
      `<i class="${i < done ? "on" : ""}"></i>`,
    ).join("");
    return `<button type="button" class="${classes.join(" ")}" data-day="${iso}">
      <span class="dy-day-num">${day}</span>
      <span class="dy-day-dots">${dots}</span>
    </button>`;
  }

  function onCalendarClick(event) {
    const cell = event.target.closest("button[data-day]");
    if (!cell) return;
    state.selectedDay = cell.dataset.day;
    renderCalendar();
    const record = ((state.calendar && state.calendar.days) || []).find(
      (d) => d.day === state.selectedDay,
    );
    const d = parseDay(state.selectedDay);
    const label = `${WEEKDAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
    if (!record) {
      $("dyCalDetail").textContent =
        state.selectedDay > state.day
          ? `${label} — still to come.`
          : `${label} — nothing logged.`;
      return;
    }
    const titles = (record.done_phases || [])
      .map((key) => {
        const phase = ((state.calendar && state.calendar.phases) || []).find(
          (p) => p.key === key,
        );
        return phase ? phase.title : key;
      })
      // Phase titles contain commas of their own ("One loss, reviewed"), so a
      // comma-joined list reads as one long run-on.
      .join(" · ");
    $("dyCalDetail").textContent =
      `${label} — ${record.phases_done} of ${record.phases_total}, ` +
      `${fmtMinutes(record.seconds_total)}${titles ? `. ${titles}` : "."}`;
  }

  // ── Repertoire: the drill ─────────────────────────────────────────────── //

  function setRepView(name) {
    // Three views now, so this is a switch rather than the old boolean — a
    // two-way toggle silently treated "build" as "drill".
    const view = ["drill", "build", "lines"].includes(name) ? name : "drill";
    $("dyDrill").classList.toggle("hidden", view !== "drill");
    $("dyBuild").classList.toggle("hidden", view !== "build");
    $("dyLinesView").classList.toggle("hidden", view !== "lines");
    document.querySelectorAll("#daily-repertoire [data-rep]").forEach((btn) => {
      const on = btn.dataset.rep === view;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (view === "build" && !build.data) { void loadBuild(true); void loadPrep(); }
    // The book is rebuilt on every entry, not cached: it is edited in the
    // Build tab next door, so anything rendered once at load is stale by the
    // time the user comes back to look at it.
    if (view === "lines") void loadBook().then(renderBook);
    // Chessground measured a hidden container; both boards need a nudge.
    if (view !== "lines") window.dispatchEvent(new Event("resize"));
  }

  async function loadDrill(force) {
    if (drill.loaded && !force) return;
    drill.loaded = true;
    const [queue, repertoire] = await Promise.all([
      api("/api/repertoire/drill?limit=40"),
      api("/api/repertoire"),
    ]);
    drill.cards = queue.cards || [];
    drill.repertoire = repertoire;
    drill.index = 0;
    renderRepCounts(queue, repertoire);
    void renderLines(repertoire);
    if (!drill.cards.length) {
      $("dyRepEmpty").classList.remove("hidden");
      $("dyRepContent").classList.add("hidden");
      $("dyRepEmptyText").textContent =
        queue.reason || repertoire.reason || "Nothing to drill yet.";
      return;
    }
    $("dyRepEmpty").classList.add("hidden");
    $("dyRepContent").classList.remove("hidden");
    showCard(0);
  }

  function renderRepCounts(queue, repertoire) {
    const counts = (queue && queue.counts) || {};
    const parts = [];
    if (counts.fix) parts.push(`<span class="dy-chip dy-chip--fix">${counts.fix} to fix</span>`);
    if (counts.gap) parts.push(`<span class="dy-chip dy-chip--gap">${counts.gap} undecided</span>`);
    if (counts.keep) parts.push(`<span class="dy-chip">${counts.keep} to reinforce</span>`);
    if (repertoire && repertoire.games) {
      parts.push(`<span class="dy-chip dy-chip--muted">from ${repertoire.games} games</span>`);
    }
    $("dyRepCounts").innerHTML = parts.join("");
  }

  const VERDICT_LABEL = { fix: "Fix this", gap: "Undecided", keep: "Your line" };

  function showCard(index) {
    if (index < 0 || index >= drill.cards.length) return;
    drill.index = index;
    drill.current = drill.cards[index];
    drill.answered = false;
    drill.revealed = false;
    const card = drill.current;

    const verdict = $("dyVerdict");
    verdict.textContent = VERDICT_LABEL[card.verdict] || card.verdict;
    verdict.className = `dy-verdict dy-verdict--${card.verdict}`;
    $("dyOpening").textContent = [card.eco, card.opening].filter(Boolean).join(" · ") || "—";
    const side = card.color === "white" ? "White" : "Black";
    $("dyPrompt").textContent = `You are ${side}. What do you play?`;
    // The reason names the move — it is the answer, so it waits until they
    // have committed to one.
    $("dyWhy").textContent =
      card.verdict === "fix"
        ? "The reviews say your usual move here costs you. Find the better one."
        : card.verdict === "gap"
          ? "You have played several different moves here. Play the one you have decided on."
          : "Your own line. Play it without thinking about it.";
    $("dyFeedback").classList.add("hidden");
    $("dyFeedback").textContent = "";
    $("dyAltsWrap").classList.add("hidden");
    $("dyNextCard").disabled = true;
    $("dyShow").disabled = false;
    $("dyDrillProgress").textContent =
      `Card ${index + 1} of ${drill.cards.length}`;
    $("dyLineSan").textContent = formatLine(card.line_san);
    $("dyTurn").textContent = `${side} to move · move ${Math.floor(card.ply / 2) + 1}`;
    drawBoard(card, { interactive: true, shapes: [] });
  }

  function formatLine(sans) {
    if (!sans || !sans.length) return "From the start.";
    const out = [];
    sans.forEach((san, i) => {
      if (i % 2 === 0) out.push(`${i / 2 + 1}.${san}`);
      else out.push(san);
    });
    return out.join(" ");
  }

  function legalDests(chess) {
    const dests = new Map();
    chess.moves({ verbose: true }).forEach((move) => {
      const targets = dests.get(move.from) || [];
      targets.push(move.to);
      dests.set(move.from, targets);
    });
    return dests;
  }

  function drawBoard(card, opts) {
    const el = $("dyBoard");
    if (!el || typeof Chessground === "undefined" || typeof Chess === "undefined") return;
    const chess = new Chess();
    if (!chess.load(card.fen)) return;
    const orientation = card.color === "black" ? "black" : "white";
    const config = {
      fen: card.fen,
      orientation,
      turnColor: chess.turn() === "w" ? "white" : "black",
      coordinates: true,
      viewOnly: false,
      animation: { enabled: true, duration: 150 },
      drawable: { enabled: false, visible: true, autoShapes: opts.shapes || [] },
      movable: {
        free: false,
        color: opts.interactive ? orientation : undefined,
        dests: opts.interactive ? legalDests(chess) : new Map(),
        events: { after: onDrillMove },
      },
    };
    if (drill.cg) drill.cg.set(config);
    else drill.cg = Chessground(el, config);
  }

  async function onDrillMove(from, to) {
    const card = drill.current;
    if (!card) return;
    // Openings never promote inside the first sixteen plies, but a queen is the
    // right default if one ever does — the expected move carries its own suffix.
    const played = from + to + (card.expected_uci.length > 4 ? card.expected_uci[4] : "");
    const correct = played === card.expected_uci
      || from + to === card.expected_uci.slice(0, 4);
    const first = !drill.answered && !drill.revealed;
    drill.answered = true;
    showResult(correct, played);
    if (first) {
      try {
        await api("/api/repertoire/attempt", {
          method: "POST",
          body: JSON.stringify({
            card_id: card.card_id,
            fen: card.fen,
            expected_uci: card.expected_uci,
            played_uci: from + to,
            verdict: card.verdict,
          }),
        });
      } catch (_) {
        /* the answer still stands on screen; the log is best-effort */
      }
    }
  }

  function showResult(correct, playedUci) {
    const card = drill.current;
    const box = $("dyFeedback");
    box.classList.remove("hidden");
    box.className = `dy-feedback ${correct ? "is-right" : "is-wrong"}`;
    const expected = escapeHtml(card.expected_san || card.expected_uci);
    if (correct) {
      box.innerHTML = `<strong>${expected}</strong> — yes. ${escapeHtml(card.reason)}`;
    } else {
      const played = escapeHtml(sanFor(card.fen, playedUci) || playedUci);
      box.innerHTML =
        `<strong>${played}</strong> is not the move here. ` +
        `Play <strong>${expected}</strong>. ${escapeHtml(card.reason)}`;
    }
    renderAlternatives(card);
    $("dyNextCard").disabled = false;
    drawBoard(card, {
      interactive: false,
      shapes: [
        { orig: card.expected_uci.slice(0, 2), dest: card.expected_uci.slice(2, 4), brush: "green" },
      ].concat(
        correct || !playedUci
          ? []
          : [{ orig: playedUci.slice(0, 2), dest: playedUci.slice(2, 4), brush: "red" }],
      ),
    });
  }

  function sanFor(fen, uci) {
    if (typeof Chess === "undefined" || !uci || uci.length < 4) return null;
    const chess = new Chess();
    if (!chess.load(fen)) return null;
    const move = chess.move({
      from: uci.slice(0, 2),
      to: uci.slice(2, 4),
      promotion: uci[4] || "q",
    });
    return move ? move.san : null;
  }

  function renderAlternatives(card) {
    const alts = (card.alternatives || []).filter((a) => a.games > 0);
    if (!alts.length) return;
    $("dyAltsWrap").classList.remove("hidden");
    $("dyAlts").innerHTML = alts
      .map((alt) => {
        const score = alt.score_pct == null ? "—" : `${Math.round(alt.score_pct * 100)}%`;
        const loss = alt.mean_loss == null
          ? ""
          : `<span class="dy-alt-loss">−${alt.mean_loss.toFixed(1)} win%/game</span>`;
        const mark = alt.uci === card.expected_uci ? " is-pick" : "";
        return `<li class="dy-alt${mark}">
          <b>${escapeHtml(alt.san || alt.uci)}</b>
          <span>${alt.games} ${alt.games === 1 ? "game" : "games"} · ${score}</span>
          ${loss}
        </li>`;
      })
      .join("");
  }

  function revealAnswer() {
    const card = drill.current;
    if (!card) return;
    drill.revealed = true;
    showResult(true, null);
    $("dyFeedback").className = "dy-feedback is-shown";
    $("dyFeedback").innerHTML =
      `<strong>${escapeHtml(card.expected_san || card.expected_uci)}</strong>. ` +
      escapeHtml(card.reason);
  }

  function nextCard() {
    if (drill.index + 1 < drill.cards.length) showCard(drill.index + 1);
    else {
      $("dyFeedback").classList.remove("hidden");
      $("dyFeedback").className = "dy-feedback is-done";
      $("dyFeedback").textContent =
        "That is the whole queue. Rebuild it any time, or move on to the next phase.";
      $("dyNextCard").disabled = true;
    }
  }

  // ── Repertoire: the lines view ────────────────────────────────────────── //

  async function renderLines(repertoire) {
    const host = $("dyLines");
    if (!host) return;
    // Two repertoires share this view and they are not the same thing: the
    // book is what you CHOSE in the builder, the lines below are what you
    // actually played. Showing only the mined one made moves added in the
    // builder look like they had gone nowhere.
    renderBook(await loadBook());
    renderMined(repertoire, host);
  }

  async function loadBook() {
    try {
      const [white, black] = await Promise.all([
        api("/api/opening-book/tree?color=white"),
        api("/api/opening-book/tree?color=black"),
      ]);
      return { white, black };
    } catch (_) {
      return null;
    }
  }

  function renderBook(book) {
    const host = $("dyBook");
    if (!host) return;
    if (!book || !(book.white.moves + book.black.moves)) {
      host.innerHTML = "";
      return;
    }
    const blocks = ["white", "black"].map((color) => {
      const tree = book[color];
      if (!tree.moves) return "";
      const lines = (tree.lines || []).slice(0, 12).map(bookLine).join("");
      return `<section class="dy-linecol">
        <h2>Your book as ${color}
          <small>${tree.decisions} decision${tree.decisions === 1 ? "" : "s"}
          · ${tree.moves} moves · ${tree.max_ply} plies deep</small></h2>
        ${lines || '<p class="dy-lines-empty">No complete line yet.</p>'}
      </section>`;
    });
    host.innerHTML = `<div class="dy-booklines">${blocks.join("")}</div>`;
  }

  function bookLine(line) {
    const moves = line.moves
      .map((move, i) => {
        const number = i % 2 === 0 ? `<span class="dy-ln-num">${i / 2 + 1}.</span>` : "";
        const cls = move.role === "mine" ? "dy-ln-move is-keep" : "dy-ln-move";
        return `${number}<span class="${cls}">${escapeHtml(move.san)}</span>`;
      })
      .join(" ");
    return `<article class="dy-linecard">
      <header><h3>${line.plies} plies</h3></header>
      <p class="dy-ln">${moves}</p>
    </article>`;
  }

  function renderMined(repertoire, host) {
    if (!repertoire || !repertoire.available) {
      host.innerHTML = `<p class="dy-lines-empty">${escapeHtml(
        (repertoire && repertoire.reason) || "No repertoire yet.",
      )}</p>`;
      return;
    }
    const blocks = ["white", "black"].map((color) => {
      const lines = (repertoire.lines && repertoire.lines[color]) || [];
      if (!lines.length) {
        return `<section class="dy-linecol">
          <h2>As ${color}</h2>
          <p class="dy-lines-empty">No line repeats often enough yet.</p>
        </section>`;
      }
      return `<section class="dy-linecol">
        <h2>As ${color} <small>${(repertoire.games_by_color || {})[color] || 0} games</small></h2>
        ${lines.map(lineCard).join("")}
      </section>`;
    });
    host.innerHTML = blocks.join("");
  }

  function lineCard(line) {
    const moves = line.moves
      .map((move, i) => {
        const number = i % 2 === 0 ? `<span class="dy-ln-num">${i / 2 + 1}.</span>` : "";
        const cls = move.user_to_move ? `dy-ln-move is-${move.verdict || "keep"}` : "dy-ln-move";
        const title = move.reason ? ` title="${escapeHtml(move.reason)}"` : "";
        return `${number}<span class="${cls}"${title}>${escapeHtml(move.san)}</span>`;
      })
      .join(" ");
    const score = line.score_pct == null ? "—" : `${Math.round(line.score_pct * 100)}%`;
    const roi = line.roi_score
      ? `<span class="dy-chip dy-chip--roi">${line.roi_score.toFixed(1)} pts/100 at stake</span>`
      : "";
    return `<article class="dy-linecard">
      <header>
        <h3>${escapeHtml(line.opening || "Unnamed line")}</h3>
        <span class="dy-chip dy-chip--muted">${escapeHtml(line.eco || "")}</span>
        <span class="dy-chip dy-chip--muted">${line.games} games · ${score}</span>
        ${roi}
      </header>
      <p class="dy-ln">${moves}</p>
    </article>`;
  }

  // ── Repertoire: the builder ───────────────────────────────────────────── //
  //
  // A Chessbook-style position-by-position tree: walk the board, read the
  // evidence, pick a move. The three dots per row are the distinctive part —
  // engine / human / results — and each is TRI-state. `null` renders as a
  // hollow ring ("we did not ask"), never as an unlit dot ("we asked and the
  // answer is no"); collapsing those two is the one way this display can lie,
  // and on a box with no Maia or no network it would lie on every row.

  const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const SIGNAL_KEYS = ["engine", "human", "results"];
  const SIGNAL_LABEL = {
    engine: "Stockfish's top move",
    human: "the move Maia plays",
    results: "best score in the Lichess database",
  };
  const COLLAPSED_ROWS = 6;

  const build = {
    color: "white",
    fen: START_FEN,
    path: [],          // uci moves from the start, for the back button
    sans: [],
    data: null,
    cg: null,
    expanded: false,
    busy: false,
    prep: null,
  };

  async function loadBuild(force) {
    if (build.busy && !force) return;
    build.busy = true;
    setText("dyBuildStatus", "Reading the position…");
    try {
      const qs = `fen=${encodeURIComponent(build.fen)}&color=${build.color}`;
      const data = await api(`/api/opening-book/candidates?${qs}`);
      build.data = data;
      renderBuild(data);
      void loadBuildCoverage();
    } catch (err) {
      setText("dyBuildStatus", `Could not read that position: ${err.message || err}`);
    } finally {
      build.busy = false;
    }
  }

  async function loadBuildCoverage() {
    const el = $("dyBuildCoverage");
    if (!el) return;
    try {
      const qs = `fen=${encodeURIComponent(build.fen)}&color=${build.color}`;
      const cov = await api(`/api/opening-book/coverage?${qs}`);
      if (cov.coverage == null) {
        // Null, not zero: "we do not know what you face" is not "you have
        // covered nothing", and only one of those is the user's fault.
        el.textContent = "";
        el.title = "";
        return;
      }
      const pct = Math.round(cov.coverage * 100);
      el.textContent = `${pct}% covered`;
      el.className = `dy-build-cover ${pct >= 80 ? "is-good" : pct >= 40 ? "is-mid" : "is-low"}`;
      el.title = cov.uncovered.length
        ? `Biggest gap: ${cov.uncovered[0].san || cov.uncovered[0].uci} `
          + `(${Math.round(cov.uncovered[0].share * 100)}% of replies)`
        : "Every reply you meet has an answer.";
    } catch (_) {
      el.textContent = "";
    }
  }

  function signalDot(key, value) {
    // Three states, three classes. `is-off` and `is-unknown` must look
    // different or the whole display is untrustworthy.
    const state = value === true ? "is-on" : value === false ? "is-off" : "is-unknown";
    const word = value === true ? "yes" : value === false ? "no" : "not available";
    return `<i class="dy-dot dy-dot--${key} ${state}" title="${escapeHtml(
      `${SIGNAL_LABEL[key]}: ${word}`,
    )}"></i>`;
  }

  function candidateRow(c) {
    const dots = SIGNAL_KEYS.map((k) => signalDot(k, c.signals[k])).join("");
    const lit = SIGNAL_KEYS.filter((k) => c.signals[k] === true).length;
    const peer = c.peer_score == null
      ? "—"
      : `${Math.round(c.peer_score * 100)}%`;
    const games = c.peer_games ? fmtCount(c.peer_games) : "—";
    const mast = c.master_share ? `${(c.master_share * 100).toFixed(1)}%` : "—";
    const evalCell = c.eval_cp == null
      ? "—"
      : `${c.eval_cp > 0 ? "+" : ""}${(c.eval_cp / 100).toFixed(2)}`;
    const flags = [];
    if (c.in_repertoire) flags.push('<span class="dy-flag is-mine">in your book</span>');
    if (c.low_sample) flags.push('<span class="dy-flag">few games</span>');
    if (c.obscure) flags.push('<span class="dy-flag is-warn">rare</span>');
    return `<div class="dy-cand${c.in_repertoire ? " is-mine" : ""}${
      lit === 3 ? " is-unanimous" : ""
    }" data-uci="${escapeHtml(c.uci)}">
      <button type="button" class="dy-cand-main" data-act="play" data-uci="${escapeHtml(c.uci)}">
        <span class="dy-cand-san">${escapeHtml(c.san)}</span>
        <span class="dy-cand-dots">${dots}</span>
        <span class="dy-cand-stat" title="Share of master games">${mast}</span>
        <span class="dy-cand-stat" title="Your rating band's score with this move">${peer}</span>
        <span class="dy-cand-stat dy-cand-n" title="Games at your level">${games}</span>
        <span class="dy-cand-stat dy-cand-eval" title="Engine evaluation">${evalCell}</span>
      </button>
      <div class="dy-cand-side">
        ${flags.join("")}
        <button type="button" class="dy-mini ${c.in_repertoire ? "" : "dy-mini--go"}"
                data-act="${c.in_repertoire ? "remove" : "add"}"
                data-uci="${escapeHtml(c.uci)}">${c.in_repertoire ? "Remove" : "Add"}</button>
      </div>
    </div>`;
  }

  function fmtCount(n) {
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${Math.round(n / 1000)}k`;
    return String(n);
  }

  function renderBuild(data) {
    drawBuildBoard(data);
    setText("dyBuildSan", build.sans.length ? prettyLine(build.sans) : "Start position");
    setText(
      "dyBuildOpening",
      data.opening_name || (data.opening_eco ? data.opening_eco : "—"),
    );
    setText(
      "dyBuildPrompt",
      data.user_to_move ? "What do you play here?" : "What do they play here?",
    );
    setText(
      "dyBuildWhy",
      data.user_to_move
        ? "Pick your move. It becomes a card you get drilled on."
        : "Cover the replies you actually meet — you are not drilled on these.",
    );

    // Say plainly which signals are live. A grey dot with no explanation is
    // worse than no dot at all.
    const src = data.sources || {};
    const missing = [];
    if (src.engine !== "engine") missing.push("engine");
    if (src.maia !== "maia") missing.push("human");
    if (src.peers !== "cache" && src.peers !== "network") missing.push("results");
    setText(
      "dyBuildLegendNote",
      missing.length
        ? `· ${missing.join(" and ")} unavailable here (hollow = not asked)`
        : `· Maia ${src.maia_rating || 1900}`,
    );

    const cands = data.candidates || [];
    const shown = build.expanded ? cands : cands.slice(0, COLLAPSED_ROWS);
    const host = $("dyBuildCands");
    if (host) {
      host.innerHTML = shown.length
        ? shown.map(candidateRow).join("")
        : `<p class="dy-lines-empty">No candidate data for this position.
             Any legal move on the board still works.</p>`;
    }
    const more = $("dyBuildMore");
    if (more) {
      more.classList.toggle("hidden", cands.length <= COLLAPSED_ROWS);
      more.textContent = build.expanded
        ? "Show fewer"
        : `Show ${cands.length - COLLAPSED_ROWS} more`;
    }
    const chosen = (data.chosen || []).length;
    setText(
      "dyBuildStatus",
      chosen
        ? `${chosen} move${chosen === 1 ? "" : "s"} chosen here.`
        : "Nothing chosen at this position yet.",
    );
  }

  function prettyLine(sans) {
    return sans
      .map((san, i) => (i % 2 === 0 ? `${i / 2 + 1}. ${san}` : san))
      .join(" ");
  }

  function drawBuildBoard(data) {
    const el = $("dyBuildBoard");
    if (!el || typeof Chessground === "undefined" || typeof Chess === "undefined") return;
    const chess = new Chess();
    if (!chess.load(build.fen)) return;
    const config = {
      fen: build.fen,
      orientation: build.color === "black" ? "black" : "white",
      turnColor: chess.turn() === "w" ? "white" : "black",
      coordinates: true,
      viewOnly: false,
      animation: { enabled: true, duration: 150 },
      drawable: { enabled: false, visible: true, autoShapes: [] },
      movable: {
        free: false,
        color: chess.turn() === "w" ? "white" : "black",
        dests: legalDests(chess),
        // The spec is explicit that an unlisted legal move must work
        // ("Something else…"), so the board is always live — the candidate
        // table ranks, it does not gate.
        events: { after: (from, to) => void playBuildMove(from + to) },
      },
    };
    if (build.cg) build.cg.set(config);
    else build.cg = Chessground(el, config);
  }

  async function playBuildMove(uci) {
    const chess = new Chess();
    if (!chess.load(build.fen)) return;
    const from = uci.slice(0, 2);
    const to = uci.slice(2, 4);
    const move = chess.move({ from, to, promotion: uci[4] || "q" });
    if (!move) return;
    build.path.push(move.from + move.to + (move.promotion || ""));
    build.sans.push(move.san);
    build.fen = chess.fen();
    build.expanded = false;
    await loadBuild(true);
  }

  async function addBuildMove(uci) {
    try {
      const cand = (build.data?.candidates || []).find((c) => c.uci === uci);
      await api("/api/opening-book/edges", {
        method: "POST",
        body: JSON.stringify({
          color: build.color,
          fen: build.fen,
          uci,
          path: build.path,
          // Snapshot the dots as they were when the choice was made — the
          // evidence moves underneath and "I picked this when all three agreed"
          // is only interpretable against what it said then.
          signals: cand ? cand.signals : null,
          opening_name: build.data?.opening_name || null,
          opening_eco: build.data?.opening_eco || null,
        }),
      });
      await loadBuild(true);
    } catch (err) {
      setText("dyBuildStatus", `Could not add that move: ${err.message || err}`);
    }
  }

  async function removeBuildMove(uci) {
    try {
      await api("/api/opening-book/edges/delete", {
        method: "POST",
        body: JSON.stringify({ color: build.color, fen: build.fen, uci }),
      });
      await loadBuild(true);
    } catch (err) {
      setText("dyBuildStatus", `Could not remove that move: ${err.message || err}`);
    }
  }

  function buildBack() {
    if (!build.path.length) return;
    build.path.pop();
    build.sans.pop();
    const chess = new Chess();
    build.path.forEach((u) =>
      chess.move({ from: u.slice(0, 2), to: u.slice(2, 4), promotion: u[4] || "q" }),
    );
    build.fen = chess.fen();
    build.expanded = false;
    void loadBuild(true);
  }

  function buildReset() {
    build.path = [];
    build.sans = [];
    build.fen = START_FEN;
    build.expanded = false;
    void loadBuild(true);
  }

  // ── Build: prep suggestions from Insights ─────────────────────────────── //
  //
  // The ROI leak board already knows which openings cost the most; this is the
  // hand-off from that answer into somewhere to act on it. The ranking is NOT
  // recomputed here — a second ranking that can disagree with the dashboard is
  // worse than one that can be wrong.

  async function loadPrep() {
    const host = $("dyPrepList");
    const wrap = $("dyPrep");
    if (!host || !wrap) return;
    try {
      const data = await api("/api/opening-book/prep");
      build.prep = data;
      renderPrep(data);
    } catch (_) {
      wrap.classList.add("hidden");
    }
  }

  function renderPrep(data) {
    const host = $("dyPrepList");
    const wrap = $("dyPrep");
    if (!host || !wrap) return;
    const rows = (data && data.suggestions) || [];
    if (!rows.length) {
      // A reason is shown only when there is something to say; "run Insights"
      // is useful, "nothing is below par" is a quiet good outcome.
      if (data && data.reason && data.available === false) {
        wrap.classList.remove("hidden");
        host.innerHTML = `<p class="dy-lines-empty">${escapeHtml(data.reason)}</p>`;
      } else {
        wrap.classList.add("hidden");
      }
      return;
    }
    wrap.classList.remove("hidden");
    host.innerHTML = rows.map(prepRow).join("");
  }

  function prepRow(s) {
    const line = s.line_san && s.line_san.length ? prettyLine(s.line_san) : "from move one";
    const done = s.in_book && s.first_gap_ply == null;
    return `<article class="dy-prepcard${done ? " is-done" : ""}">
      <header>
        <h4>${escapeHtml(s.opening || "Unnamed line")}
          <small>as ${escapeHtml(s.color)}</small></h4>
        <span class="dy-chip dy-chip--roi">${(s.points_per_100_games || 0).toFixed(1)} pts/100</span>
      </header>
      <p class="dy-prep-line">${escapeHtml(line)}</p>
      <p class="dy-prep-why">${escapeHtml(s.why || "")}</p>
      <button type="button" class="dy-mini dy-mini--go" data-prep="${escapeHtml(s.fen)}"
              data-prep-color="${escapeHtml(s.color)}"
              data-prep-line="${escapeHtml((s.line_uci || []).join(" "))}">
        ${done ? "Review it" : "Prep this"}
      </button>
    </article>`;
  }

  async function openPrep(fen, color, lineUci) {
    // Jump the builder straight to the position the games actually reach.
    build.color = color === "black" ? "black" : "white";
    const select = $("dyBuildColor");
    if (select) select.value = build.color;
    build.path = lineUci ? lineUci.split(" ").filter(Boolean) : [];
    build.fen = fen;
    build.sans = [];
    try {
      const chess = new Chess();
      build.path.forEach((u) => {
        const mv = chess.move({ from: u.slice(0, 2), to: u.slice(2, 4), promotion: u[4] || "q" });
        if (mv) build.sans.push(mv.san);
      });
    } catch (_) {
      build.sans = [];
    }
    build.expanded = false;
    await loadBuild(true);
  }

  // ── Build: PGN import ─────────────────────────────────────────────────── //

  async function importPgn() {
    const box = $("dyImportText");
    const status = $("dyImportStatus");
    const button = $("dyImportGo");
    if (!box || !status) return;
    const pgn = box.value.trim();
    if (!pgn) { status.textContent = "Paste a PGN first."; return; }
    if (button) button.disabled = true;
    status.textContent = "Importing…";
    try {
      const report = await api("/api/opening-book/import", {
        method: "POST",
        body: JSON.stringify({ color: build.color, pgn }),
      });
      status.textContent = report.message || "Done.";
      // A paste with none of your own moves in it is a wrong-colour import and
      // is never drilled, so it gets the warning colour rather than success.
      status.className = "dy-import-status"
        + (report.added && !report.decisions ? " is-warn" : report.added ? " is-good" : "");
      if (report.added) {
        box.value = "";
        await loadBuild(true);
      }
    } catch (err) {
      status.textContent = err.message || String(err);
      status.className = "dy-import-status is-warn";
    } finally {
      if (button) button.disabled = false;
    }
  }

  function wireBuild() {
    const host = $("dyBuildCands");
    if (host) {
      host.addEventListener("click", (event) => {
        const btn = event.target.closest("button[data-act]");
        if (!btn) return;
        const uci = btn.dataset.uci;
        if (btn.dataset.act === "add") void addBuildMove(uci);
        else if (btn.dataset.act === "remove") void removeBuildMove(uci);
        else void playBuildMove(uci);
      });
    }
    $("dyBuildBack")?.addEventListener("click", buildBack);
    $("dyBuildStart")?.addEventListener("click", buildReset);
    $("dyBuildMore")?.addEventListener("click", () => {
      build.expanded = !build.expanded;
      if (build.data) renderBuild(build.data);
    });
    $("dyBuildColor")?.addEventListener("change", (event) => {
      build.color = event.target.value === "black" ? "black" : "white";
      buildReset();
    });
    $("dyPrepList")?.addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-prep]");
      if (!btn) return;
      void openPrep(btn.dataset.prep, btn.dataset.prepColor, btn.dataset.prepLine);
    });
    $("dyPrepToggle")?.addEventListener("click", () => {
      const list = $("dyPrepList");
      const hidden = list.classList.toggle("hidden");
      $("dyPrepToggle").textContent = hidden ? "Show" : "Hide";
    });
    $("dyImportGo")?.addEventListener("click", () => void importPgn());
  }

  // ── Boot ──────────────────────────────────────────────────────────────── //

  function wire() {
    if (state.booted) return;
    state.booted = true;
    buildHud();

    $("dyPhases").addEventListener("click", (e) => void onPhaseClick(e));
    $("dyGrid").addEventListener("click", onCalendarClick);
    $("dyPrimary").addEventListener("click", async () => {
      const key = $("dyPrimary").dataset.phase;
      if (key) await startPhase(key);
    });
    $("dyPrevMonth").addEventListener("click", () =>
      void loadCalendar(shiftMonth(state.month, -1)),
    );
    $("dyNextMonth").addEventListener("click", () =>
      void loadCalendar(shiftMonth(state.month, 1)),
    );

    $("dyRepBack").addEventListener("click", () => navigate("/daily"));
    $("dyRepRefresh").addEventListener("click", () => void loadDrill(true));
    $("dyShow").addEventListener("click", revealAnswer);
    $("dyNextCard").addEventListener("click", nextCard);
    $("dyRepEmptyCta").addEventListener("click", () => navigate("/game-review"));
    document.querySelectorAll("#daily-repertoire [data-rep]").forEach((btn) => {
      btn.addEventListener("click", () => setRepView(btn.dataset.rep));
    });
    wireBuild();

    setInterval(tick, 1000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") void flush();
    });
    window.addEventListener("pagehide", () => {
      // `keepalive` is what makes this survive the unload; a plain fetch is
      // cancelled and the last few seconds are lost.
      if (!clock.phase || clock.pending <= 0) return;
      navigator.sendBeacon?.(
        `/api/daily/phase/${clock.phase}/heartbeat`,
        new Blob(
          [JSON.stringify({ day: state.day, seconds: clock.pending })],
          { type: "application/json" },
        ),
      );
      clock.pending = 0;
    });
  }

  async function activate(path) {
    state.view = path === "/daily/repertoire" ? "repertoire" : "home";
    $("daily-home").classList.toggle("hidden", state.view !== "home");
    $("daily-repertoire").classList.toggle("hidden", state.view !== "repertoire");
    try {
      await loadToday();
      if (state.view === "home") {
        if (!state.calendar || state.month !== monthOf(state.day)) {
          await loadCalendar(monthOf(state.day));
        } else {
          await refreshCalendar();
        }
      } else {
        await loadDrill(false);
        // Chessground measures on insert and the root was hidden until now.
        window.dispatchEvent(new Event("resize"));
      }
    } catch (err) {
      $("dyHeroNote").textContent = err.message || String(err);
    }
    renderHud();
  }

  window.__dailySetActive = function (isActive, path) {
    wire();
    state.active = !!isActive;
    state.path = path || state.path;
    if (!state.active) {
      renderHud();
      return;
    }
    void activate(state.path);
  };

  // The HUD has to come back after a reload even when the player lands on
  // another tab, so the session is fetched on boot rather than on activation.
  async function restore() {
    wire();
    try {
      await loadToday();
      if (clock.phase) clock.running = true;
    } catch (_) {
      /* not signed in yet — `chessmax:authenticated` retries */
    }
    renderHud();
  }

  document.addEventListener("chessmax:authenticated", () => void restore());
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => void restore());
  } else {
    void restore();
  }
})();
