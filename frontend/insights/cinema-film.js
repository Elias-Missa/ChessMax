/* Insights Cinema — the film.
 *
 * Not a slide deck. Every panel lives side by side on one horizontal strip and
 * every animation on it is *scrubbed* by scroll position rather than fired on
 * entry: a panel builds itself as it approaches the centre of the frame and
 * unbuilds as it leaves, so the whole thing is one continuous move under the
 * wheel. Autoplay is just a constant velocity added to the same scroll target,
 * which is why taking the wheel mid-cruise blends instead of cutting.
 *
 * Scroll model (deliberately hand-rolled, not Lenis / ScrollTrigger):
 *   target  — where the strip wants to be, in px. Wheel and autoplay both add
 *             to this and nothing else.
 *   pos     — where it actually is; chases `target` with a frame-rate
 *             normalized lerp, which is the entire "smooth" feel.
 *   d(panel)— signed distance of a panel's centre from the frame's centre in
 *             viewport widths. Every visual on the strip is a function of it.
 *
 * A real scroller was tried first and rejected: autoplay has to blend with user
 * input rather than fight it, and ScrollTrigger's play/reverse semantics are
 * the slide-deck behaviour we are removing.
 *
 * Requires GSAP + SplitText + DrawSVGPlugin on `window` (vendored, see
 * index.html). Degrades to a plain, readable, non-animated strip without them.
 */
(function () {
  const api = window.__insightsCinemaInternals;
  if (!api) return;

  const {
    cineEl, root, $, $$, esc, isNum, clamp, node, titleCase, reduced,
    mountBoard, destroyGrounds, safeParse,
  } = api;

  const G = window.gsap || null;
  if (G && window.DrawSVGPlugin) G.registerPlugin(window.DrawSVGPlugin);

  // ── Feel ────────────────────────────────────────────────────────────────

  const FEEL = {
    // Seconds for one viewport width to pass at 1×. The old player dwelled
    // 8-11s per scene; this reads as motion rather than a timer.
    cruiseSecPerScreen: 4.4,
    lerp: 0.1,             // strip chase; higher = tighter, less float
    wheel: 1.15,           // wheel px → strip px
    skew: 0.055,           // skew per px/frame of velocity
    skewMax: 9,
    depthScale: 0.12,      // how much a panel shrinks per viewport off-centre
    depthRotate: 17,       // deg of Y-rotation at one viewport off-centre
    depthBlur: 4,          // px of blur at one viewport off-centre
    depthFade: 0.55,       // opacity lost per viewport off-centre
    // Panels must be *half* built when they are half a screen out, or the
    // midpoint of every transition is two invisible panels and a black frame.
    // This is the single number that decides whether the strip reads as
    // continuous motion or as a deck with a fade between slides.
    buildWindow: 0.95,
    // Idle time before autoplay picks back up after the wheel takes over. Long
    // enough to read the panel you stopped on; an explicit pause (Space, or the
    // button) is never overridden.
    resumeAfterMs: 5200,
  };

  const SPEEDS = [1, 1.6, 2.4];

  // ── State ───────────────────────────────────────────────────────────────

  const F = {
    metrics: null,
    meta: {},
    panels: [],        // { id, chapter, el, tl, width, setup, mounted }
    pos: 0,
    target: 0,
    max: 0,
    vel: 0,
    playing: true,
    speedIdx: 0,
    raf: null,
    observer: null,
    lastT: 0,
    idleTimer: null,
    fx: null,
    built: false,
    mode: "play",
  };

  // ── Data accessors ──────────────────────────────────────────────────────

  const pro = () => (F.metrics && F.metrics.pro) || {};
  const narr = () => (F.metrics && F.metrics.narrative) || {};
  const facts = () => (F.metrics && F.metrics.game_explorer) || [];
  const headline = () => pro().headline || {};

  // ══════════════════════════════════════════════════════════════════════
  // Markup helpers
  // ══════════════════════════════════════════════════════════════════════

  /** `data-par` layers drift against the scroll; the number is their depth. */
  function head(kicker, title, sub, opts) {
    const o = opts || {};
    return `
      <div class="pnl-head" data-par="${o.par || 0.06}">
        ${kicker ? `<p class="pnl-kicker" data-fx="kicker">${esc(kicker)}</p>` : ""}
        <h2 class="pnl-title" data-fx="title">${title}</h2>
        ${sub ? `<p class="pnl-sub" data-fx="sub">${sub}</p>` : ""}
      </div>`;
  }

  function tile(k, v, s, cls) {
    const numeric = isNum(v);
    return `
      <div class="tile" data-fx="tile">
        <span class="k">${esc(k)}</span>
        <span class="v ${cls || ""}" ${numeric ? `data-num="${v}"` : ""}>${numeric ? "0" : esc(v)}</span>
        ${s ? `<span class="s">${esc(s)}</span>` : ""}
      </div>`;
  }

  function rowHtml(name, sub, val, fill, cls) {
    return `
      <div class="row ${cls || ""}" data-fx="row" data-fill="${clamp(fill || 0, 0, 1).toFixed(3)}">
        <i class="row-fill"></i>
        <div class="row-main">
          <div class="row-name">${name}</div>
          ${sub ? `<div class="row-sub">${sub}</div>` : ""}
        </div>
        <div class="row-val">${val}</div>
      </div>`;
  }

  function gaugeHtml(value, label, sub, colour, opts) {
    const floor = (opts && isNum(opts.floor)) ? Number(opts.floor) : 0;
    const v = isNum(value) ? clamp((Number(value) - floor) / Math.max(1, 100 - floor), 0, 1) : 0;
    const r = 46;
    const c = 2 * Math.PI * r;
    return `
      <div class="gauge" data-fx="gauge">
        <div class="gauge-ring" style="--g:${colour || "var(--cine-accent)"}">
          <svg viewBox="0 0 110 110" aria-hidden="true">
            <circle class="t" cx="55" cy="55" r="${r}"></circle>
            <circle class="a" cx="55" cy="55" r="${r}"
                    stroke-dasharray="${c.toFixed(1)}"
                    stroke-dashoffset="${(c * (1 - v)).toFixed(1)}"
                    data-arc="${c.toFixed(1)}"></circle>
          </svg>
          <div class="gauge-num" ${isNum(value) ? `data-num="${value}"` : ""}>${isNum(value) ? "0" : "—"}</div>
        </div>
        <div class="gauge-lab">${esc(label)}</div>
        ${sub ? `<div class="gauge-sub">${esc(sub)}</div>` : ""}
      </div>`;
  }

  function chartHtml(series, opts) {
    const o = opts || {};
    const W = 1000;
    const H = 420;
    const pad = { l: o.padLeft || 54, r: 26, t: 20, b: 34 };
    const all = series.flatMap((s) => s.points);
    if (all.length < 2) return "";
    const xs = all.map((p) => p.x);
    const ys = all.map((p) => p.y);
    const x0 = Math.min(...xs);
    const x1 = Math.max(...xs);
    let y0 = isNum(o.yMin) ? o.yMin : Math.min(...ys);
    let y1 = isNum(o.yMax) ? o.yMax : Math.max(...ys);
    if (y1 - y0 < 1e-6) y1 = y0 + 1;
    const padY = (y1 - y0) * 0.12;
    if (!isNum(o.yMin)) y0 -= padY;
    if (!isNum(o.yMax)) y1 += padY;

    const sx = (x) => pad.l + ((x - x0) / Math.max(1e-9, x1 - x0)) * (W - pad.l - pad.r);
    const sy = (y) => pad.t + (1 - (y - y0) / Math.max(1e-9, y1 - y0)) * (H - pad.t - pad.b);

    const paths = series.map((s, i) => {
      const d = s.points.map((p, j) => `${j ? "L" : "M"}${sx(p.x).toFixed(1)} ${sy(p.y).toFixed(1)}`).join(" ");
      const area = s.area
        ? `<path class="area" fill="url(#cg${i})" d="${d} L${sx(s.points[s.points.length - 1].x).toFixed(1)} ${(H - pad.b).toFixed(1)} L${sx(s.points[0].x).toFixed(1)} ${(H - pad.b).toFixed(1)} Z"></path>`
        : "";
      return `${area}<path class="plot ${s.cls || ""}" data-draw="${i}" d="${d}"></path>`;
    }).join("");

    const defs = series.map((s, i) => (s.area ? `
      <linearGradient id="cg${i}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${s.areaColor || "rgba(68,214,44,.34)"}"/>
        <stop offset="100%" stop-color="transparent"/>
      </linearGradient>` : "")).join("");

    const marks = (o.markers || []).map((m) => {
      const px = sx(m.x);
      const py = sy(m.y);
      return `
        <line class="marker-line" x1="${px.toFixed(1)}" y1="${pad.t}" x2="${px.toFixed(1)}" y2="${(H - pad.b).toFixed(1)}"></line>
        <circle class="dot ${m.bad ? "dot--bad" : ""}" data-fx="dot" cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="7"></circle>
        ${m.label ? `<text class="${m.bad ? "lab-bad" : ""}" data-fx="clab" x="${clamp(px, pad.l, W - pad.r - 150).toFixed(1)}" y="${clamp(py - 20, 20, H - pad.b - 8).toFixed(1)}">${esc(m.label)}</text>` : ""}`;
    }).join("");

    const guides = (o.guides || []).map((g) => {
      const py = sy(g.y);
      return `<line class="mid-line" x1="${pad.l}" y1="${py.toFixed(1)}" x2="${(W - pad.r).toFixed(1)}" y2="${py.toFixed(1)}"></line>
              ${g.label ? `<text x="2" y="${(py + 5).toFixed(1)}">${esc(g.label)}</text>` : ""}`;
    }).join("");

    const axis = (o.axisLabels || []).map((a) => {
      const px = clamp(sx(a.x), pad.l, W - pad.r - 50);
      return `<text x="${px.toFixed(1)}" y="${H - 8}">${esc(a.label)}</text>`;
    }).join("");

    return `<div class="chart" data-fx="chart">
        <svg viewBox="0 0 ${W} ${H}" aria-hidden="true"><defs>${defs}</defs>${guides}${paths}${marks}${axis}</svg>
      </div>`;
  }

  /** Vertical bar cluster — used for blunder timing, clock, opponents. */
  function barsHtml(items, opts) {
    const o = opts || {};
    const max = Math.max(...items.map((b) => Number(b.value) || 0), o.min || 0.0001);
    return `
      <div class="bars" data-fx="bars">
        ${items.map((b) => `
          <div class="bar ${b.cls || ""}">
            <div class="bar-val">${esc(b.display)}</div>
            <div class="bar-col"><i style="--h:${((Number(b.value) || 0) / max * 100).toFixed(1)}%"></i></div>
            <div class="bar-lab">${esc(b.label)}</div>
            ${b.sub ? `<div class="bar-sub">${esc(b.sub)}</div>` : ""}
          </div>`).join("")}
      </div>`;
  }

  function shortOpening(name, cap) {
    const limit = cap || 34;
    let s = String(name || "").replace(/\s+/g, " ").trim();
    if (!s) return "Unknown opening";
    for (const pre of [
      "Queens Pawn Opening ", "Queen's Pawn Opening ", "Queens Pawn Game ",
      "Queen's Pawn Game ", "Kings Pawn Opening ", "King's Pawn Opening ",
      "Kings Pawn Game ", "King's Pawn Game ",
    ]) {
      if (s.startsWith(pre) && s.length > pre.length + 3) { s = s.slice(pre.length); break; }
    }
    if (s.length <= limit) return s;
    const cut = s.lastIndexOf(" ", limit);
    return `${(cut >= 12 ? s.slice(0, cut) : s.slice(0, limit)).replace(/[ ,:-]+$/, "")}…`;
  }

  // ══════════════════════════════════════════════════════════════════════
  // The scrubbed timeline for one panel
  // ══════════════════════════════════════════════════════════════════════

  /**
   * Build the panel's whole reveal as one paused timeline of unit length. The
   * strip drives `.progress()` every frame, so the reveal is reversible and
   * tied to the wheel rather than to a clock.
   */
  function buildTimeline(el) {
    if (!G) { paintStatic(el); return null; }
    const tl = G.timeline({ paused: true });
    const q = (sel) => $$(sel, el);

    // Headline: characters fly in on a curve, words stay on their baseline.
    const title = $('[data-fx="title"]', el);
    if (title && window.SplitText) {
      try {
        const split = new window.SplitText(title, { type: "chars,words" });
        el._split = split;
        tl.from(split.chars, {
          yPercent: 118, rotationX: -78, opacity: 0, filter: "blur(9px)",
          transformOrigin: "50% 100%", duration: 0.55,
          stagger: { each: 0.012, from: "start" }, ease: "power3.out",
        }, 0);
      } catch { tl.from(title, { y: 40, opacity: 0, duration: 0.5 }, 0); }
    } else if (title) {
      tl.from(title, { y: 40, opacity: 0, filter: "blur(8px)", duration: 0.5, ease: "power3.out" }, 0);
    }

    const kicker = $('[data-fx="kicker"]', el);
    if (kicker) tl.from(kicker, { x: -26, opacity: 0, duration: 0.35, ease: "power2.out" }, 0);

    const sub = $('[data-fx="sub"]', el);
    if (sub) tl.from(sub, { y: 22, opacity: 0, filter: "blur(6px)", duration: 0.45, ease: "power2.out" }, 0.18);

    const tiles = q('[data-fx="tile"]');
    if (tiles.length) {
      tl.from(tiles, {
        y: 46, opacity: 0, scale: 0.9, rotationX: -22, filter: "blur(7px)",
        duration: 0.5, stagger: 0.055, ease: "back.out(1.5)",
      }, 0.2);
    }

    // Rows fill from the left; the fill *is* the measurement.
    const rows = q('[data-fx="row"]');
    if (rows.length) {
      tl.from(rows, {
        x: -60, opacity: 0, duration: 0.42, stagger: 0.05, ease: "power3.out",
      }, 0.2);
      rows.forEach((r, i) => {
        tl.fromTo($(".row-fill", r), { scaleX: 0 },
          { scaleX: Number(r.dataset.fill || 0), duration: 0.55, ease: "expo.out" }, 0.3 + i * 0.05);
      });
    }

    // Segmented result bar: each block grows from its own left edge.
    const segs = q('[data-fx="seg"]');
    if (segs.length) {
      tl.fromTo(segs, { scaleX: 0 },
        { scaleX: 1, duration: 0.6, stagger: 0.07, ease: "expo.out", transformOrigin: "left center" }, 0.22);
    }

    // Columns and bar clusters rise out of the floor.
    const cols = q(".col-bar, .bar-col i");
    if (cols.length) {
      tl.from(cols, {
        scaleY: 0, duration: 0.62, stagger: 0.06, ease: "expo.out", transformOrigin: "50% 100%",
      }, 0.24);
    }

    // Gauges: DrawSVG when available, dashoffset otherwise.
    const arcs = q(".gauge-ring .a");
    if (arcs.length) {
      if (window.DrawSVGPlugin) {
        tl.from(arcs, { drawSVG: "0%", duration: 0.75, stagger: 0.09, ease: "power2.inOut" }, 0.2);
      } else {
        arcs.forEach((a, i) => {
          tl.fromTo(a, { strokeDashoffset: Number(a.dataset.arc || 0) },
            { strokeDashoffset: a.getAttribute("stroke-dashoffset"), duration: 0.75, ease: "power2.inOut" },
            0.2 + i * 0.09);
        });
      }
      tl.from(q(".gauge"), { scale: 0.72, opacity: 0, duration: 0.45, stagger: 0.08, ease: "back.out(1.6)" }, 0.14);
    }

    // Charts draw themselves.
    const plots = q(".chart .plot");
    if (plots.length && window.DrawSVGPlugin) {
      tl.from(plots, { drawSVG: "0%", duration: 0.9, stagger: 0.16, ease: "power1.inOut" }, 0.18);
    }
    const areas = q(".chart .area");
    if (areas.length) tl.from(areas, { opacity: 0, duration: 0.6 }, 0.5);
    const dots = q('[data-fx="dot"]');
    if (dots.length) tl.from(dots, { scale: 0, opacity: 0, duration: 0.4, stagger: 0.1, ease: "back.out(3)", transformOrigin: "50% 50%" }, 0.72);
    const clabs = q('[data-fx="clab"]');
    if (clabs.length) tl.from(clabs, { opacity: 0, duration: 0.3 }, 0.8);

    // The board arrives with a little depth.
    const figs = q('[data-fx="fig"]');
    if (figs.length) {
      tl.from(figs, {
        scale: 0.84, opacity: 0, rotationY: 20, filter: "blur(10px)",
        duration: 0.6, ease: "power3.out",
      }, 0.14);
    }

    const tags = q('[data-fx="tag"]');
    if (tags.length) {
      tl.from(tags, {
        scale: 0.5, opacity: 0, y: 30, duration: 0.42,
        stagger: { each: 0.04, from: "random" }, ease: "back.out(2)",
      }, 0.2);
    }

    const chips = q('[data-fx="chip"]');
    if (chips.length) tl.from(chips, { y: 18, opacity: 0, duration: 0.32, stagger: 0.05, ease: "power2.out" }, 0.42);

    const cta = q('[data-fx="cta"]');
    if (cta.length) tl.from(cta, { y: 26, opacity: 0, duration: 0.4, stagger: 0.06, ease: "power2.out" }, 0.5);

    // Counters ride the same progress, so the numbers tick up as you scroll in.
    q("[data-num]").forEach((n, i) => {
      const to = Number(n.dataset.num);
      if (!Number.isFinite(to)) return;
      const dec = Math.abs(to) < 10 && !Number.isInteger(to) ? 1 : 0;
      const prefix = n.dataset.prefix || "";
      const suffix = n.dataset.suffix || "";
      const obj = { v: 0 };
      tl.to(obj, {
        v: to, duration: 0.7, ease: "power2.out",
        onUpdate() {
          const sign = n.dataset.signed && obj.v > 0 ? "+" : "";
          n.textContent = `${prefix}${sign}${obj.v.toFixed(dec)}${suffix}`;
        },
      }, 0.22 + Math.min(i, 8) * 0.035);
    });

    tl.progress(0);
    return tl;
  }

  /** No GSAP: show everything in its resting state so the strip still reads. */
  function paintStatic(el) {
    $$("[data-num]", el).forEach((n) => {
      const to = Number(n.dataset.num);
      if (!Number.isFinite(to)) return;
      const dec = Math.abs(to) < 10 && !Number.isInteger(to) ? 1 : 0;
      const sign = n.dataset.signed && to > 0 ? "+" : "";
      n.textContent = `${n.dataset.prefix || ""}${sign}${to.toFixed(dec)}${n.dataset.suffix || ""}`;
    });
    $$(".row-fill", el).forEach((f) => {
      f.style.transform = `scaleX(${f.parentElement.dataset.fill || 0})`;
    });
  }

  // ══════════════════════════════════════════════════════════════════════
  // Panels
  // ══════════════════════════════════════════════════════════════════════

  function buildPanels() {
    const m = F.metrics || {};
    const h = headline();
    const p = pro();
    const nr = narr();
    const f = facts();
    const out = [];
    const add = (x) => { if (x) out.push(x); };

    add(pTitle(h, p, m, f));
    add(pRecord(h));
    add(pRating(h, f));
    add(pColour(f));
    add(pVerdict(nr));
    add(pQuality(h, p));
    add(pPhases(p));
    add(pSpine(nr));
    add(pShapes(nr));
    add(pMoment(nr, f));
    add(pOpenings(p));
    add(pCritical(p));
    add(pBlunderClock(p));
    add(pTactics(m, nr));
    add(pClock(m, p));
    add(pOpponents(f));
    add(pEndgame(p));
    add(pResilience(p));
    add(pHabits(nr));
    add(pStrengths(nr, p));
    add(pLeaks(p, h));
    add(pOutro(m, h));
    return out;
  }

  const panel = (id, chapter, html, opts) =>
    ({ id, chapter, html, width: 1, hasBoard: !!(opts && opts.hasBoard) });

  // 1 ── Title: a dense montage, not a name card.
  function pTitle(h, p, m, f) {
    const meta = F.meta || {};
    const rec = h.record || {};
    const games = rec.games || f.length;
    if (!games) return null;
    const handle = meta.handle || meta.chesscom_handle || "You";
    const src = meta.source === "lichess" ? "Lichess" : "Chess.com";
    const err = h.error_rates || {};
    const acc = h.accuracy || {};
    const rating = h.rating || {};
    const elo = h.elo_left_on_board || {};
    const leak = (p.leaks || [])[0];
    const mq = p.move_quality || {};

    // Everything a player wants before they read a sentence, streaming in.
    const stats = [
      ["Record", `${rec.wins || 0}–${rec.draws || 0}–${rec.losses || 0}`, "win / draw / loss"],
      ["Score", isNum(rec.score_pct) ? `${Math.round(rec.score_pct * 100)}%` : "—", "of the points available"],
      ["Accuracy", isNum(acc.mean) ? `${acc.mean.toFixed(1)}%` : "—", acc.consistency ? String(acc.consistency) : "across every move"],
      ["Performance", h.performance_rating ?? "—", "what this run was worth"],
      ["Rating", isNum(rating.delta) ? `${rating.delta > 0 ? "+" : ""}${rating.delta}` : "—",
        isNum(rating.start) ? `${rating.start} → ${rating.end}` : "unrated window"],
      ["Blunders", err.blunders ?? "—", isNum(err.blunders_per_100) ? `${err.blunders_per_100.toFixed(1)} per 100 moves` : ""],
      ["Clean games", isNum(err.clean_game_rate) ? `${Math.round(err.clean_game_rate * 100)}%` : "—", "with no blunder at all"],
      ["On the table", isNum(elo.points) ? `+${elo.points}` : "—", "rating you left behind"],
    ];

    return panel("title", "Titles", `
      <div class="pnl-inner pnl-title-card">
        <div class="tt-marquee" data-par="0.22" aria-hidden="true">
          <span>${esc(handle)}</span><span>${esc(handle)}</span><span>${esc(handle)}</span>
        </div>
        <div class="tt-main" data-par="0.05">
          <p class="pnl-kicker" data-fx="kicker">${esc(src)} · last ${esc(meta.window_days || 30)} days · ${esc(meta.time_class || "blitz")}</p>
          <h2 class="pnl-title tt-name" data-fx="title">${esc(handle)}</h2>
          <p class="pnl-sub" data-fx="sub">
            <b>${games}</b> games · <b>${(h.volume && h.volume.user_moves || 0).toLocaleString()}</b> of your moves
            through the engine${mq.total_moves ? "" : ""}. Scroll — or let it run.
          </p>
        </div>
        <div class="tt-grid" data-par="-0.04">
          ${stats.map(([k, v, s]) => `
            <div class="tt-cell" data-fx="tile">
              <span class="k">${esc(k)}</span>
              <span class="v">${esc(String(v))}</span>
              ${s ? `<span class="s">${esc(s)}</span>` : ""}
            </div>`).join("")}
        </div>
        ${leak ? `<p class="tt-leak" data-fx="chip"><em>Biggest leak</em> ${esc(leak.title)} — costing about ${Number(leak.impact_win_pct_per_game).toFixed(1)} win% every game.</p>` : ""}
      </div>`);
  }

  // 2 ── Record
  function pRecord(h) {
    const r = h.record || {};
    if (!r.games) return null;
    const dec = r.decided || r.games || 1;
    const w = (r.wins || 0) / dec;
    const d = (r.draws || 0) / dec;
    const l = (r.losses || 0) / dec;
    const exp = h.expectancy || {};
    return panel("record", "The record", `
      <div class="pnl-inner">
        ${head("The window", `You scored <em data-num="${Math.round((r.score_pct || 0) * 100)}" data-suffix="%">0%</em>`,
          `${r.wins || 0} wins, ${r.draws || 0} draws, ${r.losses || 0} losses across ${r.games} games.`)}
        <div class="segbar">
          <i class="w" data-fx="seg" style="flex-grow:${Math.max(w, 0.02)}">${r.wins || ""}</i>
          <i class="d" data-fx="seg" style="flex-grow:${Math.max(d, 0.02)}">${r.draws || ""}</i>
          <i class="l" data-fx="seg" style="flex-grow:${Math.max(l, 0.02)}">${r.losses || ""}</i>
        </div>
        <div class="tiles">
          ${tile("Performance rating", h.performance_rating, "what this run was worth")}
          ${tile("Expected score",
            isNum(exp.expected) && exp.n ? `${Math.round((exp.expected / exp.n) * 100)}%` : "—",
            exp.n ? `predicted by the rating gaps over ${exp.n} games` : "needs rated opponents")}
          ${tile("Vs expectation", isNum(exp.delta) ? exp.delta : "—",
            exp.note ? String(exp.note) : "points against the model",
            isNum(exp.delta) ? (exp.delta >= 0 ? "is-good" : "is-bad") : "")}
        </div>
      </div>`);
  }

  // 3 ── Rating line
  function pRating(h, f) {
    const r = h.rating || {};
    const chrono = f.slice().reverse().filter((g) => isNum(g.user_rating));
    if (chrono.length < 4 || !isNum(r.delta)) return null;
    const pts = chrono.map((g, i) => ({ x: i, y: Number(g.user_rating) }));
    const up = Number(r.delta) >= 0;
    const peakIdx = pts.reduce((b, p, i) => (p.y > pts[b].y ? i : b), 0);
    const floorIdx = pts.reduce((b, p, i) => (p.y < pts[b].y ? i : b), 0);
    return panel("rating", "The rating line", `
      <div class="pnl-inner">
        ${head("Where it went", up
          ? `Your rating climbed <em data-num="${r.delta}" data-signed="1">0</em>`
          : `Your rating fell <span class="bad" data-num="${r.delta}">0</span>`,
          `${r.start} to ${r.end} over ${chrono.length} rated games. Peak ${r.peak}, floor ${r.floor}.`)}
        ${chartHtml([{ points: pts, cls: up ? "" : "plot--cool", area: true,
          areaColor: up ? "rgba(68,214,44,.28)" : "rgba(90,210,255,.24)" }], {
          markers: [
            { x: peakIdx, y: pts[peakIdx].y, label: `peak ${pts[peakIdx].y}` },
            { x: floorIdx, y: pts[floorIdx].y, label: `floor ${pts[floorIdx].y}`, bad: true },
          ],
          axisLabels: [{ x: 0, label: "first game" }, { x: pts.length - 1, label: "latest" }],
        })}
      </div>`);
  }

  // 4 ── Colour
  function pColour(f) {
    if (f.length < 6) return null;
    const side = (c) => {
      const rows = f.filter((g) => g.user_color === c && isNum(g.points));
      if (!rows.length) return null;
      const acc = rows.filter((g) => isNum(g.accuracy));
      return {
        n: rows.length,
        score: rows.reduce((a, g) => a + Number(g.points), 0) / rows.length,
        wins: rows.filter((g) => g.outcome === "win").length,
        accuracy: acc.length ? acc.reduce((a, g) => a + Number(g.accuracy), 0) / acc.length : null,
      };
    };
    const wht = side("white");
    const blk = side("black");
    if (!wht || !blk || wht.n < 3 || blk.n < 3) return null;
    const gap = (wht.score - blk.score) * 100;
    const big = Math.abs(gap) >= 12;
    const strong = gap >= 0 ? "White" : "Black";
    const top = Math.max(wht.score, blk.score, 0.01);
    const col = (s, label, cls) => `
      <div class="col ${cls}">
        <div class="col-val" data-num="${Math.round(s.score * 100)}" data-suffix="%">0%</div>
        <div class="col-bar" style="height:${((s.score / top) * 100).toFixed(1)}%"></div>
        <div class="col-lab">${label}</div>
        <div class="col-sub">${s.wins}W of ${s.n}${isNum(s.accuracy) ? ` · ${Math.round(s.accuracy)}% accuracy` : ""}</div>
      </div>`;
    return panel("colour", "White and Black", `
      <div class="pnl-inner">
        ${head("Colour", big
          ? `You are a different player as <em>${esc(strong)}</em>`
          : "Both colours look the same",
          big
            ? `${Math.round(Math.abs(gap))} points of score separate your two colours — a repertoire problem on one side, not variance.`
            : `${Math.round(Math.abs(gap))} points separate them. Whatever is costing you games, it is not the colour you get.`)}
        <div class="cols">${col(wht, "White", "col--white")}${col(blk, "Black", "col--black")}</div>
        <p class="pnl-note">Score % — a win is 1, a draw is a half.</p>
      </div>`);
  }

  // 5 ── Verdict
  function pVerdict(nr) {
    const v = nr.verdict || {};
    if (!v.headline) return null;
    const chips = (v.chips || []).slice(0, 3);
    return panel("verdict", "The verdict", `
      <div class="pnl-inner pnl-centre">
        ${head("The verdict", `<em>${esc(v.headline)}</em>`, v.diagnosis ? esc(v.diagnosis) : "", { par: 0.09 })}
        ${chips.length ? `<div class="chipline">
          ${chips.map((c) => `<span class="bigchip" data-fx="chip"><b>${esc(String(c.label || ""))}</b>${esc(String(c.value ?? ""))}</span>`).join("")}
        </div>` : ""}
        ${(v.not_the_reason || []).length ? `<p class="pnl-note" data-fx="chip">Not the reason: ${(v.not_the_reason || []).slice(0, 2).map((x) => esc(String(x))).join(" · ")}</p>` : ""}
      </div>`);
  }

  // 6 ── Move quality
  function pQuality(h, p) {
    const mq = p.move_quality || {};
    const err = h.error_rates || {};
    if (!mq.total_moves) return null;
    const mix = (mq.mix || []).filter((x) => x.n > 0);
    return panel("quality", "Move quality", `
      <div class="pnl-inner">
        ${head("Every move you played", isNum(mq.mean_accuracy)
          ? `<em data-num="${mq.mean_accuracy}" data-suffix="%">0%</em> accuracy over ${mq.total_moves.toLocaleString()} moves`
          : `${mq.total_moves.toLocaleString()} moves measured`,
          isNum(err.moves_per_blunder)
            ? `One blunder every ${Math.round(err.moves_per_blunder)} moves. The average hides where the loss actually happens.`
            : "")}
        <div class="pnl-split">
          <div class="gauges gauges--solo">
            ${gaugeHtml(mq.mean_accuracy, "Mean accuracy",
              isNum(err.best_move_rate) ? `${Math.round(err.best_move_rate * 100)}% top-engine moves` : "", null, { floor: 50 })}
          </div>
          <div>
            <div class="tiles">
              ${tile("Blunders", err.blunders, isNum(err.blunders_per_100) ? `${err.blunders_per_100.toFixed(1)} per 100` : "", "is-bad")}
              ${tile("Mistakes", err.mistakes, isNum(err.mistakes_per_100) ? `${err.mistakes_per_100.toFixed(1)} per 100` : "", "is-warn")}
              ${tile("Inaccuracies", err.inaccuracies, isNum(err.inaccuracies_per_100) ? `${err.inaccuracies_per_100.toFixed(1)} per 100` : "")}
              ${tile("Clean games", isNum(err.clean_game_rate) ? Math.round(err.clean_game_rate * 100) : "—", "no blunder at all", "is-good")}
            </div>
            ${mix.length ? `<div class="mixbar">
              ${mix.map((x) => `<i data-fx="seg" class="mx-${esc(x.label)}" style="flex-grow:${Math.max(x.rate, 0.004)}" title="${esc(x.label)} ${x.n}"></i>`).join("")}
            </div>
            <div class="mixkey">${mix.slice(0, 6).map((x) => `<span data-fx="chip"><i class="mx-${esc(x.label)}"></i>${esc(titleCase(x.label))} ${Math.round(x.rate * 100)}%</span>`).join("")}</div>` : ""}
          </div>
        </div>
      </div>`);
  }

  // 7 ── Phases
  function pPhases(p) {
    const phases = ((p.move_quality || {}).by_phase || []).filter((x) => x.moves > 0);
    if (phases.length < 2) return null;
    const weakest = (p.move_quality || {}).weakest_phase;
    const maxLoss = Math.max(...phases.map((x) => x.delta_w_per_move), 0.01);
    return panel("phases", "Where it leaks", `
      <div class="pnl-inner">
        ${head("Phase by phase", weakest
          ? `Your <span class="bad">${esc(weakest)}</span> is where it goes`
          : "Phase by phase",
          "Accuracy on the left, win% given away per move on the right. The phase that leaks is rarely the one that feels hardest.")}
        <div class="gauges">
          ${phases.map((ph) => gaugeHtml(ph.accuracy, titleCase(ph.phase),
            `${ph.moves} moves · ${ph.delta_w_per_move.toFixed(1)} win%/move`,
            ph.phase === weakest ? "var(--cine-bad)" : "var(--cine-accent)", { floor: 50 })).join("")}
        </div>
        <div class="rows rows--tight">
          ${phases.map((ph) => rowHtml(
            esc(titleCase(ph.phase)),
            `${ph.moves} moves · ${Math.round(ph.blunder_rate * 1000) / 10}% of them blunders`,
            `${ph.delta_w_per_move.toFixed(1)} <span class="dim">win% / move</span>`,
            ph.delta_w_per_move / maxLoss,
            ph.phase === weakest ? "is-bad" : "",
          )).join("")}
        </div>
      </div>`);
  }

  // 8 ── Spine
  function pSpine(nr) {
    const sp = nr.spine || {};
    const wins = (sp.wins || []).filter(isNum);
    const losses = (sp.losses || []).filter(isNum);
    if (wins.length < 5 || losses.length < 5) return null;
    const toPts = (arr) => arr.map((y, i) => ({ x: i / (arr.length - 1), y: Number(y) }));
    const last = losses.length - 1;
    const finalGap = Math.max(0.05, wins[last] - losses[last]);
    let breakAt = last;
    for (let i = 1; i < losses.length; i++) {
      if (wins[i] - losses[i] >= finalGap * 0.4) { breakAt = i; break; }
    }
    const phaseName = breakAt / losses.length < 0.34 ? "opening"
      : breakAt / losses.length < 0.72 ? "middlegame" : "endgame";
    const gapPct = Math.round((wins[breakAt] - losses[breakAt]) * 100);
    return panel("spine", "Shape of a game", `
      <div class="pnl-inner">
        ${head("Averaged over every game", `Your losses turn in the <em>${esc(phaseName)}</em>`,
          `Two average games: the ones you won, the ones you lost. By this point they are already ${gapPct} win% apart, and they never converge again.`)}
        ${chartHtml([
          { points: toPts(wins), area: true, areaColor: "rgba(68,214,44,.2)" },
          { points: toPts(losses), cls: "plot--cool" },
        ], {
          yMin: 0, yMax: 1,
          guides: [{ y: 0.5, label: "even" }],
          markers: [{ x: breakAt / last, y: Number(losses[breakAt]), label: "they split here", bad: true }],
          axisLabels: [{ x: 0, label: "move 1" }, { x: 1, label: "final move" }],
        })}
        <div class="legend">
          <span data-fx="chip"><i style="background:var(--cine-accent)"></i>games you won</span>
          <span data-fx="chip"><i style="background:var(--cine-cool)"></i>games you lost</span>
        </div>
      </div>`);
  }

  // 9 ── Loss shapes
  function pShapes(nr) {
    const shapes = ((nr.why_you_lose || {}).shapes || []).filter((s) => s.n > 0);
    if (shapes.length < 2) return null;
    const top = shapes[0];
    const max = Math.max(...shapes.map((s) => s.n));
    return panel("shapes", "How you lose", `
      <div class="pnl-inner">
        ${head("How the losses look", `Most often: <span class="bad">${esc(top.label)}</span>`,
          esc(top.caption || "Your losses are not one thing — but they are not evenly spread either."))}
        <div class="rows">
          ${shapes.slice(0, 5).map((s, i) => rowHtml(
            esc(s.label), esc(s.caption || ""),
            `${s.n} <span class="dim">${Math.round((s.share || 0) * 100)}%</span>`,
            s.n / max, i === 0 ? "is-bad" : "",
          )).join("")}
        </div>
      </div>`);
  }

  // 10 ── The moment
  function pMoment(nr, f) {
    const moments = (nr.why_you_lose || {}).moments || [];
    const moment = moments.find((mm) => mm.fen && (mm.sparkline || []).length >= 6) || moments.find((mm) => mm.fen);
    if (!moment) return null;
    const fact = f.find((g) => g.game_id === moment.game_id);
    const raw = (moment.sparkline || []).filter(isNum).map(Number);
    const curve = moment.user_color === "black" ? raw.map((v) => 1 - v) : raw;
    const hasCurve = curve.length >= 6;
    const steepestNear = (from, radius) => {
      let at = from;
      let best = -Infinity;
      for (let i = Math.max(1, from - radius); i <= Math.min(curve.length - 1, from + radius); i++) {
        const dd = curve[i - 1] - curve[i];
        if (dd > best) { best = dd; at = i; }
      }
      return at;
    };
    let markIdx = null;
    if (hasCurve && isNum(moment.ply) && fact && fact.ply_count) {
      markIdx = steepestNear(clamp(Math.round((Number(moment.ply) / fact.ply_count) * (curve.length - 1)), 0, curve.length - 1), 2);
    } else if (hasCurve) {
      markIdx = steepestNear(Math.floor(curve.length / 2), curve.length);
    }
    const moveNo = isNum(moment.ply) ? Math.ceil(Number(moment.ply) / 2) : null;
    const caption = String(moment.caption || "").trim();
    const restates = caption && moment.san
      && caption.replace(/[^a-z0-9+#=-]/gi, "").toLowerCase() === `youplayed${moment.san}`.replace(/[^a-z0-9+#=-]/gi, "").toLowerCase();
    const sub = (caption && !restates) ? caption
      : `${moment.opponent ? `Against ${moment.opponent}` : ""}${moment.opening ? ` in the ${shortOpening(moment.opening, 40)}` : ""}.`.trim()
        || "One move, and the position changed hands.";
    return panel("moment", "The moment", `
      <div class="pnl-inner">
        ${head("One position", `You played <span class="bad">${esc(moment.san || "—")}</span>${moment.best_san ? `, not <em>${esc(moment.best_san)}</em>` : ""}`, esc(sub))}
        <div class="pnl-split">
          <div class="fig" data-fx="fig">
            <div class="fig-board" data-fen="${esc(moment.fen)}" data-color="${esc(moment.user_color || "white")}"
                 data-played="${esc(moment.played_uci || "")}" data-best="${esc(moment.best_uci || "")}"></div>
            <div class="fig-moves">
              ${moment.san ? `<span class="chip chip--played" data-fx="chip"><i></i>${esc(moment.san)} played</span>` : ""}
              ${moment.best_san ? `<span class="chip chip--best" data-fx="chip"><i></i>${esc(moment.best_san)} wanted</span>` : ""}
            </div>
            <p class="fig-cap">${moveNo ? `Move ${moveNo}. ` : ""}${esc(titleCase(moment.outcome || ""))}${moment.opponent ? ` vs ${esc(moment.opponent)}` : ""}.</p>
          </div>
          <div>
            ${hasCurve ? chartHtml([{ points: curve.map((y, i) => ({ x: i, y })), area: true }], {
              yMin: 0, yMax: 1,
              guides: [{ y: 0.5, label: "even" }],
              markers: markIdx !== null ? [{ x: markIdx, y: curve[markIdx], label: "here", bad: true }] : [],
              axisLabels: [{ x: 0, label: "move 1" }, { x: curve.length - 1, label: "final move" }],
            }) : ""}
            <p class="pnl-note">Your winning chances across that whole game. One of ${moments.length || 1} flagged — the rest are waiting at the end.</p>
            <div class="ctaline">
              <button type="button" class="cine-btn cine-btn--go" data-fx="cta" data-act="review"
                      data-game="${esc(moment.game_id || "")}" data-ply="${esc(moment.ply || "")}">Open this game</button>
            </div>
          </div>
        </div>
      </div>`, { hasBoard: true });
  }

  // 11 ── Openings
  function pOpenings(p) {
    const op = p.openings || {};
    const rows = (op.rows || []).filter((r) => r.n >= (op.min_games || 3) && isNum(r.score_pct));
    if (rows.length < 2) return null;
    const sorted = rows.slice().sort((a, b) => b.score_pct - a.score_pct);
    const best = sorted[0];
    const worst = sorted[sorted.length - 1];
    const show = [...sorted.slice(0, 3), ...sorted.slice(-3)].filter((r, i, arr) => arr.indexOf(r) === i).slice(0, 6);
    return panel("openings", "Openings", `
      <div class="pnl-inner">
        ${head("The repertoire", `<em>${esc(shortOpening(best.opening, 22))}</em> works.<br><span class="bad">${esc(shortOpening(worst.opening, 22))}</span> does not.`,
          `${Math.round(best.score_pct * 100)}% from ${best.n} games as ${best.color} against ${Math.round(worst.score_pct * 100)}% from ${worst.n} as ${worst.color}. Minimum ${op.min_games || 3} games.`)}
        <div class="rows">
          ${show.map((r) => rowHtml(
            `${esc(shortOpening(r.opening))} <span class="dim">as ${esc(r.color)}</span>`,
            `${r.n} games${isNum(r.mean_accuracy) ? ` · ${Math.round(r.mean_accuracy)}% accuracy` : ""}${isNum(r.mean_deviation_ply) ? ` · out of book on move ${Math.ceil(r.mean_deviation_ply / 2)}` : ""}`,
            `${Math.round(r.score_pct * 100)}%`, r.score_pct,
            r.score_pct >= 0.55 ? "is-good" : r.score_pct <= 0.4 ? "is-bad" : "",
          )).join("")}
        </div>
      </div>`);
  }

  // 12 ── Critical moments
  function pCritical(p) {
    const cm = p.critical_moments || {};
    const buckets = (cm.buckets || []).filter((b) => b.moves > 0 && isNum(b.accuracy));
    if (buckets.length < 2) return null;
    const gap = cm.criticality_gap;
    const bad = isNum(gap) && gap >= 8;
    const colourFor = (k) => (k === "critical" && bad ? "var(--cine-bad)" : k === "tense" ? "var(--cine-warn)" : "var(--cine-accent)");
    return panel("critical", "Under pressure", `
      <div class="pnl-inner">
        ${head("The moves that decide games", bad
          ? `Your accuracy drops <span class="bad" data-num="${Math.round(gap)}">0</span> points when it matters`
          : "You hold your level when the position sharpens",
          bad
            ? "Quiet moves are easy for everyone. These are the positions where several moves genuinely differ — and they are where your average is being spent."
            : `The gap between your quiet moves and your sharpest ones is ${isNum(gap) ? Math.round(Math.abs(gap)) : "small"} points. That is a real strength.`)}
        <div class="gauges">
          ${buckets.map((b) => gaugeHtml(b.accuracy, titleCase(b.key),
            `${b.moves} moves${isNum(b.mean_time) ? ` · ${Math.round(b.mean_time)}s each` : ""}`,
            colourFor(b.key), { floor: 50 })).join("")}
        </div>
        ${cm.time_note ? `<p class="pnl-note" data-fx="chip">${esc(cm.time_note)}</p>`
          : isNum(cm.critical_conversion) ? `<p class="pnl-note" data-fx="chip">You handle ${Math.round(cm.critical_conversion * 100)}% of critical moments without a real loss.</p>` : ""}
      </div>`);
  }

  // 13 ── When the blunders land
  function pBlunderClock(p) {
    const bt = p.blunder_timing || {};
    const buckets = (bt.buckets || []).filter((b) => b.moves > 20);
    if (buckets.length < 3) return null;
    const worst = buckets.reduce((a, b) => (b.blunder_rate > a.blunder_rate ? b : a), buckets[0]);
    return panel("blunderclock", "When it happens", `
      <div class="pnl-inner">
        ${head("The blunder clock", `It happens around <em>${esc(worst.label.replace("Moves ", "move "))}</em>`,
          `Blunder rate per move, by how deep into the game you are. ${Math.round(worst.blunder_rate * 1000) / 10}% of your moves in that window are blunders — the highest of any stretch.`)}
        ${barsHtml(buckets.map((b) => ({
          value: b.blunder_rate,
          display: `${(b.blunder_rate * 100).toFixed(1)}%`,
          label: b.label.replace("Moves ", ""),
          sub: `${b.moves} moves · ${b.blunders} blunders`,
          cls: b === worst ? "is-bad" : "",
        })))}
        <p class="pnl-note">Every bar is a rate, not a count — a stretch you rarely reach cannot hide behind a small total.</p>
      </div>`);
  }

  // 14 ── Missed tactics
  function pTactics(m, nr) {
    const rows = ((m.missed_tactics || {}).rows || []).filter((r) => r.n > 0);
    const narrRows = ((nr.why_you_lose || {}).tactics || []).filter((t) => t.n > 0);
    const src = narrRows.length ? narrRows : rows.map((r) => ({ id: r.tag, label: titleCase(r.tag), n: r.n }));
    if (!src.length) return null;
    const total = src.reduce((a, t) => a + Number(t.n || 0), 0);
    const top = src[0];
    const max = Math.max(...src.map((t) => Number(t.n) || 1));
    return panel("tactics", "Missed tactics", `
      <div class="pnl-inner pnl-centre">
        ${head("What you walked past", `<span class="bad" data-num="${total}">0</span> tactics you could have played`,
          `The pattern you miss most is the <em>${esc(String(top.label || "").toLowerCase())}</em>. These are not engine-only lines — they are patterns a player at your level finds.`)}
        <div class="tags">
          ${src.slice(0, 10).map((t, i) => `
            <span class="tag ${i === 0 ? "is-hot" : ""}" data-fx="tag"
                  style="--w:${(0.72 + 0.5 * (Number(t.n) / max)).toFixed(2)}">
              <b data-num="${t.n}">0</b><span>${esc(String(t.label || t.id))}</span>
            </span>`).join("")}
        </div>
      </div>`);
  }

  // 15 ── The clock
  function pClock(m, p) {
    const scr = m.time_scramble_decay || {};
    const cm = p.critical_moments || {};
    const buckets = (cm.buckets || []);
    const crit = buckets.find((b) => b.key === "critical");
    const quiet = buckets.find((b) => b.key === "quiet");
    const haveTimes = crit && quiet && isNum(crit.mean_time) && isNum(quiet.mean_time);
    const scrMoves = Number(scr.scramble_moves || scr.moves || 0);
    if (!haveTimes && !scrMoves) return null;
    const inverted = haveTimes && crit.mean_time < quiet.mean_time;
    const items = [];
    if (haveTimes) {
      items.push({ value: crit.mean_time, display: `${crit.mean_time.toFixed(1)}s`, label: "Critical", sub: `${crit.moves} moves`, cls: inverted ? "is-bad" : "" });
      const tense = buckets.find((b) => b.key === "tense");
      if (tense && isNum(tense.mean_time)) items.push({ value: tense.mean_time, display: `${tense.mean_time.toFixed(1)}s`, label: "Tense", sub: `${tense.moves} moves`, cls: "is-warn" });
      items.push({ value: quiet.mean_time, display: `${quiet.mean_time.toFixed(1)}s`, label: "Quiet", sub: `${quiet.moves} moves`, cls: "is-good" });
    }
    return panel("clock", "The clock", `
      <div class="pnl-inner">
        ${head("Where your time goes", inverted
          ? "You spend your time on the <span class=\"bad\">wrong moves</span>"
          : "Your clock follows the position",
          inverted
            ? `You give critical positions ${crit.mean_time.toFixed(1)}s and quiet ones ${quiet.mean_time.toFixed(1)}s. The budget is inverted: the moves that decide the game get less thought than the moves that do not.`
            : haveTimes
              ? `${crit.mean_time.toFixed(1)}s on critical moves against ${quiet.mean_time.toFixed(1)}s on quiet ones — you are already spending where it counts.`
              : "Time spent per move, split by how sharp the position was.")}
        ${items.length ? barsHtml(items) : ""}
        ${scrMoves ? `<p class="pnl-note" data-fx="chip">${scrMoves} of your moves were played under 20 seconds on the clock${isNum(scr.delta_w_per_move) ? `, giving away ${scr.delta_w_per_move.toFixed(1)} win% each` : ""}.</p>` : ""}
      </div>`);
  }

  // 16 ── Opponents
  function pOpponents(f) {
    const bands = [
      { key: "lower", label: "Weaker", sub: "100+ below you" },
      { key: "similar", label: "Your level", sub: "within 100" },
      { key: "higher", label: "Stronger", sub: "100+ above you" },
    ];
    const rows = bands.map((b) => {
      const g = f.filter((x) => x.rating_band === b.key && isNum(x.points));
      if (g.length < 3) return null;
      return {
        ...b,
        n: g.length,
        score: g.reduce((a, x) => a + Number(x.points), 0) / g.length,
        accuracy: (() => {
          const a = g.filter((x) => isNum(x.accuracy));
          return a.length ? a.reduce((s, x) => s + Number(x.accuracy), 0) / a.length : null;
        })(),
      };
    }).filter(Boolean);
    if (rows.length < 2) return null;
    const vsStrong = rows.find((r) => r.key === "higher");
    const vsWeak = rows.find((r) => r.key === "lower");
    const slips = vsWeak && vsWeak.score < 0.72;
    return panel("opponents", "Opponents", `
      <div class="pnl-inner">
        ${head("Who you beat", slips
          ? `You drop points to players <span class="bad">below you</span>`
          : vsStrong && vsStrong.score >= 0.45 ? `You hold your own against <em>stronger</em> players` : "By opponent strength",
          slips
            ? `${Math.round(vsWeak.score * 100)}% against opponents 100+ points weaker is where free rating disappears — those games should be near-automatic.`
            : "Score against three bands of opponent. The band you underperform is more useful than the average.")}
        ${barsHtml(rows.map((r) => ({
          value: r.score,
          display: `${Math.round(r.score * 100)}%`,
          label: r.label,
          sub: `${r.n} games${isNum(r.accuracy) ? ` · ${Math.round(r.accuracy)}% acc` : ""}`,
          cls: r.score >= 0.6 ? "is-good" : r.score <= 0.4 ? "is-bad" : "",
        })), { min: 1 })}
        <p class="pnl-note">${esc(bands.map((b) => `${b.label}: ${b.sub}`).join(" · "))}</p>
      </div>`);
  }

  // 17 ── Endgame
  function pEndgame(p) {
    const eg = p.endgame || {};
    const entry = (eg.entry || []).filter((e) => e.n > 0 && isNum(e.score_pct));
    if (!eg.reached || entry.length < 2) return null;
    const worse = entry.find((e) => e.key === "losing");
    return panel("endgame", "Endgames", `
      <div class="pnl-inner">
        ${head("How endgames go", worse && worse.score_pct <= 0.2
          ? `You arrive in <span class="bad">worse endgames</span> and stay there`
          : `<em data-num="${Math.round((eg.reach_rate || 0) * 100)}" data-suffix="%">0%</em> of your games reach one`,
          worse && worse.score_pct <= 0.2
            ? `${worse.n} of your endgames started from a worse position and scored ${Math.round(worse.score_pct * 100)}%. The ending is not the leak — the approach to it is.`
            : `${eg.reached} endgames over ${eg.moves} moves, giving away ${(eg.delta_w_per_move || 0).toFixed(1)} win% per move.`)}
        ${barsHtml(entry.map((e) => ({
          value: Math.max(e.score_pct, 0.01),
          display: `${Math.round(e.score_pct * 100)}%`,
          label: e.label.replace("Entered ", ""),
          sub: `${e.n} endgames`,
          cls: e.score_pct >= 0.6 ? "is-good" : e.score_pct <= 0.25 ? "is-bad" : "is-warn",
        })), { min: 1 })}
        <p class="pnl-note">Score from the position you were in when the endgame began.</p>
      </div>`);
  }

  // 18 ── Resilience
  function pResilience(p) {
    const rs = p.resilience || {};
    const conv = rs.conversion || {};
    const come = rs.comeback || {};
    const missed = rs.missed_wins || {};
    if (!conv.n && !come.n) return null;
    const leaky = isNum(conv.score_pct) && conv.score_pct < 0.85;
    return panel("resilience", "Converting", `
      <div class="pnl-inner">
        ${head("Winning, and staying that way", leaky
          ? `You convert <span class="bad" data-num="${Math.round((conv.score_pct || 0) * 100)}" data-suffix="%">0%</span> of winning positions`
          : "You finish what you start",
          leaky
            ? `${conv.n} games where you were clearly winning; ${(conv.points_dropped || 0).toFixed(1)} whole points went back across them. That is the cheapest rating on this page.`
            : `${conv.n} winning positions and ${Math.round((conv.score_pct || 0) * 100)}% of the points collected. Keep doing it.`)}
        <div class="tiles">
          ${tile("Converted", isNum(conv.score_pct) ? Math.round(conv.score_pct * 100) : "—",
            `from ${conv.n || 0} winning positions`, leaky ? "is-bad" : "is-good")}
          ${tile("Points dropped", conv.points_dropped, "from games already won", "is-bad")}
          ${tile("Rescued", isNum(come.score_pct) ? Math.round(come.score_pct * 100) : "—",
            `from ${come.n || 0} losing positions`, "is-good")}
          ${missed.n ? tile("Missed wins", missed.n, `of ${missed.of} nearly-won games`, "is-warn") : ""}
        </div>
      </div>`);
  }

  // 19 ── Habits
  function pHabits(nr) {
    const habits = ((nr.why_you_lose || {}).habits || []).slice(0, 4);
    if (habits.length < 2) return null;
    const max = Math.max(...habits.map((h) => Number(h.n) || 1), 1);
    return panel("habits", "The habits", `
      <div class="pnl-inner">
        ${head("The habits underneath", "Same mistakes, different games",
          "None of these is a single bad night. Each showed up often enough to be a pattern.")}
        <div class="rows">
          ${habits.map((h, i) => rowHtml(esc(h.title || ""), esc(h.caption || ""),
            isNum(h.n) ? `${h.n}` : "—", isNum(h.n) ? Number(h.n) / max : 0.35, i === 0 ? "is-warn" : "")).join("")}
        </div>
      </div>`);
  }

  // 20 ── Strengths
  function pStrengths(nr, p) {
    const keep = ((nr.how_you_win || {}).strengths || []).filter((s) => s.title);
    const fallback = (p.strengths || []).filter((s) => s.title);
    const rows = (keep.length ? keep : fallback).slice(0, 4);
    if (!rows.length) return null;
    return panel("strengths", "What works", `
      <div class="pnl-inner">
        ${head("Keep doing this", "You are already good at <em>these</em>",
          "A report that only lists faults teaches you to play scared. These held up across the whole window.")}
        <div class="rows">
          ${rows.map((s) => rowHtml(esc(s.title), esc(s.detail || ""), "✓", 0.62, "is-good")).join("")}
        </div>
      </div>`);
  }

  // 21 ── The bill
  function pLeaks(p, h) {
    const leaks = (p.leaks || []).slice(0, 3);
    if (!leaks.length) return null;
    const elo = h.elo_left_on_board || {};
    const max = Math.max(...leaks.map((l) => Number(l.impact_win_pct_per_game) || 0), 1);
    const hasElo = isNum(elo.points) && elo.points > 0;
    return panel("leaks", "The bill", `
      <div class="pnl-inner">
        ${head("What it is costing you", hasElo
          ? `<em data-num="${elo.points}">0</em> rating points, left on the board`
          : "Your three biggest leaks",
          hasElo
            ? `Recover only the moves a player at your level would realistically have found, and this window scores like a ${Math.round(elo.points)}-point stronger player. Nothing here assumes engine vision.`
            : "Ranked by cost per game. They overlap, so they are ranked, never summed.")}
        <div class="rows">
          ${leaks.map((l, i) => rowHtml(
            `<span class="dim">${i + 1}.</span> ${esc(l.title)}`, esc(l.detail || ""),
            `${Number(l.impact_win_pct_per_game).toFixed(1)}<span class="dim"> / game</span>`,
            Number(l.impact_win_pct_per_game) / max,
            l.severity === "high" ? "is-bad" : l.severity === "medium" ? "is-warn" : "",
          )).join("")}
        </div>
      </div>`);
  }

  // 22 ── Outro
  function pOutro(m, h) {
    const practice = (m.practice_flags || {}).items || [];
    const fixes = ((narr().how_you_win || {}).fixes || []).slice(0, 3);
    return panel("outro", "What to do", `
      <div class="pnl-inner pnl-centre">
        ${head("The part that changes your rating", practice.length
          ? `<em data-num="${practice.length}">0</em> positions from your own games`
          : "Now go and fix it",
          practice.length
            ? "Each is a move you played, a move you could have found, and the difference between them. Loaded and waiting."
            : "The full report, every table behind these numbers, and your practice set are one click away.")}
        ${fixes.length ? `<div class="rows rows--wide">
          ${fixes.map((fx, i) => rowHtml(`<span class="dim">${i + 1}.</span> ${esc(fx.title)}`,
            esc(fx.promise || fx.why || ""), "→", 0.5, "")).join("")}
        </div>` : ""}
        <div class="ctaline">
          <button type="button" class="cine-btn cine-btn--go" data-fx="cta" data-act="atlas">Explore everything →</button>
        </div>
      </div>`);
  }

  // ══════════════════════════════════════════════════════════════════════
  // The strip
  // ══════════════════════════════════════════════════════════════════════

  function mountStrip() {
    const track = $("#cine-track", cineEl);
    const rail = $("#cine-ticks", cineEl);
    track.innerHTML = F.panels.map((p, i) => `
      <section class="pnl" data-i="${i}" style="--w:${p.width}">
        <div class="pnl-glow" aria-hidden="true"></div>
        ${p.html}
      </section>`).join("");

    rail.innerHTML = F.panels.map((p, i) =>
      `<button type="button" class="tick" data-tick="${i}" title="${esc(p.chapter)}"><span>${esc(p.chapter)}</span></button>`).join("");

    F.panels.forEach((p, i) => {
      p.el = $(`.pnl[data-i="${i}"]`, track);
      p.inner = $(".pnl-inner", p.el);
      p.tl = null;
    });

    measure();
    // Timelines need real geometry (SplitText measures), so build after layout.
    F.panels.forEach((p) => { p.tl = buildTimeline(p.el); });
    paint();
  }

  function measure() {
    const vw = cineEl.clientWidth || window.innerWidth;
    F.vw = vw;
    let x = 0;
    F.panels.forEach((p) => {
      // offsetLeft/offsetWidth, not vw * width: any disagreement between the
      // model and the layout accumulates down the strip and puts the content
      // off-centre by whole screens near the end.
      p.x = p.el ? p.el.offsetLeft : x;
      p.w = p.el ? p.el.offsetWidth : vw * p.width;
      x = p.x + p.w;
    });
    F.contentW = x;
    // Land the last panel centred rather than flush, so the strip never ends
    // with half a panel pinned against the right edge.
    F.max = Math.max(0, x - vw);
    F.target = clamp(F.target, 0, F.max);
    F.pos = clamp(F.pos, 0, F.max);
  }

  // ── The frame loop ──────────────────────────────────────────────────────

  function frame(t, force) {
    F.raf = requestAnimationFrame(frame);
    if (F.mode !== "play") return;
    const dt = Math.min(0.05, (t - (F.lastT || t)) / 1000);
    F.lastT = t;

    if (F.playing && !force) {
      const speed = (F.vw / FEEL.cruiseSecPerScreen) * SPEEDS[F.speedIdx];
      F.target += speed * dt;
      if (F.target >= F.max) { F.target = F.max; if (F.pos > F.max - 4) showAtlas(); }
    }

    // Frame-rate normalized lerp: the same feel at 60 and 144 Hz.
    const k = 1 - Math.pow(1 - FEEL.lerp, dt * 60);
    const prev = F.pos;
    F.pos += (F.target - F.pos) * (force ? 1 : k);
    F.vel = F.pos - prev;

    paint();
  }

  function paint() {
    const vw = F.vw || 1;
    const track = $("#cine-track", cineEl);
    const skew = clamp(F.vel * FEEL.skew, -FEEL.skewMax, FEEL.skewMax);

    if (G) G.set(track, { x: -F.pos, skewX: reduced() ? 0 : skew, force3D: true });
    else track.style.transform = `translate3d(${-F.pos}px,0,0)`;

    const centre = F.pos + vw / 2;
    let nearest = 0;
    let nearestD = Infinity;

    F.panels.forEach((p, i) => {
      const d = (p.x + p.w / 2 - centre) / vw;
      const ad = Math.abs(d);
      if (ad < nearestD) { nearestD = ad; nearest = i; }

      // Cull anything two screens away — 22 panels of blurred DOM is the one
      // thing that can actually drop frames here.
      const near = ad < 1.9;
      if (p.el.style.visibility !== (near ? "visible" : "hidden")) {
        p.el.style.visibility = near ? "visible" : "hidden";
      }
      if (!near) return;

      if (p.hasBoard && !p.boardMounted && ad < 1.4) {
        p.boardMounted = true;
        $$(".fig-board[data-fen]", p.el).forEach(mountBoard);
      }

      if (G && !reduced()) {
        const depth = clamp(ad, 0, 1.6);
        G.set(p.inner, {
          scale: 1 - depth * FEEL.depthScale,
          rotationY: d * -FEEL.depthRotate,
          opacity: clamp(1 - depth * FEEL.depthFade, 0, 1),
          filter: `blur(${(depth * FEEL.depthBlur).toFixed(2)}px)`,
          transformPerspective: 1400,
          force3D: true,
        });
        // Layered parallax inside the panel.
        $$("[data-par]", p.el).forEach((layer) => {
          G.set(layer, { x: d * vw * Number(layer.dataset.par || 0), force3D: true });
        });
        G.set($(".pnl-glow", p.el), { opacity: clamp(1 - ad * 1.8, 0, 1) });
      }

      // The reveal is a pure function of distance — this is the whole point.
      if (p.tl) p.tl.progress(clamp(1 - ad / FEEL.buildWindow, 0, 1));
    });

    if (F.fx) F.fx.step(F.vel, F.pos);

    // Rail
    const fill = $("#cine-rail-fill", cineEl);
    if (fill) fill.style.transform = `scaleX(${F.max ? clamp(F.pos / F.max, 0, 1) : 1})`;
    if (F.nearest !== nearest) {
      F.nearest = nearest;
      $$(".tick", cineEl).forEach((b, i) => b.classList.toggle("is-on", i === nearest));
      const chap = $("#cine-chapter", cineEl);
      if (chap) {
        const p = F.panels[nearest];
        chap.innerHTML = `<b>${nearest + 1}/${F.panels.length}</b> · ${esc(p ? p.chapter : "")}`;
      }
    }
  }

  // ── Input ───────────────────────────────────────────────────────────────

  function nudge(dx) {
    F.target = clamp(F.target + dx, 0, F.max);
    pauseForInput();
  }

  function pauseForInput() {
    if (F.playing) setPlaying(false, { auto: true });
    if (F.idleTimer) clearTimeout(F.idleTimer);
    F.idleTimer = setTimeout(() => {
      if (F.mode === "play" && F.autoPaused) setPlaying(true);
    }, FEEL.resumeAfterMs);
  }

  function setPlaying(on, opts) {
    F.playing = on;
    F.autoPaused = !on && !!(opts && opts.auto);
    const btn = $("#cine-play", cineEl);
    const icon = $("#cine-play-icon", cineEl);
    if (!btn || !icon) return;
    icon.innerHTML = on
      ? `<path d="M8 5v14M16 5v14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>`
      : `<path d="M8 5l12 7-12 7z" fill="currentColor"/>`;
    btn.setAttribute("aria-label", on ? "Pause" : "Play");
    btn.title = on ? "Pause (Space)" : "Play (Space)";
  }

  function goPanel(i, opts) {
    const p = F.panels[clamp(i, 0, F.panels.length - 1)];
    if (!p) return;
    F.target = clamp(p.x + p.w / 2 - F.vw / 2, 0, F.max);
    if (!(opts && opts.keepPlaying)) pauseForInput();
  }

  function bindInput() {
    const strip = $("#cine-strip", cineEl);

    // GSAP's Observer normalizes wheel / trackpad / touch / drag across
    // browsers, which is most of the reason it is worth loading here.
    if (G && window.Observer) {
      G.registerPlugin(window.Observer);
      F.observer = window.Observer.create({
        target: strip,
        type: "wheel,touch,pointer",
        wheelSpeed: 1,
        dragMinimum: 6,
        preventDefault: true,
        onChangeY: (self) => nudge(self.deltaY * FEEL.wheel),
        onChangeX: (self) => nudge(self.deltaX * FEEL.wheel),
      });
    } else {
      strip.addEventListener("wheel", (e) => {
        e.preventDefault();
        nudge((Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX) * FEEL.wheel);
      }, { passive: false });
    }
  }

  // ── Ambient canvas ──────────────────────────────────────────────────────

  /** A drifting glyph field that leans with scroll velocity. Cheap, and it is
   *  what keeps the space between panels from reading as empty black. */
  function makeFx(canvas) {
    const ctx = canvas.getContext("2d");
    const GLYPHS = ["♟", "♞", "♝", "♜", "♛", "♚"];
    let dpr = 1;
    let parts = [];

    function resize() {
      dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = cineEl.clientWidth * dpr;
      canvas.height = cineEl.clientHeight * dpr;
      canvas.style.width = `${cineEl.clientWidth}px`;
      canvas.style.height = `${cineEl.clientHeight}px`;
      const n = Math.round((cineEl.clientWidth * cineEl.clientHeight) / 52000);
      parts = Array.from({ length: clamp(n, 12, 34) }, () => ({
        x: Math.random(), y: Math.random(),
        z: 0.3 + Math.random() * 0.9,
        g: GLYPHS[(Math.random() * GLYPHS.length) | 0],
        r: (Math.random() - 0.5) * 0.5,
        vr: (Math.random() - 0.5) * 0.0015,
        s: 22 + Math.random() * 44,
        a: 0.018 + Math.random() * 0.032,
      }));
    }

    function step(vel) {
      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (const p of parts) {
        p.x -= (vel * 0.00042 * p.z) + 0.00012 * p.z;
        p.r += p.vr;
        if (p.x < -0.08) p.x += 1.16;
        if (p.x > 1.08) p.x -= 1.16;
        ctx.save();
        ctx.translate(p.x * w, p.y * h);
        ctx.rotate(p.r);
        ctx.globalAlpha = p.a * clamp(p.z, 0.3, 1);
        ctx.fillStyle = p.z > 0.9 ? "#8bff70" : "#cfe6ff";
        ctx.font = `${p.s * p.z * dpr}px "Segoe UI Symbol", serif`;
        ctx.fillText(p.g, 0, 0);
        ctx.restore();
      }
    }

    resize();
    return { step, resize };
  }

  // ══════════════════════════════════════════════════════════════════════
  // Open / close / atlas hooks
  // ══════════════════════════════════════════════════════════════════════

  function teardown() {
    if (F.raf) cancelAnimationFrame(F.raf);
    F.raf = null;
    if (F.observer) { F.observer.kill(); F.observer = null; }
    if (F.idleTimer) clearTimeout(F.idleTimer);
    F.panels.forEach((p) => {
      if (p.tl) p.tl.kill();
      if (p.el && p.el._split) { try { p.el._split.revert(); } catch { /* ignore */ } }
    });
    F.panels = [];
    destroyGrounds();
  }

  function start(metrics, meta, opts) {
    F.metrics = metrics;
    F.meta = meta || {};
    teardown();
    F.panels = buildPanels();
    if (!F.panels.length) return false;

    F.pos = 0;
    F.target = 0;
    F.vel = 0;
    F.nearest = -1;
    F.mode = "play";
    F.speedIdx = 0;
    setPlaying(true);
    updateSpeedBtn();

    const canvas = $("#cine-fx", cineEl);
    if (canvas && !reduced()) {
      F.fx = makeFx(canvas);
      canvas.style.display = "";
    } else if (canvas) {
      canvas.style.display = "none";
      F.fx = null;
    }

    mountStrip();
    bindInput();
    if (opts && isNum(opts.at)) goPanel(Number(opts.at), { keepPlaying: true });
    F.lastT = 0;
    F.raf = requestAnimationFrame(frame);
    return true;
  }

  function updateSpeedBtn() {
    const b = $("#cine-speed", cineEl);
    if (b) b.textContent = `${SPEEDS[F.speedIdx]}×`;
  }

  function cycleSpeed() {
    F.speedIdx = (F.speedIdx + 1) % SPEEDS.length;
    updateSpeedBtn();
    if (!F.playing) setPlaying(true);
  }

  function onResize() {
    if (F.mode !== "play" || !F.panels.length) return;
    measure();
    if (F.fx) F.fx.resize();
    paint();
  }

  function showAtlas() {
    if (F.mode === "atlas") return;
    F.mode = "atlas";
    setPlaying(false);
    api.showAtlas();
  }

  // ── Exposed to the shell half of cinema.js ──────────────────────────────

  window.__insightsFilm = {
    start,
    teardown,
    resize: onResize,
    goPanel,
    setMode: (m) => {
      F.mode = m;
      if (m === "play") { F.lastT = 0; setPlaying(true); }
    },
    togglePlay: () => setPlaying(!F.playing),
    cycleSpeed,
    nudge: (dx) => nudge(dx),
    step: (dir) => goPanel((F.nearest || 0) + dir),
    panels: () => F.panels.map((p) => ({ id: p.id, chapter: p.chapter })),
    debug: () => ({ pos: F.pos, target: F.target, max: F.max, vw: F.vw, vel: F.vel,
                    playing: F.playing, nearest: F.nearest, speed: SPEEDS[F.speedIdx] }),
    // Land the strip on its target immediately and cancel the pending
    // autoplay-resume, so a harness can sample a still frame without the
    // cruise drifting it out from under the screenshot.
    settle: () => {
      if (F.idleTimer) { clearTimeout(F.idleTimer); F.idleTimer = null; }
      F.pos = F.target;
      paint();
    },
    mode: () => F.mode,
    isPlaying: () => F.playing,
  };
})();
