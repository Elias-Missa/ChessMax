/* Insights Cinema — the generation Forge, and the shell the film runs inside.
 *
 * Two surfaces, because they are two halves of one arc: the Forge is what a run
 * looks like while it computes, the film is what it looks like when it is done.
 *
 *   window.__insightsForge  = { open, update, ready, fail, close, isOpen }
 *   window.__insightsCinema = { open, close, adopt, route, isOpen }
 *
 * This file owns the chrome — the overlay, the transport bar, the Atlas, the
 * routing and the keyboard. `cinema-film.js` owns the strip itself and reads
 * the helpers below through `window.__insightsCinemaInternals`. The split is
 * along the seam that matters: everything here works without GSAP, everything
 * there is the animation.
 *
 * Nothing in either file computes a metric. Every number comes from
 * `metrics.pro`, `metrics.narrative` or `metrics.game_explorer` — the same
 * contract the dashboard and the post-mortem read, so the film can never
 * disagree with the tables behind it.
 */
(function () {
  const root = document.getElementById("insights-root");
  const forgeEl = document.getElementById("insights-forge");
  const cineEl = document.getElementById("insights-cinema");
  if (!root || !forgeEl || !cineEl) return;

  // ── Small utilities ─────────────────────────────────────────────────────

  const $ = (sel, r) => (r || document).querySelector(sel);
  const $$ = (sel, r) => Array.from((r || document).querySelectorAll(sel));

  const reduced = () =>
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  const isNum = (v) => v !== null && v !== undefined && Number.isFinite(Number(v));
  const n0 = (v) => (isNum(v) ? Math.round(Number(v)) : null);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const pct1 = (v) => (isNum(v) ? `${Math.round(Number(v) * 100)}%` : "—");

  function signed(v, digits) {
    if (!isNum(v)) return "—";
    const x = Number(v);
    return `${x > 0 ? "+" : ""}${x.toFixed(digits || 0)}`;
  }

  function titleCase(s) {
    return String(s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  /** Build an element from an HTML string — one node, no wrapper div. */
  function node(html) {
    const t = document.createElement("template");
    t.innerHTML = String(html).trim();
    return t.content.firstElementChild;
  }

  /** Two frames: one to commit the start state, one to transition off it. */
  function raf2(fn) {
    requestAnimationFrame(() => requestAnimationFrame(fn));
  }

  const easeOut = (t) => 1 - Math.pow(1 - t, 3);

  /** Animate a number into a node. Returns immediately under reduced motion. */
  function countUp(el, to, opts) {
    const o = opts || {};
    const fmt = o.fmt || ((v) => String(Math.round(v)));
    const from = isNum(o.from) ? Number(o.from) : 0;
    const target = Number(to);
    if (!isNum(target)) { el.textContent = o.fallback || "—"; return; }
    if (reduced()) { el.textContent = fmt(target); return; }
    const dur = o.dur || 1100;
    const delay = o.delay || 0;
    const start = performance.now() + delay;
    el.textContent = fmt(from);
    function step(now) {
      if (now < start) { requestAnimationFrame(step); return; }
      const t = clamp((now - start) / dur, 0, 1);
      el.textContent = fmt(from + (target - from) * easeOut(t));
      if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  /** Stroke-dash draw-on for any SVG path already in the document. */
  function drawPath(path, opts) {
    if (!path || !path.getTotalLength) return;
    const len = path.getTotalLength();
    path.style.strokeDasharray = `${len}`;
    path.style.strokeDashoffset = `${len}`;
    if (reduced()) { path.style.strokeDashoffset = "0"; return; }
    const o = opts || {};
    path.style.transitionDuration = `${o.dur || 1500}ms`;
    path.style.transitionDelay = `${o.delay || 0}ms`;
    raf2(() => { path.style.strokeDashoffset = "0"; });
  }

  // ══════════════════════════════════════════════════════════════════════
  // FORGE — generation
  // ══════════════════════════════════════════════════════════════════════

  const STEPS = [
    { id: "fetching", label: "Fetch" },
    { id: "analyzing", label: "Analyze" },
    { id: "measuring", label: "Measure" },
    { id: "practice", label: "Select" },
    { id: "story", label: "Compose" },
  ];

  const STAGE_COPY = {
    pending: { kicker: "Standing by", line: "Opening the run" },
    fetching: { kicker: "Step one", line: "Collecting your games" },
    analyzing: { kicker: "Step two", line: "Walking every move through the engine" },
    measuring: { kicker: "Step three", line: "Measuring what each move cost you" },
    practice: { kicker: "Step four", line: "Choosing the positions worth drilling" },
    story: { kicker: "Step five", line: "Writing your report" },
    complete: { kicker: "Ready", line: "Your report is finished" },
    error: { kicker: "Stopped", line: "The run could not finish" },
  };

  const forge = {
    ring: null,
    circumference: 0,
    lastStage: null,
    seen: new Set(),
    onCancel: null,
  };

  function forgeBuild() {
    forgeEl.innerHTML = `
      <div class="cine-bg"></div>
      <div class="cine-vignette"></div>
      <div class="cine-grain"></div>
      <div class="forge-core">
        <div class="forge-ring">
          <svg viewBox="0 0 120 120" aria-hidden="true">
            <defs>
              <linearGradient id="forge-grad" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stop-color="#44d62c"/>
                <stop offset="55%" stop-color="#8bff70"/>
                <stop offset="100%" stop-color="#5ad2ff"/>
              </linearGradient>
            </defs>
            <circle class="forge-track" cx="60" cy="60" r="52"></circle>
            <circle class="forge-arc" id="forge-arc" cx="60" cy="60" r="52"></circle>
            <circle class="forge-sweep" cx="60" cy="60" r="44"
                    stroke-dasharray="18 260"></circle>
          </svg>
          <div class="forge-readout">
            <div class="forge-pct" id="forge-pct">0<sup>%</sup></div>
            <div class="forge-count" id="forge-count">warming up</div>
          </div>
        </div>

        <div class="forge-stage">
          <p class="forge-kicker" id="forge-kicker">Standing by</p>
          <p class="forge-line" id="forge-line">Opening the run</p>
          <p class="forge-detail" id="forge-detail"></p>
        </div>

        <div class="forge-steps" id="forge-steps">
          ${STEPS.map((s) => `<span class="forge-step" data-step="${s.id}"><i></i>${esc(s.label)}</span>`).join("")}
        </div>

        <div class="forge-stream" id="forge-stream" aria-hidden="true"></div>

        <div class="forge-foot">
          <p class="forge-hint" id="forge-hint">This runs on your machine — the engine is analyzing every move you played.</p>
          <button type="button" class="forge-quit" id="forge-quit">Run in background</button>
        </div>
      </div>`;

    forge.ring = $("#forge-arc", forgeEl);
    const r = 52;
    forge.circumference = 2 * Math.PI * r;
    forge.ring.style.strokeDasharray = `${forge.circumference}`;
    forge.ring.style.strokeDashoffset = `${forge.circumference}`;

    $("#forge-quit", forgeEl).addEventListener("click", () => {
      forgeClose();
      if (forge.onCancel) forge.onCancel();
    });
  }

  function forgeOpen(meta, opts) {
    if (!forge.ring) forgeBuild();
    forge.lastStage = null;
    forge.seen = new Set();
    forge.onCancel = (opts && opts.onBackground) || null;
    $("#forge-stream", forgeEl).innerHTML = "";
    $("#forge-detail", forgeEl).textContent = "";
    $("#forge-count", forgeEl).textContent = "warming up";
    $("#forge-pct", forgeEl).innerHTML = `0<sup>%</sup>`;
    forge.ring.style.strokeDashoffset = `${forge.circumference}`;
    $$(".forge-step", forgeEl).forEach((s) => s.classList.remove("is-done", "is-live"));
    forgeStage("pending", "");
    forgeEl.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    const handle = (meta && (meta.handle || meta.username)) || "";
    if (handle) {
      $("#forge-hint", forgeEl).textContent =
        `Analyzing ${handle}'s games on this machine — every move goes through the engine.`;
    }
  }

  function forgeStage(stage, detail) {
    const copy = STAGE_COPY[stage] || STAGE_COPY.pending;
    const kicker = $("#forge-kicker", forgeEl);
    const line = $("#forge-line", forgeEl);
    if (forge.lastStage !== stage) {
      forge.lastStage = stage;
      kicker.textContent = copy.kicker;
      line.textContent = copy.line;
      [kicker, line].forEach((el) => {
        el.classList.remove("forge-swap");
        void el.offsetWidth;
        el.classList.add("forge-swap");
      });
      let passed = true;
      $$(".forge-step", forgeEl).forEach((el) => {
        const isLive = el.dataset.step === stage;
        if (isLive) passed = false;
        el.classList.toggle("is-live", isLive);
        el.classList.toggle("is-done", passed && !isLive);
      });
      if (stage === "complete") {
        $$(".forge-step", forgeEl).forEach((el) => {
          el.classList.remove("is-live");
          el.classList.add("is-done");
        });
      }
    }
    if (detail !== undefined && detail !== null) {
      $("#forge-detail", forgeEl).textContent = detail;
    }
  }

  /**
   * Called on every poll. `data` is the raw /api/insights/{id} payload, so the
   * narration tracks real server phases rather than a fabricated timeline.
   */
  function forgeUpdate(data) {
    if (forgeEl.classList.contains("hidden")) return;
    const progress = clamp(Number(data.progress || 0), 0, 1);
    const total = Number(data.games_total || 0);
    const done = Number(data.games_analyzed || 0);

    // The ring reserves the last 8% for the post-analysis phases, which have no
    // progress of their own but are not instant either.
    const shown = data.status === "complete" ? 1 : progress * 0.92;
    forge.ring.style.strokeDashoffset = `${forge.circumference * (1 - shown)}`;
    $("#forge-pct", forgeEl).innerHTML = `${Math.round(shown * 100)}<sup>%</sup>`;
    $("#forge-count", forgeEl).textContent = total
      ? `${done} / ${total} games`
      : done ? `${done} games` : "warming up";

    const stage = data.stage || (data.status === "complete" ? "complete" : "analyzing");
    forgeStage(stage, data.stage_detail || "");

    // One card per newly analyzed game. The ring moves once a game; the stream
    // moves every time, which is what keeps a four-minute run alive.
    if (data.stage === "analyzing" && data.stage_detail) {
      const key = `${done}:${data.stage_detail}`;
      if (!forge.seen.has(key) && done > 0) {
        forge.seen.add(key);
        forgeCard(data.stage_detail, done);
      }
    }
  }

  function forgeCard(text, index) {
    const stream = $("#forge-stream", forgeEl);
    if (!stream) return;
    const card = node(`
      <div class="forge-card">
        <b>${esc(text)}</b>
        <span>game ${index}</span>
      </div>`);
    stream.appendChild(card);
    while (stream.children.length > 6) stream.removeChild(stream.firstElementChild);
  }

  /**
   * The run finished. Hold on a completion beat rather than snapping to the
   * launcher — then roll the film. `onPlay` runs either when the button is hit
   * or when the countdown lapses; `onDismiss` when the viewer opts out.
   */
  function forgeReady(summary, onPlay, onDismiss) {
    if (forgeEl.classList.contains("hidden")) { if (onPlay) onPlay(); return; }
    forgeStage("complete", summary || "");
    const foot = $(".forge-foot", forgeEl);
    if (!foot) { if (onPlay) onPlay(); return; }

    let countdown = reduced() ? 0 : 3;
    let timer = null;
    const stop = () => { if (timer) clearInterval(timer); timer = null; };

    foot.innerHTML = `
      <button type="button" class="cine-btn cine-btn--go" id="forge-play"
              style="font-size:.92rem; padding:.55rem 1.3rem">
        <span id="forge-play-label">Play your report</span>
      </button>
      <button type="button" class="forge-quit" id="forge-later">Just show me the launcher</button>`;

    const play = () => { stop(); if (onPlay) onPlay(); };
    $("#forge-play", forgeEl).addEventListener("click", play);
    $("#forge-later", forgeEl).addEventListener("click", () => {
      stop();
      forgeClose();
      if (onDismiss) onDismiss();
    });

    if (!countdown) { play(); return; }
    const label = $("#forge-play-label", forgeEl);
    label.textContent = `Play your report · ${countdown}`;
    timer = setInterval(() => {
      countdown -= 1;
      if (countdown <= 0) { play(); return; }
      label.textContent = `Play your report · ${countdown}`;
    }, 1000);
  }

  function forgeFail(message) {
    if (forgeEl.classList.contains("hidden")) return;
    forgeStage("error", message || "");
    $("#forge-quit", forgeEl).textContent = "Close";
  }

  function forgeClose() {
    forgeEl.classList.add("hidden");
    if (cineEl.classList.contains("hidden")) document.body.style.overflow = "";
  }

  // ══════════════════════════════════════════════════════════════════════
  // CINEMA — the shell around the film
  // ══════════════════════════════════════════════════════════════════════

  const cine = {
    metrics: null,
    meta: {},
    grounds: [],
    arenaObserver: null,
    built: false,
  };

  const film = () => window.__insightsFilm || null;

  // ── Boards ──────────────────────────────────────────────────────────────

  function destroyGrounds() {
    cine.grounds.forEach((g) => {
      try { g.destroy && g.destroy(); } catch { /* ignore */ }
    });
    cine.grounds = [];
  }

  function mountBoard(el) {
    if (!el || !window.Chessground || !el.dataset.fen || el._cgMounted) return null;
    let wrap = el.querySelector(".cg-wrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.className = "cg-wrap";
      el.appendChild(wrap);
    }
    const shapes = [];
    const played = el.dataset.played || "";
    const best = el.dataset.best || "";
    if (played.length >= 4) shapes.push({ orig: played.slice(0, 2), dest: played.slice(2, 4), brush: "red" });
    if (best.length >= 4) shapes.push({ orig: best.slice(0, 2), dest: best.slice(2, 4), brush: "green" });
    const ground = window.Chessground(wrap, {
      fen: el.dataset.fen,
      orientation: el.dataset.color === "black" ? "black" : "white",
      viewOnly: true,
      coordinates: false,
      drawable: { enabled: false, visible: true, autoShapes: shapes },
    });
    el._cgMounted = true;
    requestAnimationFrame(() => { try { ground.redrawAll && ground.redrawAll(); } catch { /* ignore */ } });
    cine.grounds.push(ground);
    return ground;
  }

  function safeParse(s) {
    try { return JSON.parse(s); } catch { return null; }
  }

  // The film module reads these rather than duplicating them.
  window.__insightsCinemaInternals = {
    cineEl, root, $, $$, esc, isNum, clamp, node, titleCase, reduced,
    mountBoard, destroyGrounds, safeParse,
    showAtlas: () => showAtlas(),
  };

  // ── Chrome ──────────────────────────────────────────────────────────────

  function cineBuild() {
    cineEl.innerHTML = `
      <canvas id="cine-fx" class="cine-fx" aria-hidden="true"></canvas>
      <div class="cine-bg"></div>
      <div class="cine-vignette"></div>
      <div class="cine-grain"></div>
      <div class="cine-edge cine-edge--l" aria-hidden="true"></div>
      <div class="cine-edge cine-edge--r" aria-hidden="true"></div>

      <div class="cine-rail">
        <div class="cine-rail-bar"><i id="cine-rail-fill"></i></div>
        <div class="cine-ticks" id="cine-ticks" role="tablist" aria-label="Chapters"></div>
      </div>

      <div class="cine-top">
        <div class="cine-id">
          <b id="cine-handle">—</b>
          <span id="cine-meta"></span>
        </div>
        <button type="button" class="cine-btn cine-btn--icon" id="cine-play" aria-label="Pause" title="Pause (Space)">
          <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" id="cine-play-icon">
            <path d="M8 5v14M16 5v14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>
          </svg>
        </button>
        <button type="button" class="cine-btn cine-btn--speed" id="cine-speed" title="Playback speed">1×</button>
        <button type="button" class="cine-btn" id="cine-skip">Skip to explore</button>
        <button type="button" class="cine-btn cine-btn--icon" id="cine-close" aria-label="Close" title="Close (Esc)">
          <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>
          </svg>
        </button>
      </div>

      <div class="cine-strip" id="cine-strip">
        <div class="cine-track" id="cine-track"></div>
      </div>

      <div class="cine-atlas" id="cine-atlas"></div>

      <div class="cine-foot">
        <div class="cine-chapter" id="cine-chapter"></div>
        <div class="cine-keys" id="cine-keys"></div>
      </div>`;

    $("#cine-close", cineEl).addEventListener("click", () => cineClose());
    $("#cine-play", cineEl).addEventListener("click", () => film() && film().togglePlay());
    $("#cine-speed", cineEl).addEventListener("click", () => film() && film().cycleSpeed());
    $("#cine-skip", cineEl).addEventListener("click", () => {
      if (cineEl.dataset.mode === "atlas") replay(0);
      else showAtlas();
    });
    $("#cine-ticks", cineEl).addEventListener("click", (e) => {
      const t = e.target.closest("[data-tick]");
      if (t && film()) film().goPanel(Number(t.dataset.tick));
    });

    cineEl.addEventListener("click", (e) => {
      const act = e.target.closest("[data-act]");
      if (!act) return;
      const kind = act.dataset.act;
      if (kind === "practice") {
        cineClose({ silent: true });
        if (window.__insightsGoPractice) window.__insightsGoPractice(act.dataset.arg || "mistakes");
      } else if (kind === "story") {
        cineClose({ silent: true });
        if (window.__insightsOpenStory) window.__insightsOpenStory(act.dataset.arg || "verdict");
      } else if (kind === "deep") {
        cineClose({ silent: true });
        if (window.__insightsOpenDeepDive) window.__insightsOpenDeepDive();
      } else if (kind === "review") {
        cineClose({ silent: true });
        if (window.__insightsGoReview) window.__insightsGoReview(act.dataset.game, act.dataset.ply);
      } else if (kind === "position") {
        const item = safeParse(act.dataset.item);
        if (item && window.__insightsShowPosition) window.__insightsShowPosition(item);
      } else if (kind === "replay") {
        replay(Number(act.dataset.arg || 0));
      } else if (kind === "atlas") {
        showAtlas();
      }
    });

    window.addEventListener("resize", () => {
      if (!cineEl.classList.contains("hidden") && film()) film().resize();
    });
  }

  function setTransport(mode) {
    cineEl.dataset.mode = mode;
    const skip = $("#cine-skip", cineEl);
    const chap = $("#cine-chapter", cineEl);
    const keys = $("#cine-keys", cineEl);
    if (skip) skip.textContent = mode === "atlas" ? "Replay the film" : "Skip to explore";
    if (chap && mode === "atlas") {
      chap.innerHTML = "<b>Explore</b> · everything the film covered, and the tables underneath";
    }
    if (keys) {
      keys.innerHTML = mode === "atlas"
        ? "<span><kbd>Esc</kbd> close</span>"
        : "<span><kbd>scroll</kbd> anywhere</span>"
          + "<span><kbd>Space</kbd> pause</span>"
          + "<span><kbd>←</kbd> <kbd>→</kbd> chapter</span>"
          + "<span><kbd>Esc</kbd> explore</span>";
    }
  }

  function replay(at) {
    setTransport("play");
    if (film()) {
      film().setMode("play");
      film().goPanel(at || 0, { keepPlaying: true });
    }
  }

  // ══════════════════════════════════════════════════════════════════════
  // ATLAS
  // ══════════════════════════════════════════════════════════════════════

  function showAtlas() {
    setTransport("atlas");
    if (film()) film().setMode("atlas");
    destroyGrounds();
    renderAtlas();
    const atlas = $("#cine-atlas", cineEl);
    if (atlas) atlas.scrollTop = 0;
  }

  function renderAtlas() {
    const m = cine.metrics || {};
    const h = (m.pro && m.pro.headline) || {};
    const meta = cine.meta || {};
    const items = ((m.practice_flags || {}).items || []).filter((it) => it.fen);
    const elo = h.elo_left_on_board || {};
    const handle = meta.handle || meta.chesscom_handle || "your report";
    const chapters = film() ? film().panels() : [];
    const atlas = $("#cine-atlas", cineEl);

    atlas.innerHTML = `
      <div class="atlas-inner">
        <header class="atlas-hero">
          <p class="pnl-kicker">${esc(handle)} · ${esc(meta.window_days || 30)} days · ${esc(meta.time_class || "blitz")}</p>
          <h2>Everything, in one place</h2>
          <p>
            The film is the summary. This is the whole report: the positions worth
            drilling, the chapters you can replay, and every table the numbers came from.
            ${isNum(elo.points) && elo.points > 0
              ? `There are about <b>${Math.round(elo.points)} rating points</b> sitting in the moves below.` : ""}
          </p>
          <div class="atlas-cta">
            ${items.length ? `<button type="button" class="cine-btn cine-btn--go" data-act="practice" data-arg="mistakes">Drill these ${items.length} positions →</button>` : ""}
            <button type="button" class="cine-btn" data-act="story" data-arg="why">Read why you lose</button>
            <button type="button" class="cine-btn" data-act="story" data-arg="how">How to fix it</button>
            <button type="button" class="cine-btn" data-act="deep">Deep dive tables</button>
            <button type="button" class="cine-btn" data-act="replay" data-arg="0">Replay the film</button>
          </div>
        </header>

        <section class="atlas-sec">
          <div class="atlas-sec-head">
            <h3>Your practice set</h3>
            <p>${items.length
              ? "Costly, findable misses from your own games — click any board to inspect it."
              : "Nothing crossed the bar this window."}</p>
          </div>
          ${items.length ? `<div class="arena" id="cine-arena">
            ${items.slice(0, 40).map((it, i) => arenaCard(it, i)).join("")}
          </div>` : `<div class="atlas-empty">No position in this window was both costly enough and findable enough to be worth drilling. That is a good sign.</div>`}
        </section>

        <section class="atlas-sec">
          <div class="atlas-sec-head">
            <h3>Replay a chapter</h3>
            <p>Jump straight back to any part of the film.</p>
          </div>
          <div class="chapters">
            ${chapters.map((s, i) => `
              <button type="button" class="chapter" data-act="replay" data-arg="${i}">
                <span class="n">${String(i + 1).padStart(2, "0")}</span>
                <span class="t">${esc(s.chapter)}</span>
              </button>`).join("")}
          </div>
        </section>

        <section class="atlas-sec">
          <div class="atlas-sec-head">
            <h3>Go deeper</h3>
            <p>The written report and the full seven-section dashboard.</p>
          </div>
          <div class="chapters">
            <button type="button" class="chapter" data-act="story" data-arg="verdict">
              <span class="n">STORY</span><span class="t">The verdict</span></button>
            <button type="button" class="chapter" data-act="story" data-arg="why">
              <span class="n">STORY</span><span class="t">Why you lose</span></button>
            <button type="button" class="chapter" data-act="story" data-arg="how">
              <span class="n">STORY</span><span class="t">How to fix it</span></button>
            <button type="button" class="chapter" data-act="deep">
              <span class="n">TABLES</span><span class="t">Deep dive dashboard</span></button>
            <button type="button" class="chapter" data-act="practice" data-arg="mistakes">
              <span class="n">TRAIN</span><span class="t">Your Mistakes</span></button>
            <button type="button" class="chapter" data-act="practice" data-arg="defense">
              <span class="n">TRAIN</span><span class="t">Defense Gym</span></button>
          </div>
        </section>
      </div>`;

    mountArena();
  }

  function arenaCard(it, i) {
    const turn = String(it.fen || "").split(" ")[1] === "b" ? "Black to move" : "White to move";
    const moveNo = isNum(it.ply) ? Math.ceil(Number(it.ply) / 2) : null;
    const payload = esc(JSON.stringify({
      fen: it.fen, san: it.san, ply: it.ply, delta_w: it.delta_w,
      findability: it.findability, volatility: it.volatility,
      best_uci: it.best_uci, move_uci: it.move_uci,
      game_id: it.game_id, user_color: it.user_color,
    }));
    return `
      <button type="button" class="arena-card" data-act="position" data-item="${payload}">
        <div class="arena-board" data-fen="${esc(it.fen)}"
             data-color="${esc(it.user_color || "white")}"
             data-best="${esc(it.best_uci || "")}"></div>
        <div class="arena-meta">
          <span class="arena-cost">−${Number(it.delta_w || 0).toFixed(0)} win%</span>
          <span class="arena-turn">${turn}</span>
        </div>
        <div class="arena-meta">
          <span class="arena-line">${moveNo ? `Move <b>${moveNo}</b>` : `#${i + 1}`}${it.san ? ` · played <b>${esc(it.san)}</b>` : ""}</span>
          ${isNum(it.findability) ? `<span class="arena-find">${Math.round(it.findability)} findable</span>` : ""}
        </div>
        ${it.opponent ? `<div class="arena-line">vs ${esc(it.opponent)}</div>` : ""}
      </button>`;
  }

  /** Forty chessgrounds at once is a visible stall; mount them on approach. */
  function mountArena() {
    if (cine.arenaObserver) { cine.arenaObserver.disconnect(); cine.arenaObserver = null; }
    const nodes = $$(".arena-board[data-fen]", cineEl);
    if (!nodes.length || !window.Chessground) return;
    const atlas = $("#cine-atlas", cineEl);
    cine.arenaObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        mountBoard(entry.target);
        cine.arenaObserver.unobserve(entry.target);
      });
    }, { root: atlas, rootMargin: "240px", threshold: 0.01 });
    nodes.forEach((el) => cine.arenaObserver.observe(el));
  }

  // ══════════════════════════════════════════════════════════════════════
  // Open / close / route
  // ══════════════════════════════════════════════════════════════════════

  function cineAdopt(metrics, meta) {
    cine.metrics = metrics || null;
    cine.meta = meta || {};
  }

  function cinePath() {
    const id = (cine.meta && (cine.meta.run_id || cine.meta.runId))
      || (window.__insightsActiveRunId && window.__insightsActiveRunId()) || "";
    return id ? `/insights/${id}/cinema` : "/insights";
  }

  function parseCinePath(pathname) {
    const path = String(pathname || "").replace(/\/+$/, "") || "/";
    const m = path.match(/^\/insights\/([^/]+)\/cinema$/);
    return m ? { runId: m[1] } : null;
  }

  function cineRoute(pathname) {
    const parsed = parseCinePath(pathname);
    if (!parsed) {
      if (!cineEl.classList.contains("hidden")) cineClose({ silent: true });
      return false;
    }
    if (cineEl.classList.contains("hidden")) {
      if (window.__insightsPlayReport) window.__insightsPlayReport({ push: false });
      else return false;
    }
    return true;
  }

  function cineOpen(metrics, meta, opts) {
    if (!cine.built) { cineBuild(); cine.built = true; }
    if (metrics) cineAdopt(metrics, meta);
    if (!cine.metrics) return;

    const m = cine.meta || {};
    $("#cine-handle", cineEl).textContent = m.handle || m.chesscom_handle || "Insights";
    $("#cine-meta", cineEl).textContent = [
      m.source === "lichess" ? "Lichess" : "Chess.com",
      `${m.window_days || 30}d`,
      m.time_class || "",
      `${((cine.metrics.game_explorer || []).length) || m.games_analyzed || 0} games`,
    ].filter(Boolean).join(" · ");

    if (window.__postmortemClose) window.__postmortemClose({ silent: true });
    if (window.__insightsCloseDeepDive) window.__insightsCloseDeepDive();
    forgeClose();

    root.dataset.anim = reduced() ? "off" : "on";
    cineEl.classList.remove("hidden");
    setTransport("play");
    document.body.style.overflow = "hidden";

    const path = cinePath();
    if ((!opts || opts.push !== false) && window.location.pathname !== path) {
      if (window.__shellNavigate) window.__shellNavigate(path);
      else history.pushState({ chessmax: true, path }, "", path);
    }

    // The strip measures itself, so it must be laid out before it starts.
    void cineEl.offsetWidth;
    const ok = film() && film().start(cine.metrics, cine.meta, opts);
    if (!ok) showAtlas();
  }

  function cineClose(opts) {
    if (film()) film().teardown();
    if (cine.arenaObserver) { cine.arenaObserver.disconnect(); cine.arenaObserver = null; }
    destroyGrounds();
    cineEl.classList.add("hidden");
    if (forgeEl.classList.contains("hidden")) document.body.style.overflow = "";
    if (!(opts && opts.silent) && parseCinePath(window.location.pathname) && window.__shellNavigate) {
      window.__shellNavigate("/insights");
    }
  }

  // ── Keyboard ────────────────────────────────────────────────────────────

  document.addEventListener("keydown", (e) => {
    if (cineEl.classList.contains("hidden")) return;
    if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    const atlas = cineEl.dataset.mode === "atlas";
    if (e.key === "Escape") {
      e.preventDefault();
      if (atlas) cineClose(); else showAtlas();
      return;
    }
    if (atlas || !film()) return;
    if (e.key === " " || e.code === "Space") { e.preventDefault(); film().togglePlay(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); film().step(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); film().step(-1); }
    else if (e.key === "Home") { e.preventDefault(); film().goPanel(0); }
    else if (e.key === "End") { e.preventDefault(); showAtlas(); }
  });

  // A backgrounded tab runs no rAF callbacks, so the strip would leap forward
  // the moment it came back. Hold instead.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && !cineEl.classList.contains("hidden") && film() && film().isPlaying()) {
      film().togglePlay();
    }
  });

  // ── Public API ──────────────────────────────────────────────────────────

  window.__insightsForge = {
    open: forgeOpen,
    update: forgeUpdate,
    ready: forgeReady,
    fail: forgeFail,
    close: forgeClose,
    isOpen: () => !forgeEl.classList.contains("hidden"),
  };

  window.__insightsCinema = {
    open: cineOpen,
    close: cineClose,
    adopt: cineAdopt,
    route: cineRoute,
    isOpen: () => !cineEl.classList.contains("hidden"),
  };
})();
