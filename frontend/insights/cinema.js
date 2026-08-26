/* Insights Cinema — the generation forge and the auto-playing report.
 *
 * Two surfaces, one file, because they are two halves of one arc: the Forge is
 * what a run looks like while it computes, the Cinema is what it looks like
 * when it is done. Both read the same payloads the launcher already loads.
 *
 *   window.__insightsForge  = { open, update, fail, close, isOpen }
 *   window.__insightsCinema = { open, close, adopt, isOpen }
 *
 * Nothing here computes a metric. Every number on screen comes from
 * `metrics.pro`, `metrics.narrative` or `metrics.game_explorer`, which is the
 * same contract the dashboard and the post-mortem read — so the movie can
 * never disagree with the tables behind it.
 *
 * The scene list is data, not markup: `buildScenes()` returns only the scenes
 * whose inputs actually exist, so a 6-game run plays a short film rather than
 * a long one full of em-dashes.
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
  // CINEMA — the player
  // ══════════════════════════════════════════════════════════════════════

  const cine = {
    metrics: null,
    meta: {},
    scenes: [],
    idx: 0,
    playing: true,
    elapsed: 0,
    last: 0,
    raf: null,
    grounds: [],
    arenaObserver: null,
    mode: "play",
  };

  function cineBuild() {
    cineEl.innerHTML = `
      <div class="cine-bg"></div>
      <div class="cine-vignette"></div>
      <div class="cine-grain"></div>

      <div class="cine-progress" id="cine-progress" role="tablist" aria-label="Chapters"></div>

      <div class="cine-top">
        <div class="cine-id">
          <b id="cine-handle">—</b>
          <span id="cine-meta"></span>
        </div>
        <button type="button" class="cine-btn cine-btn--icon" id="cine-play"
                aria-label="Pause" title="Pause (Space)">
          <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" id="cine-play-icon">
            <path d="M8 5v14M16 5v14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>
          </svg>
        </button>
        <button type="button" class="cine-btn" id="cine-skip">Skip to explore</button>
        <button type="button" class="cine-btn cine-btn--icon" id="cine-close"
                aria-label="Close" title="Close (Esc)">
          <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>
          </svg>
        </button>
      </div>

      <div class="cine-stage" id="cine-stage">
        <button type="button" class="cine-zone cine-zone--prev" id="cine-prev" aria-label="Previous chapter"></button>
        <button type="button" class="cine-zone cine-zone--next" id="cine-next" aria-label="Next chapter"></button>
        <div class="cine-slide" id="cine-slide"></div>
      </div>

      <div class="cine-atlas" id="cine-atlas"></div>

      <div class="cine-foot">
        <div class="cine-chapter" id="cine-chapter"></div>
        <div class="cine-keys">
          <span><kbd>Space</kbd> pause</span>
          <span><kbd>←</kbd> <kbd>→</kbd> chapter</span>
          <span><kbd>Esc</kbd> exit</span>
        </div>
      </div>`;

    $("#cine-close", cineEl).addEventListener("click", () => cineClose());
    $("#cine-play", cineEl).addEventListener("click", togglePlay);
    $("#cine-skip", cineEl).addEventListener("click", () => {
      if (cine.mode === "atlas") replay(0);
      else showAtlas();
    });
    $("#cine-prev", cineEl).addEventListener("click", () => go(cine.idx - 1));
    $("#cine-next", cineEl).addEventListener("click", () => go(cine.idx + 1));

    $("#cine-progress", cineEl).addEventListener("click", (e) => {
      const seg = e.target.closest("[data-seg]");
      if (seg) go(Number(seg.dataset.seg));
    });

    // Delegated actions used by scenes and the atlas alike.
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
  }

  function safeParse(s) {
    try { return JSON.parse(s); } catch { return null; }
  }

  // ── Transport ───────────────────────────────────────────────────────────

  function togglePlay() {
    setPlaying(!cine.playing);
  }

  function setPlaying(on) {
    cine.playing = on;
    const icon = $("#cine-play-icon", cineEl);
    const btn = $("#cine-play", cineEl);
    if (!icon || !btn) return;
    icon.innerHTML = on
      ? `<path d="M8 5v14M16 5v14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>`
      : `<path d="M8 5l12 7-12 7z" fill="currentColor"/>`;
    btn.setAttribute("aria-label", on ? "Pause" : "Play");
    btn.title = on ? "Pause (Space)" : "Play (Space)";
  }

  function tick(now) {
    cine.raf = requestAnimationFrame(tick);
    if (cine.mode !== "play") return;
    const dt = now - (cine.last || now);
    cine.last = now;
    if (!cine.playing) return;
    const scene = cine.scenes[cine.idx];
    if (!scene) return;
    cine.elapsed += dt;
    const f = clamp(cine.elapsed / scene.dur, 0, 1);
    const seg = $(`[data-seg="${cine.idx}"]`, cineEl);
    if (seg) seg.style.setProperty("--fill", f.toFixed(4));
    if (f >= 1) go(cine.idx + 1);
  }

  function startLoop() {
    stopLoop();
    cine.last = 0;
    cine.raf = requestAnimationFrame((t) => { cine.last = t; tick(t); });
  }

  function stopLoop() {
    if (cine.raf) cancelAnimationFrame(cine.raf);
    cine.raf = null;
  }

  function go(i, opts) {
    if (i < 0) i = 0;
    if (i >= cine.scenes.length) { showAtlas(); return; }
    cine.idx = i;
    cine.elapsed = 0;
    cine.last = 0;
    if (opts && opts.play) setPlaying(true);
    paintSegments();
    mountScene(cine.scenes[i]);
  }

  /** Top-bar and footer copy differ between the film and the Atlas. */
  function setTransport(mode) {
    const skip = $("#cine-skip", cineEl);
    const chap = $("#cine-chapter", cineEl);
    const keys = $(".cine-keys", cineEl);
    if (skip) skip.textContent = mode === "atlas" ? "Replay the film" : "Skip to explore";
    if (chap && mode === "atlas") {
      chap.innerHTML = "<b>Explore</b> · everything the film covered, and the tables underneath";
    }
    if (keys) {
      keys.innerHTML = mode === "atlas"
        ? "<span><kbd>Esc</kbd> close</span>"
        : "<span><kbd>Space</kbd> pause</span>"
          + "<span><kbd>←</kbd> <kbd>→</kbd> chapter</span>"
          + "<span><kbd>Esc</kbd> explore</span>";
    }
  }

  function paintSegments() {
    const bar = $("#cine-progress", cineEl);
    if (!bar) return;
    if (bar.children.length !== cine.scenes.length) {
      bar.innerHTML = cine.scenes
        .map((s, i) => `<button type="button" class="cine-seg" data-seg="${i}" title="${esc(s.chapter)}"></button>`)
        .join("");
    }
    $$(".cine-seg", bar).forEach((seg, i) => {
      seg.classList.toggle("is-live", i === cine.idx);
      seg.style.setProperty("--fill", i < cine.idx ? "1" : i === cine.idx ? "0" : "0");
    });
    const chap = $("#cine-chapter", cineEl);
    if (chap) {
      const s = cine.scenes[cine.idx];
      chap.innerHTML = `<b>${cine.idx + 1}/${cine.scenes.length}</b> · ${esc(s ? s.chapter : "")}`;
    }
  }

  function mountScene(scene) {
    const slide = $("#cine-slide", cineEl);
    destroyGrounds();
    slide.innerHTML = "";
    const el = scene.build();
    if (!el) return;
    el.classList.add("scn", "is-entering");
    slide.appendChild(el);
    // Force layout so the "from" state of every transition is committed before
    // the enter hook flips it — otherwise the browser coalesces both into one
    // computed value and nothing animates.
    void el.offsetWidth;
    if (scene.enter) raf2(() => scene.enter(el));
  }

  // ── Board mounting ──────────────────────────────────────────────────────

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
    if (played.length >= 4) {
      shapes.push({ orig: played.slice(0, 2), dest: played.slice(2, 4), brush: "red" });
    }
    if (best.length >= 4) {
      shapes.push({ orig: best.slice(0, 2), dest: best.slice(2, 4), brush: "green" });
    }
    const ground = window.Chessground(wrap, {
      fen: el.dataset.fen,
      orientation: el.dataset.color === "black" ? "black" : "white",
      viewOnly: true,
      coordinates: false,
      drawable: { enabled: false, visible: true, autoShapes: shapes },
    });
    el._cgMounted = true;
    requestAnimationFrame(() => {
      try { ground.redrawAll && ground.redrawAll(); } catch { /* ignore */ }
    });
    cine.grounds.push(ground);
    return ground;
  }

  // ══════════════════════════════════════════════════════════════════════
  // Scene ingredients
  // ══════════════════════════════════════════════════════════════════════

  const pro = () => (cine.metrics && cine.metrics.pro) || {};
  const narr = () => (cine.metrics && cine.metrics.narrative) || {};
  const facts = () => (cine.metrics && cine.metrics.game_explorer) || [];
  const headline = () => pro().headline || {};

  function head(kicker, title, sub) {
    return `
      <div class="scn-head">
        ${kicker ? `<p class="scn-kicker rise" style="--i:0">${esc(kicker)}</p>` : ""}
        <h2 class="scn-title rise" style="--i:1">${title}</h2>
        ${sub ? `<p class="scn-sub rise" style="--i:2">${sub}</p>` : ""}
      </div>`;
  }

  function tile(k, v, s, cls) {
    return `
      <div class="tile pop ${cls || ""}">
        <span class="k">${esc(k)}</span>
        <span class="v" data-count="${isNum(v) ? v : ""}">${isNum(v) ? "0" : esc(v)}</span>
        ${s ? `<span class="s">${esc(s)}</span>` : ""}
      </div>`;
  }

  /** Run every `[data-count]` in a subtree, honouring an optional formatter. */
  function runCounters(el, fmt) {
    $$("[data-count]", el).forEach((n, i) => {
      const raw = n.dataset.count;
      if (raw === "") return;
      countUp(n, Number(raw), {
        delay: 180 + i * 70,
        fmt: fmt || ((v) => String(Math.round(v))),
      });
    });
  }

  function rowHtml(name, sub, val, fill, cls) {
    return `
      <div class="row ${cls || ""} rise" data-fill="${clamp(fill || 0, 0, 1).toFixed(3)}">
        <div class="row-main">
          <div class="row-name">${name}</div>
          ${sub ? `<div class="row-sub">${sub}</div>` : ""}
        </div>
        <div class="row-val">${val}</div>
      </div>`;
  }

  function fillRows(el) {
    $$(".row[data-fill]", el).forEach((r, i) => {
      r.style.setProperty("--i", String(i + 3));
      setTimeout(() => r.style.setProperty("--fill", r.dataset.fill), 260 + i * 90);
    });
  }

  function gaugeHtml(value, label, sub, colour, opts) {
    const floor = (opts && isNum(opts.floor)) ? Number(opts.floor) : 0;
    const v = isNum(value)
      ? clamp((Number(value) - floor) / Math.max(1, 100 - floor), 0, 1)
      : 0;
    const r = 46;
    const c = 2 * Math.PI * r;
    return `
      <div class="gauge pop">
        <div class="gauge-ring" style="--g:${colour || "var(--cine-accent)"}">
          <svg viewBox="0 0 110 110" aria-hidden="true">
            <circle class="t" cx="55" cy="55" r="${r}"></circle>
            <circle class="a" cx="55" cy="55" r="${r}"
                    stroke-dasharray="${c.toFixed(1)}"
                    stroke-dashoffset="${c.toFixed(1)}"
                    data-goal="${(c * (1 - v)).toFixed(1)}"></circle>
          </svg>
          <div class="gauge-num" data-count="${isNum(value) ? value : ""}">${isNum(value) ? "0" : "—"}</div>
        </div>
        <div class="gauge-lab">${esc(label)}</div>
        ${sub ? `<div class="gauge-sub">${esc(sub)}</div>` : ""}
      </div>`;
  }

  function runGauges(el) {
    $$(".gauge-ring .a[data-goal]", el).forEach((a, i) => {
      setTimeout(() => { a.style.strokeDashoffset = a.dataset.goal; }, 200 + i * 130);
    });
  }

  /**
   * A line chart over `series` (arrays of {x, y} in data space).
   * Returns the SVG markup; call `runChart` in the enter hook to draw it.
   */
  function chartHtml(series, opts) {
    const o = opts || {};
    const W = 1000;
    const H = 420;
    const pad = { l: o.padLeft || 44, r: 22, t: 18, b: 30 };
    const all = series.flatMap((s) => s.points);
    if (all.length < 2) return "";
    const xs = all.map((p) => p.x);
    const ys = all.map((p) => p.y);
    const x0 = Math.min(...xs);
    const x1 = Math.max(...xs);
    let y0 = isNum(o.yMin) ? o.yMin : Math.min(...ys);
    let y1 = isNum(o.yMax) ? o.yMax : Math.max(...ys);
    if (y1 - y0 < 1e-6) { y1 = y0 + 1; }
    const padY = (y1 - y0) * 0.12;
    if (!isNum(o.yMin)) y0 -= padY;
    if (!isNum(o.yMax)) y1 += padY;

    const sx = (x) => pad.l + ((x - x0) / Math.max(1e-9, x1 - x0)) * (W - pad.l - pad.r);
    const sy = (y) => pad.t + (1 - (y - y0) / Math.max(1e-9, y1 - y0)) * (H - pad.t - pad.b);

    const paths = series.map((s) => {
      const d = s.points
        .map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)} ${sy(p.y).toFixed(1)}`)
        .join(" ");
      const cls = ["plot", s.cls || "", s.draw === false ? "" : "draw"].filter(Boolean).join(" ");
      return `<path class="${cls}" d="${d}" data-delay="${s.delay || 0}"></path>`;
    }).join("");

    const marks = (o.markers || []).map((m) => {
      const px = sx(m.x);
      const py = sy(m.y);
      return `
        <line class="marker-line" x1="${px.toFixed(1)}" y1="${pad.t}" x2="${px.toFixed(1)}" y2="${(H - pad.b).toFixed(1)}"></line>
        <circle class="dot ${m.bad ? "dot--bad" : ""} pop" cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="6"></circle>
        ${m.label ? `<text class="${m.bad ? "lab-bad" : ""}" x="${clamp(px, pad.l, W - pad.r - 130).toFixed(1)}" y="${clamp(py - 16, 16, H - pad.b - 6).toFixed(1)}">${esc(m.label)}</text>` : ""}`;
    }).join("");

    const guides = (o.guides || []).map((g) => {
      const py = sy(g.y);
      return `
        <line class="mid-line" x1="${pad.l}" y1="${py.toFixed(1)}" x2="${(W - pad.r).toFixed(1)}" y2="${py.toFixed(1)}"></line>
        ${g.label ? `<text x="4" y="${(py + 4).toFixed(1)}">${esc(g.label)}</text>` : ""}`;
    }).join("");

    const axis = (o.axisLabels || []).map((a) => {
      const px = clamp(sx(a.x), pad.l, W - pad.r - 40);
      return `<text x="${px.toFixed(1)}" y="${H - 8}">${esc(a.label)}</text>`;
    }).join("");

    return `
      <div class="chart pop">
        <svg viewBox="0 0 ${W} ${H}" aria-hidden="true">
          ${guides}${paths}${marks}${axis}
        </svg>
      </div>`;
  }

  function runChart(el) {
    $$(".chart .draw", el).forEach((p) => {
      drawPath(p, { dur: 1600, delay: Number(p.dataset.delay || 0) + 150 });
    });
  }

  // ══════════════════════════════════════════════════════════════════════
  // Scenes
  // ══════════════════════════════════════════════════════════════════════

  function buildScenes() {
    const m = cine.metrics || {};
    const h = headline();
    const p = pro();
    const nr = narr();
    const f = facts();
    const out = [];
    const add = (s) => { if (s) out.push(s); };

    add(sceneOpen(h, f));
    add(sceneRecord(h));
    add(sceneRating(h, f));
    add(sceneColors(f));
    add(sceneVerdict(nr));
    add(sceneQuality(h, p));
    add(sceneSpine(nr));
    add(sceneShapes(nr));
    add(sceneCliff(nr, f));
    add(sceneOpenings(p));
    add(sceneCritical(p));
    add(sceneTactics(m, nr));
    add(sceneHabits(nr));
    add(sceneStrengths(nr, p));
    add(sceneLeaks(p, h));
    add(sceneOutro(m, h));
    return out;
  }

  // 1 ── Title card
  function sceneOpen(h, f) {
    const meta = cine.meta || {};
    const games = (h.record && h.record.games) || f.length;
    if (!games) return null;
    const handle = meta.handle || meta.chesscom_handle || "You";
    const src = meta.source === "lichess" ? "Lichess" : "Chess.com";
    const days = meta.window_days || 30;
    const moves = (h.volume && h.volume.user_moves) || 0;
    return {
      id: "open",
      chapter: "Opening titles",
      dur: 6200,
      build: () => node(`
        <div class="scn" style="text-align:center; align-items:center">
          <p class="scn-kicker rise" style="--i:0">${esc(src)} · last ${esc(days)} days · ${esc(meta.time_class || "blitz")}</p>
          <h2 class="scn-title rise" style="--i:1; font-size:clamp(2.2rem,7vw,5rem)">${esc(handle)}</h2>
          <div class="rise" style="--i:2">
            <div class="big big--accent"><span data-count="${games}">0</span></div>
            <div class="big-cap">games, move by move</div>
          </div>
          <p class="scn-sub rise" style="--i:3; margin-inline:auto; text-align:center">
            ${moves ? `Every one of your <b>${moves.toLocaleString()}</b> moves went through the engine. ` : ""}
            Here is what they say.
          </p>
        </div>`),
      enter: (el) => runCounters(el),
    };
  }

  // 2 ── The record
  function sceneRecord(h) {
    const r = h.record || {};
    if (!r.games) return null;
    const dec = r.decided || r.games || 1;
    const w = (r.wins || 0) / dec;
    const d = (r.draws || 0) / dec;
    const l = (r.losses || 0) / dec;
    const perf = h.performance_rating;
    const exp = h.expectancy || {};
    return {
      id: "record",
      chapter: "The record",
      dur: 8200,
      build: () => node(`
        <div class="scn">
          ${head("The window", `You scored <em>${Math.round((r.score_pct || 0) * 100)}%</em>`,
            `${r.wins || 0} wins, ${r.draws || 0} draws, ${r.losses || 0} losses across ${r.games} games.`)}
          <div class="scn-body">
            <div class="segbar pop" style="--i:0">
              <i class="w" style="flex-grow:0.001" data-grow="${Math.max(w, 0.001)}">${r.wins ? r.wins : ""}</i>
              <i class="d" style="flex-grow:0.001" data-grow="${Math.max(d, 0.001)}">${r.draws ? r.draws : ""}</i>
              <i class="l" style="flex-grow:0.001" data-grow="${Math.max(l, 0.001)}">${r.losses ? r.losses : ""}</i>
            </div>
            <div class="segkey pop" style="--i:1; margin-top:.7rem">
              <span>Won <b>${Math.round(w * 100)}%</b></span>
              <span>Drew <b>${Math.round(d * 100)}%</b></span>
              <span>Lost <b>${Math.round(l * 100)}%</b></span>
            </div>
            <div class="tiles" style="margin-top:1.1rem">
              ${tile("Performance rating", perf, perf ? "what this run was worth" : "needs rated opponents")}
              ${tile("Expected score",
                isNum(exp.expected) && exp.n
                  ? `${Math.round((exp.expected / exp.n) * 100)}%` : "—",
                exp.n ? `predicted by the rating gaps over ${exp.n} rated games` : "needs rated opponents")}
              ${tile("Vs expectation", isNum(exp.delta) ? exp.delta : "—",
                exp.note ? String(exp.note) : (isNum(exp.delta) ? "points against the model" : ""),
                isNum(exp.delta) ? (exp.delta >= 0 ? "is-good" : "is-bad") : "")}
            </div>
          </div>
        </div>`),
      enter: (el) => {
        $$(".segbar > i[data-grow]", el).forEach((i) => {
          i.style.flexGrow = i.dataset.grow;
        });
        runCounters(el, (v) => (Math.abs(v) < 10 ? v.toFixed(1) : String(Math.round(v))));
      },
    };
  }

  // 3 ── Rating journey
  function sceneRating(h, f) {
    const r = h.rating || {};
    const chrono = f.slice().reverse().filter((g) => isNum(g.user_rating));
    if (chrono.length < 4 || !isNum(r.delta)) return null;
    const pts = chrono.map((g, i) => ({ x: i, y: Number(g.user_rating) }));
    const up = Number(r.delta) >= 0;
    const peakIdx = pts.reduce((b, p, i) => (p.y > pts[b].y ? i : b), 0);
    const floorIdx = pts.reduce((b, p, i) => (p.y < pts[b].y ? i : b), 0);
    return {
      id: "rating",
      chapter: "The rating line",
      dur: 8600,
      build: () => node(`
        <div class="scn">
          ${head("Where it went", up
            ? `Your rating climbed <em>${signed(r.delta)}</em>`
            : `Your rating fell <span class="bad">${signed(r.delta)}</span>`,
            `From ${r.start} to ${r.end} over ${chrono.length} rated games. Peak ${r.peak}, floor ${r.floor}.`)}
          <div class="scn-body">
            ${chartHtml([{ points: pts, cls: up ? "" : "plot--cool" }], {
              markers: [
                { x: peakIdx, y: pts[peakIdx].y, label: `peak ${pts[peakIdx].y}` },
                { x: floorIdx, y: pts[floorIdx].y, label: `floor ${pts[floorIdx].y}`, bad: true },
              ],
              axisLabels: [
                { x: 0, label: "first game" },
                { x: pts.length - 1, label: "latest" },
              ],
            })}
          </div>
        </div>`),
      enter: (el) => runChart(el),
    };
  }

  // 4 ── White vs Black
  function sceneColors(f) {
    if (f.length < 6) return null;
    const side = (c) => {
      const rows = f.filter((g) => g.user_color === c && isNum(g.points));
      if (!rows.length) return null;
      const score = rows.reduce((a, g) => a + Number(g.points), 0) / rows.length;
      const wins = rows.filter((g) => g.outcome === "win").length;
      const acc = rows.filter((g) => isNum(g.accuracy));
      return {
        n: rows.length,
        score,
        wins,
        accuracy: acc.length ? acc.reduce((a, g) => a + Number(g.accuracy), 0) / acc.length : null,
      };
    };
    const wht = side("white");
    const blk = side("black");
    if (!wht || !blk || wht.n < 3 || blk.n < 3) return null;
    const gap = (wht.score - blk.score) * 100;
    const worse = gap >= 0 ? "Black" : "White";
    const big = Math.abs(gap) >= 12;
    // Scale the columns against the better side so the gap is the visual, not
    // the absolute height — two 50% bars would read as "no difference".
    const top = Math.max(wht.score, blk.score, 0.01);
    return {
      id: "colors",
      chapter: "White and Black",
      dur: 8400,
      build: () => node(`
        <div class="scn">
          ${head("Colour", big
            ? `You are a different player as <em>${esc(worse === "Black" ? "White" : "Black")}</em>`
            : "Both colours look the same",
            big
              ? `${Math.round(Math.abs(gap))} points of score separate your two colours. That is not variance at this sample size — it is a repertoire problem on one side.`
              : `${Math.round(Math.abs(gap))} points separate them. Whatever is costing you games, it is not the colour you get.`)}
          <div class="scn-body">
            <div class="cols pop">
              <div class="col col--white">
                <div class="col-val" data-count="${Math.round(wht.score * 100)}">0</div>
                <div class="col-bar" data-h="${((wht.score / top) * 100).toFixed(1)}"></div>
                <div class="col-lab">White</div>
                <div class="col-sub">${wht.wins}W of ${wht.n}${isNum(wht.accuracy) ? ` · ${Math.round(wht.accuracy)}% accuracy` : ""}</div>
              </div>
              <div class="col col--black">
                <div class="col-val" data-count="${Math.round(blk.score * 100)}">0</div>
                <div class="col-bar" data-h="${((blk.score / top) * 100).toFixed(1)}"></div>
                <div class="col-lab">Black</div>
                <div class="col-sub">${blk.wins}W of ${blk.n}${isNum(blk.accuracy) ? ` · ${Math.round(blk.accuracy)}% accuracy` : ""}</div>
              </div>
            </div>
            <p class="scn-note pop" style="text-align:center; margin-top:.8rem">Score % — a win is 1, a draw is a half.</p>
          </div>
        </div>`),
      enter: (el) => {
        $$(".col-bar[data-h]", el).forEach((b) => { b.style.setProperty("--h", `${b.dataset.h}%`); });
        runCounters(el, (v) => `${Math.round(v)}%`);
      },
    };
  }

  // 5 ── The verdict
  function sceneVerdict(nr) {
    const v = nr.verdict || {};
    if (!v.headline) return null;
    const chips = (v.chips || []).slice(0, 4);
    return {
      id: "verdict",
      chapter: "The verdict",
      dur: 9500,
      build: () => node(`
        <div class="scn">
          ${head("The verdict", `<em>${esc(v.headline)}</em>`, v.diagnosis ? esc(v.diagnosis) : "")}
          ${chips.length ? `
            <div class="scn-body">
              <div class="rows">
                ${chips.map((c) => rowHtml(
                  esc(String(c.label || "")),
                  "",
                  esc(String(c.value ?? "—")),
                  0.5,
                  "is-bad",
                )).join("")}
              </div>
            </div>` : ""}
          ${(v.not_the_reason || []).length ? `
            <p class="scn-note rise" style="--i:5">
              Not the reason: ${(v.not_the_reason || []).slice(0, 2).map((s) => esc(String(s))).join(" · ")}
            </p>` : ""}
        </div>`),
      enter: (el) => fillRows(el),
    };
  }

  // 6 ── Move quality
  function sceneQuality(h, p) {
    const mq = p.move_quality || {};
    const err = h.error_rates || {};
    if (!mq.total_moves) return null;
    const acc = mq.mean_accuracy;
    const phases = (mq.by_phase || []).slice();
    const weakest = mq.weakest_phase;
    return {
      id: "quality",
      chapter: "Move quality",
      dur: 9200,
      build: () => node(`
        <div class="scn">
          ${head("Every move you played", isNum(acc)
            ? `<em>${Math.round(acc)}%</em> accuracy across ${mq.total_moves.toLocaleString()} moves`
            : `${mq.total_moves.toLocaleString()} moves measured`,
            weakest
              ? `Your ${esc(weakest)} is the phase that leaks — the average hides where the loss actually happens.`
              : "")}
          <div class="scn-body scn-split">
            <div class="gauges" style="grid-auto-flow:row; grid-template-columns:1fr">
              ${gaugeHtml(acc, "Mean accuracy",
                isNum(err.best_move_rate) ? `${Math.round(err.best_move_rate * 100)}% top-engine moves` : "",
                null, { floor: 50 })}
            </div>
            <div>
              <div class="tiles">
                ${tile("Blunders", err.blunders, isNum(err.blunders_per_100) ? `${err.blunders_per_100.toFixed(1)} per 100 moves` : "", "is-bad")}
                ${tile("Mistakes", err.mistakes, isNum(err.mistakes_per_100) ? `${err.mistakes_per_100.toFixed(1)} per 100` : "", "is-warn")}
                ${tile("Inaccuracies", err.inaccuracies, isNum(err.inaccuracies_per_100) ? `${err.inaccuracies_per_100.toFixed(1)} per 100` : "")}
                ${tile("Clean games", isNum(err.clean_game_rate) ? Math.round(err.clean_game_rate * 100) : "—", "no blunder at all", "is-good")}
              </div>
              ${phases.length ? `
                <div class="rows" style="margin-top:.8rem">
                  ${phases.map((ph) => rowHtml(
                    esc(titleCase(ph.phase)),
                    `${ph.moves} moves`,
                    `${Math.round(ph.accuracy)}%`,
                    clamp((100 - ph.accuracy) / 40, 0.04, 1),
                    ph.phase === weakest ? "is-bad" : "",
                  )).join("")}
                </div>` : ""}
            </div>
          </div>
        </div>`),
      enter: (el) => {
        runGauges(el);
        runCounters(el);
        fillRows(el);
      },
    };
  }

  // 7 ── The spine: mean win% curve of wins vs losses
  function sceneSpine(nr) {
    const sp = nr.spine || {};
    const wins = (sp.wins || []).filter(isNum);
    const losses = (sp.losses || []).filter(isNum);
    if (wins.length < 5 || losses.length < 5) return null;
    const toPts = (arr) => arr.map((y, i) => ({ x: i / (arr.length - 1), y: Number(y) }));
    // Where the two averages separate — not where the loss curve is steepest.
    // The steepest step is almost always the final ply of a resignation, which
    // marks the moment the game *ended*, not the moment it was decided.
    const last = losses.length - 1;
    const finalGap = Math.max(0.05, wins[last] - losses[last]);
    let breakAt = last;
    for (let i = 1; i < losses.length; i++) {
      if (wins[i] - losses[i] >= finalGap * 0.4) { breakAt = i; break; }
    }
    const phaseName = breakAt / losses.length < 0.34 ? "opening"
      : breakAt / losses.length < 0.72 ? "middlegame" : "endgame";
    const gapPct = Math.round((wins[breakAt] - losses[breakAt]) * 100);
    return {
      id: "spine",
      chapter: "The shape of a game",
      dur: 9000,
      build: () => node(`
        <div class="scn">
          ${head("Averaged over every game", `Your losses turn in the <em>${esc(phaseName)}</em>`,
            `Two average games: the ones you won, and the ones you lost. By this point the two are already ${gapPct} win% apart, and they never converge again.`)}
          <div class="scn-body">
            ${chartHtml([
              { points: toPts(wins), cls: "", delay: 0 },
              { points: toPts(losses), cls: "plot--cool", delay: 420 },
            ], {
              yMin: 0,
              yMax: 1,
              guides: [{ y: 0.5, label: "even" }],
              markers: [{
                x: breakAt / last,
                y: Number(losses[breakAt]),
                label: "they split here",
                bad: true,
              }],
              axisLabels: [
                { x: 0, label: "move 1" },
                { x: 1, label: "final move" },
              ],
            })}
            <div class="segkey pop" style="margin-top:.6rem; justify-content:center">
              <span style="color:var(--cine-accent)">— games you won</span>
              <span style="color:var(--cine-cool)">— games you lost</span>
            </div>
          </div>
        </div>`),
      enter: (el) => runChart(el),
    };
  }

  // 8 ── Loss shapes
  function sceneShapes(nr) {
    const shapes = ((nr.why_you_lose || {}).shapes || []).filter((s) => s.n > 0);
    if (shapes.length < 2) return null;
    const top = shapes[0];
    const max = Math.max(...shapes.map((s) => s.n));
    return {
      id: "shapes",
      chapter: "How you lose",
      dur: 9000,
      build: () => node(`
        <div class="scn">
          ${head("How the losses look", `Most often: <span class="bad">${esc(top.label)}</span>`,
            esc(top.caption || "Your losses are not one thing — but they are not evenly spread either."))}
          <div class="scn-body">
            <div class="rows">
              ${shapes.slice(0, 5).map((s, i) => rowHtml(
                esc(s.label),
                esc(s.caption || ""),
                `${s.n} <span style="opacity:.5;font-size:.72em">${Math.round((s.share || 0) * 100)}%</span>`,
                s.n / max,
                i === 0 ? "is-bad" : "",
              )).join("")}
            </div>
          </div>
        </div>`),
      enter: (el) => fillRows(el),
    };
  }

  // 9 ── The cliff: one real game, one real move
  function sceneCliff(nr, f) {
    const moments = (nr.why_you_lose || {}).moments || [];
    const moment = moments.find((mm) => mm.fen && (mm.sparkline || []).length >= 6) || moments.find((mm) => mm.fen);
    if (!moment) return null;
    const fact = f.find((g) => g.game_id === moment.game_id);
    const raw = (moment.sparkline || []).filter(isNum).map(Number);
    const curve = moment.user_color === "black" ? raw.map((v) => 1 - v) : raw;
    const hasCurve = curve.length >= 6;
    // The trajectory is 20 sampled points across the whole game; the miss is at
    // a ply. Place the marker proportionally rather than pretending to an
    // index the sparkline does not carry.
    const steepestNear = (from, radius) => {
      let at = from;
      let best = -Infinity;
      const lo = Math.max(1, from - radius);
      const hi = Math.min(curve.length - 1, from + radius);
      for (let i = lo; i <= hi; i++) {
        const d = curve[i - 1] - curve[i];
        if (d > best) { best = d; at = i; }
      }
      return at;
    };
    let markIdx = null;
    if (hasCurve && isNum(moment.ply) && fact && fact.ply_count) {
      const approx = clamp(
        Math.round((Number(moment.ply) / fact.ply_count) * (curve.length - 1)),
        0, curve.length - 1,
      );
      markIdx = steepestNear(approx, 2);
    } else if (hasCurve) {
      markIdx = steepestNear(Math.floor(curve.length / 2), curve.length);
    }
    const moveNo = isNum(moment.ply) ? Math.ceil(Number(moment.ply) / 2) : null;
    return {
      id: "cliff",
      chapter: "The moment",
      dur: 11000,
      build: () => node(`
        <div class="scn">
          ${head("One position", `You played <span class="bad">${esc(moment.san || "—")}</span>${moment.best_san ? `, not <em>${esc(moment.best_san)}</em>` : ""}`,
            esc(cliffCaption(moment)))}
          <div class="scn-body scn-split">
            <div class="fig pop">
              <div class="fig-board" data-fen="${esc(moment.fen)}"
                   data-color="${esc(moment.user_color || "white")}"
                   data-played="${esc(moment.played_uci || "")}"
                   data-best="${esc(moment.best_uci || "")}"></div>
              <div class="fig-moves">
                ${moment.san ? `<span class="chip chip--played"><i></i>${esc(moment.san)} played</span>` : ""}
                ${moment.best_san ? `<span class="chip chip--best"><i></i>${esc(moment.best_san)} wanted</span>` : ""}
              </div>
              <p class="fig-cap">${moveNo ? `Move ${moveNo}. ` : ""}${esc(titleCase(moment.outcome || ""))}${moment.opponent ? ` vs ${esc(moment.opponent)}` : ""}.</p>
            </div>
            <div>
              ${hasCurve ? chartHtml([{ points: curve.map((y, i) => ({ x: i, y })) }], {
                yMin: 0,
                yMax: 1,
                guides: [{ y: 0.5, label: "even" }],
                markers: markIdx !== null ? [{ x: markIdx, y: curve[markIdx], label: "here", bad: true }] : [],
                axisLabels: [{ x: 0, label: "move 1" }, { x: curve.length - 1, label: "final move" }],
              }) : ""}
              <p class="scn-note pop" style="margin-top:.7rem">
                Your winning chances across that whole game. This is one position out of
                ${(moments.length || 1)} flagged — the rest are waiting at the end.
              </p>
              <div class="atlas-cta" style="margin-top:.9rem">
                <button type="button" class="cine-btn cine-btn--go" data-act="review"
                        data-game="${esc(moment.game_id || "")}" data-ply="${esc(moment.ply || "")}">Open this game</button>
              </div>
            </div>
          </div>
        </div>`),
      enter: (el) => {
        mountBoard($(".fig-board", el));
        runChart(el);
      },
    };
  }

  /**
   * The narrative's auto caption is often just the move again. Only use it when
   * it says more than the headline already does; otherwise place the game.
   */
  function cliffCaption(m) {
    const caption = String(m.caption || "").trim();
    const restatesMove = caption && m.san
      && caption.replace(/[^a-z0-9+#=-]/gi, "").toLowerCase()
        === `youplayed${m.san}`.replace(/[^a-z0-9+#=-]/gi, "").toLowerCase();
    if (caption && !restatesMove) return caption;
    const bits = [];
    if (m.opponent) bits.push(`Against ${m.opponent}`);
    if (m.opening) bits.push(`in the ${shortOpening(m.opening, 40)}`);
    const where = bits.length ? `${bits.join(" ")}.` : "";
    const end = m.outcome === "loss" ? " You lost this one."
      : m.outcome === "win" ? " You won anyway — this is the one that nearly cost it."
      : "";
    return `${where}${end}`.trim() || "One move, and the position changed hands.";
  }

  // 10 ── Openings
  function sceneOpenings(p) {
    const op = p.openings || {};
    const rows = (op.rows || []).filter((r) => r.n >= (op.min_games || 3) && isNum(r.score_pct));
    if (rows.length < 2) return null;
    const sorted = rows.slice().sort((a, b) => b.score_pct - a.score_pct);
    const best = sorted[0];
    const worst = sorted[sorted.length - 1];
    const show = [...sorted.slice(0, 3), ...sorted.slice(-3)]
      .filter((r, i, arr) => arr.indexOf(r) === i)
      .slice(0, 6);
    return {
      id: "openings",
      chapter: "Openings",
      dur: 9200,
      build: () => node(`
        <div class="scn">
          ${head("The repertoire", `<em>${esc(shortOpening(best.opening, 22))}</em> works.<br><span class="bad">${esc(shortOpening(worst.opening, 22))}</span> does not.`,
            `${Math.round(best.score_pct * 100)}% from ${best.n} games as ${best.color} against ${Math.round(worst.score_pct * 100)}% from ${worst.n} as ${worst.color}. Openings with at least ${op.min_games || 3} games.`)}
          <div class="scn-body">
            <div class="rows">
              ${show.map((r) => rowHtml(
                `${esc(shortOpening(r.opening))} <span style="opacity:.45;font-size:.8em">as ${esc(r.color)}</span>`,
                `${r.n} games${isNum(r.mean_accuracy) ? ` · ${Math.round(r.mean_accuracy)}% accuracy` : ""}${isNum(r.mean_deviation_ply) ? ` · leaves book on move ${Math.ceil(r.mean_deviation_ply / 2)}` : ""}`,
                `${Math.round(r.score_pct * 100)}%`,
                r.score_pct,
                r.score_pct >= 0.55 ? "is-good" : r.score_pct <= 0.4 ? "is-bad" : "",
              )).join("")}
            </div>
          </div>
        </div>`),
      enter: (el) => fillRows(el),
    };
  }

  function shortOpening(name, cap) {
    const limit = cap || 34;
    let s = String(name || "").replace(/\s+/g, " ").trim();
    if (!s) return "Unknown opening";
    const prefixes = [
      "Queens Pawn Opening ", "Queen's Pawn Opening ", "Queens Pawn Game ",
      "Queen's Pawn Game ", "Kings Pawn Opening ", "King's Pawn Opening ",
      "Kings Pawn Game ", "King's Pawn Game ",
    ];
    for (const pre of prefixes) {
      if (s.startsWith(pre) && s.length > pre.length + 3) { s = s.slice(pre.length); break; }
    }
    if (s.length <= limit) return s;
    const cut = s.lastIndexOf(" ", limit);
    return `${(cut >= 12 ? s.slice(0, cut) : s.slice(0, limit)).replace(/[ ,:-]+$/, "")}…`;
  }

  // 11 ── Critical moments
  function sceneCritical(p) {
    const cm = p.critical_moments || {};
    const buckets = (cm.buckets || []).filter((b) => b.moves > 0 && isNum(b.accuracy));
    if (buckets.length < 2) return null;
    const crit = buckets.find((b) => b.key === "critical");
    const quiet = buckets.find((b) => b.key === "quiet");
    const gap = cm.criticality_gap;
    const bad = isNum(gap) && gap >= 8;
    const colourFor = (key) =>
      key === "critical" && bad ? "var(--cine-bad)"
        : key === "tense" ? "var(--cine-warn)"
        : "var(--cine-accent)";
    return {
      id: "critical",
      chapter: "Under pressure",
      dur: 9400,
      build: () => node(`
        <div class="scn">
          ${head("The moves that decide games", bad
            ? `Your accuracy drops <span class="bad">${Math.round(gap)} points</span> when it matters`
            : "You hold your level when the position sharpens",
            bad
              ? `Quiet moves are easy for everyone. These are the positions where several moves genuinely differ — and they are where your average is being spent.`
              : `The gap between your quiet moves and your sharpest ones is ${isNum(gap) ? Math.round(Math.abs(gap)) : "small"} points. That is a real strength.`)}
          <div class="scn-body">
            <div class="gauges">
              ${buckets.map((b) => gaugeHtml(
                b.accuracy,
                titleCase(b.key),
                `${b.moves} moves${isNum(b.mean_time) ? ` · ${Math.round(b.mean_time)}s each` : ""}`,
                colourFor(b.key),
                { floor: 50 },
              )).join("")}
            </div>
            ${cm.time_note ? `<p class="scn-note pop" style="text-align:center;margin-top:1rem">${esc(cm.time_note)}</p>`
              : isNum(cm.critical_conversion) ? `<p class="scn-note pop" style="text-align:center;margin-top:1rem">You handle ${Math.round(cm.critical_conversion * 100)}% of critical moments without a real loss.</p>` : ""}
          </div>
        </div>`),
      enter: (el) => { runGauges(el); runCounters(el, (v) => `${Math.round(v)}`); },
    };
  }

  // 12 ── Missed tactics
  function sceneTactics(m, nr) {
    const rows = ((m.missed_tactics || {}).rows || []).filter((r) => r.n > 0);
    const narrRows = ((nr.why_you_lose || {}).tactics || []).filter((t) => t.n > 0);
    const src = narrRows.length ? narrRows : rows.map((r) => ({
      id: r.tag,
      label: titleCase(r.tag),
      n: r.n,
      caption: `Missed ${r.n} times.`,
    }));
    if (!src.length) return null;
    const total = src.reduce((a, t) => a + Number(t.n || 0), 0);
    const top = src[0];
    return {
      id: "tactics",
      chapter: "Missed tactics",
      dur: 8600,
      build: () => node(`
        <div class="scn">
          ${head("What you walked past", `<span class="bad">${total}</span> tactics you could have played`,
            `The pattern you miss most is the <em>${esc(String(top.label || "").toLowerCase())}</em>. These are not engine-only lines — they are patterns a player at your level finds.`)}
          <div class="scn-body">
            <div class="tags">
              ${src.slice(0, 9).map((t, i) => `
                <span class="tag pop ${i === 0 ? "is-hot" : ""}" style="--i:${i}">
                  <b data-count="${t.n}">0</b><span>${esc(String(t.label || t.id))}</span>
                </span>`).join("")}
            </div>
          </div>
        </div>`),
      enter: (el) => runCounters(el),
    };
  }

  // 13 ── Habits
  function sceneHabits(nr) {
    const habits = ((nr.why_you_lose || {}).habits || []).slice(0, 4);
    if (habits.length < 2) return null;
    const max = Math.max(...habits.map((h) => Number(h.n) || 1), 1);
    return {
      id: "habits",
      chapter: "The habits",
      dur: 9000,
      build: () => node(`
        <div class="scn">
          ${head("The habits underneath", "Same mistakes, different games",
            "None of these is a single bad game. Each one showed up often enough to be a pattern.")}
          <div class="scn-body">
            <div class="rows">
              ${habits.map((h, i) => rowHtml(
                esc(h.title || ""),
                esc(h.caption || ""),
                isNum(h.n) ? `${h.n}` : "—",
                isNum(h.n) ? Number(h.n) / max : 0.35,
                i === 0 ? "is-warn" : "",
              )).join("")}
            </div>
          </div>
        </div>`),
      enter: (el) => fillRows(el),
    };
  }

  // 14 ── Strengths
  function sceneStrengths(nr, p) {
    const keep = ((nr.how_you_win || {}).strengths || []).filter((s) => s.title);
    const fallback = (p.strengths || []).filter((s) => s.title);
    const rows = (keep.length ? keep : fallback).slice(0, 4);
    if (!rows.length) return null;
    return {
      id: "strengths",
      chapter: "What works",
      dur: 8400,
      build: () => node(`
        <div class="scn">
          ${head("Keep doing this", "You are already good at <em>these</em>",
            "A report that only lists faults teaches you to play scared. These held up across the whole window.")}
          <div class="scn-body">
            <div class="rows">
              ${rows.map((s) => rowHtml(esc(s.title), esc(s.detail || ""), "✓", 0.62, "is-good")).join("")}
            </div>
          </div>
        </div>`),
      enter: (el) => fillRows(el),
    };
  }

  // 15 ── The leaks + rating on the table
  function sceneLeaks(p, h) {
    const leaks = (p.leaks || []).slice(0, 3);
    if (!leaks.length) return null;
    const elo = h.elo_left_on_board || {};
    const max = Math.max(...leaks.map((l) => Number(l.impact_win_pct_per_game) || 0), 1);
    return {
      id: "leaks",
      chapter: "The bill",
      dur: 10000,
      build: () => node(`
        <div class="scn">
          ${head("What it is costing you", isNum(elo.points) && elo.points > 0
            ? `<em><span data-count="${elo.points}">0</span></em> rating points, left on the board`
            : "Your three biggest leaks",
            isNum(elo.points) && elo.points > 0
              ? `Recover only the moves a player at your level would realistically have found, and this window scores like a ${n0(elo.points)}-point stronger player. Nothing here assumes engine vision.`
              : "Ranked by how much each one costs per game. They overlap, so they are ranked, never summed.")}
          <div class="scn-body">
            <div class="rows">
              ${leaks.map((l, i) => rowHtml(
                `<span style="opacity:.4">${i + 1}.</span> ${esc(l.title)}`,
                esc(l.detail || ""),
                `${Number(l.impact_win_pct_per_game).toFixed(1)}<span style="opacity:.5;font-size:.66em"> / game</span>`,
                Number(l.impact_win_pct_per_game) / max,
                l.severity === "high" ? "is-bad" : l.severity === "medium" ? "is-warn" : "",
              )).join("")}
            </div>
          </div>
        </div>`),
      enter: (el) => { fillRows(el); runCounters(el); },
    };
  }

  // 16 ── Outro
  function sceneOutro(m, h) {
    const practice = (m.practice_flags || {}).items || [];
    const fixes = ((narr().how_you_win || {}).fixes || []).slice(0, 3);
    return {
      id: "outro",
      chapter: "What to do about it",
      dur: 9000,
      build: () => node(`
        <div class="scn" style="text-align:center; align-items:center">
          <p class="scn-kicker rise" style="--i:0">The part that changes your rating</p>
          <h2 class="scn-title rise" style="--i:1">${practice.length
            ? `<em>${practice.length}</em> positions from your own games`
            : "Now go and fix it"}</h2>
          <p class="scn-sub rise" style="--i:2; margin-inline:auto">
            ${practice.length
              ? "Each one is a move you actually played, a move you could have found, and the difference between them. They are loaded and waiting below."
              : "The full report, every table behind these numbers, and your practice set are one click away."}
          </p>
          ${fixes.length ? `
            <div class="scn-body">
              <div class="rows" style="text-align:left; max-width:760px; margin:0 auto">
                ${fixes.map((fx, i) => rowHtml(
                  `<span style="opacity:.4">${i + 1}.</span> ${esc(fx.title)}`,
                  esc(fx.promise || fx.why || ""),
                  "→",
                  0.5,
                  "",
                )).join("")}
              </div>
            </div>` : ""}
          <div class="atlas-cta pop" style="justify-content:center">
            <button type="button" class="cine-btn cine-btn--go" data-act="atlas">Explore everything ↓</button>
          </div>
        </div>`),
      enter: (el) => fillRows(el),
    };
  }

  // ══════════════════════════════════════════════════════════════════════
  // ATLAS — where the movie lands
  // ══════════════════════════════════════════════════════════════════════

  function replay(at) {
    cine.mode = "play";
    cineEl.dataset.mode = "play";
    setTransport("play");
    go(at || 0, { play: true });
  }

  function showAtlas() {
    cine.mode = "atlas";
    cineEl.dataset.mode = "atlas";
    setPlaying(false);
    setTransport("atlas");
    destroyGrounds();
    $("#cine-slide", cineEl).innerHTML = "";
    renderAtlas();
    const atlas = $("#cine-atlas", cineEl);
    if (atlas) atlas.scrollTop = 0;
  }

  function renderAtlas() {
    const m = cine.metrics || {};
    const h = headline();
    const meta = cine.meta || {};
    const items = ((m.practice_flags || {}).items || []).filter((it) => it.fen);
    const elo = h.elo_left_on_board || {};
    const handle = meta.handle || meta.chesscom_handle || "your report";
    const atlas = $("#cine-atlas", cineEl);

    atlas.innerHTML = `
      <div class="atlas-inner">
        <header class="atlas-hero">
          <p class="scn-kicker">${esc(handle)} · ${esc(meta.window_days || 30)} days · ${esc(meta.time_class || "blitz")}</p>
          <h2>Everything, in one place</h2>
          <p>
            The film is the summary. This is the whole report: the positions worth
            drilling, the chapters you can replay, and every table the numbers came from.
            ${isNum(elo.points) && elo.points > 0
              ? `There are about <b>${n0(elo.points)} rating points</b> sitting in the moves below.` : ""}
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
            ${cine.scenes.map((s, i) => `
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
      fen: it.fen,
      san: it.san,
      ply: it.ply,
      delta_w: it.delta_w,
      findability: it.findability,
      volatility: it.volatility,
      best_uci: it.best_uci,
      move_uci: it.move_uci,
      game_id: it.game_id,
      user_color: it.user_color,
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

  /** Boards are lazy: forty chessgrounds at once is a visible stall. */
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
    }, { root: atlas, rootMargin: "220px", threshold: 0.01 });
    nodes.forEach((el) => cine.arenaObserver.observe(el));
  }

  // ══════════════════════════════════════════════════════════════════════
  // Open / close / adopt
  // ══════════════════════════════════════════════════════════════════════

  let built = false;

  // ── Routing ───────────────────────────────────────────────────────────
  //
  // The film gets a URL for the same reason the story screens do: Back should
  // leave it, and a link should land on it. The post-mortem's own parser
  // returns null for this path and closes itself silently, which is exactly
  // the hand-off we want.

  function cinePath() {
    const id = (cine.meta && (cine.meta.run_id || cine.meta.runId))
      || (window.__insightsActiveRunId && window.__insightsActiveRunId())
      || "";
    return id ? `/insights/${id}/cinema` : "/insights";
  }

  function parseCinePath(pathname) {
    const path = String(pathname || "").replace(/\/+$/, "") || "/";
    const m = path.match(/^\/insights\/([^/]+)\/cinema$/);
    return m ? { runId: m[1] } : null;
  }

  /** Called by the launcher on every insights route change. */
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


  function cineAdopt(metrics, meta) {
    cine.metrics = metrics || null;
    cine.meta = meta || {};
  }

  function cineOpen(metrics, meta, opts) {
    if (!built) { cineBuild(); built = true; }
    if (metrics) cineAdopt(metrics, meta);
    if (!cine.metrics) return;

    cine.scenes = buildScenes();
    if (!cine.scenes.length) return;

    const m = cine.meta || {};
    $("#cine-handle", cineEl).textContent = m.handle || m.chesscom_handle || "Insights";
    $("#cine-meta", cineEl).textContent = [
      m.source === "lichess" ? "Lichess" : "Chess.com",
      `${m.window_days || 30}d`,
      m.time_class || "",
      `${((cine.metrics.game_explorer || []).length) || m.games_analyzed || 0} games`,
    ].filter(Boolean).join(" · ");

    // Nothing else may own the screen at the same time.
    if (window.__postmortemClose) window.__postmortemClose({ silent: true });
    if (window.__insightsCloseDeepDive) window.__insightsCloseDeepDive();
    forgeClose();

    root.dataset.anim = reduced() ? "off" : "on";
    const path = cinePath();
    if ((!opts || opts.push !== false) && window.location.pathname !== path) {
      if (window.__shellNavigate) window.__shellNavigate(path);
      else history.pushState({ chessmax: true, path }, "", path);
    }
    cineEl.classList.remove("hidden");
    cineEl.dataset.mode = "play";
    cine.mode = "play";
    document.body.style.overflow = "hidden";
    setTransport("play");
    setPlaying(true);
    paintSegments();
    go((opts && opts.at) || 0);
    startLoop();
    cineEl.focus?.();
  }

  function cineClose(opts) {
    stopLoop();
    destroyGrounds();
    if (cine.arenaObserver) { cine.arenaObserver.disconnect(); cine.arenaObserver = null; }
    cineEl.classList.add("hidden");
    cine.mode = "play";
    if (forgeEl.classList.contains("hidden")) document.body.style.overflow = "";
    // Anything that hands off to another surface (the story, the dashboard, a
    // game review) closes silently — that surface owns the URL from there.
    if (
      !(opts && opts.silent)
      && parseCinePath(window.location.pathname)
      && window.__shellNavigate
    ) {
      window.__shellNavigate("/insights");
    }
  }

  // ── Keyboard ────────────────────────────────────────────────────────────

  document.addEventListener("keydown", (e) => {
    if (cineEl.classList.contains("hidden")) return;
    if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.key === "Escape") {
      e.preventDefault();
      if (cine.mode === "atlas") { cineClose(); return; }
      showAtlas();
      return;
    }
    if (cine.mode !== "play") return;
    if (e.key === " " || e.code === "Space") { e.preventDefault(); togglePlay(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); go(cine.idx + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); go(cine.idx - 1); }
  });

  // A backgrounded tab runs no rAF callbacks, so the film would silently jump
  // several chapters the moment it came back. Pause instead.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && !cineEl.classList.contains("hidden")) setPlaying(false);
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
