// ChessMax home screen.
//
// Presentational only — every navigation goes back through
// `window.__shellNavigate`, so the shell stays the single owner of routing.
//
// The centrepiece is `replay`: a real game (Morphy's Opera Game) played out on
// a real board, with the volatility / findability / win% readouts stepping
// through the numbers ChessMax actually computed for it. The data lives in
// `opera.js`; nothing here invents a metric.

const root = document.getElementById("home-root");

if (root) {
  // Chromium on Windows reports `prefers-reduced-motion: reduce` whenever
  // window animations are off (`MinAnimate=0` — a common performance tweak,
  // not a vestibular request). Honouring that froze every CSS animation and
  // paused the Opera replay in Chrome/Edge, while Cursor's preview (which
  // does not inherit the flag) looked alive. Motion is the point of this
  // page; the replay toggle still pauses the board if you want it still.
  const reduceMotion = false;
  const $ = (id) => document.getElementById(id);

  // Opting the page into its entry animations. Nothing in the stylesheet hides
  // content until this class is on, so a script that never runs leaves a fully
  // painted page behind rather than an empty one.
  if (!reduceMotion) root.classList.add("hm-anim");

  let homeActive = false;

  // ── Ambient 8×8 board ────────────────────────────────────────────────
  // A knight tours it, forever. Random blinking squares are noise; a knight's
  // tour is a real object — it visits all 64 exactly once, so the trail sweeps
  // the whole board without ever repeating a shape.
  const bgBoard = $("hmBoard");
  const bgTiles = [];
  let bgKnight = null;

  if (bgBoard && !bgBoard.childElementCount) {
    const frag = document.createDocumentFragment();
    for (let i = 0; i < 64; i += 1) {
      const rank = Math.floor(i / 8);
      const file = i % 8;
      const tile = document.createElement("div");
      tile.className = (rank + file) % 2 ? "hm-tile dark" : "hm-tile";
      tile.style.setProperty("--d", String(rank + file));
      bgTiles.push(tile);
      frag.appendChild(tile);
    }
    bgBoard.appendChild(frag);

    if (!reduceMotion) {
      // `.cg-wrap` is here purely to pull in the vendored cburnett sprite —
      // the same reason the hero board carries it.
      bgKnight = document.createElement("div");
      bgKnight.className = "hm-bg-knight cg-wrap";
      // Black, not white: the knight always stands on the brightest square in
      // the trail, so a silhouette is the only version of it that reads.
      const piece = document.createElement("piece");
      piece.className = "knight black";
      bgKnight.appendChild(piece);
      bgBoard.appendChild(bgKnight);
    }
  }

  const KNIGHT_HOPS = [[1, 2], [2, 1], [-1, 2], [-2, 1], [1, -2], [2, -1], [-1, -2], [-2, -1]];

  // Warnsdorff's rule: always hop to the square with the fewest onward moves.
  // From a1 on an 8×8 that reaches all 64 without backtracking, which is why
  // there is no search here — if it ever came up short the walk would simply
  // be shorter, never wrong.
  function knightTour(start) {
    const seen = new Set([start]);
    const path = [start];
    let at = start;
    while (path.length < 64) {
      let next = -1;
      let fewest = 9;
      for (const [dr, dc] of KNIGHT_HOPS) {
        const r = Math.floor(at / 8) + dr;
        const c = (at % 8) + dc;
        if (r < 0 || r > 7 || c < 0 || c > 7 || seen.has(r * 8 + c)) continue;
        let onward = 0;
        for (const [dr2, dc2] of KNIGHT_HOPS) {
          const r2 = r + dr2;
          const c2 = c + dc2;
          if (r2 >= 0 && r2 < 8 && c2 >= 0 && c2 < 8 && !seen.has(r2 * 8 + c2)) onward += 1;
        }
        if (onward < fewest) { fewest = onward; next = r * 8 + c; }
      }
      if (next < 0) break;
      seen.add(next);
      path.push(next);
      at = next;
    }
    return path;
  }

  const TOUR = knightTour(0);
  const TOUR_TRAIL = 14;
  const TOUR_BEAT = 520;
  const tourTrail = [];
  let tourAt = 0;
  let tourDir = 1;
  let tourTimer = 0;

  function tourStep() {
    const here = TOUR[tourAt];
    tourTrail.push(here);
    if (tourTrail.length > TOUR_TRAIL) {
      const faded = tourTrail.shift();
      // Reversing at the end of the tour re-treads squares, so only darken one
      // the trail has genuinely left behind.
      if (!tourTrail.includes(faded) && bgTiles[faded]) bgTiles[faded].style.setProperty("--g", "0");
    }
    tourTrail.forEach((idx, k) => {
      // Squared so the tail falls away fast and the knight's own square is
      // clearly the brightest thing on the board.
      const g = (k + 1) / tourTrail.length;
      if (bgTiles[idx]) bgTiles[idx].style.setProperty("--g", (g * g).toFixed(3));
    });
    if (bgKnight) {
      bgKnight.style.setProperty("--kx", String(here % 8));
      bgKnight.style.setProperty("--ky", String(Math.floor(here / 8)));
    }
    // Walking the tour backwards is still a sequence of legal knight moves, so
    // reversing at each end loops forever without the knight ever teleporting.
    if (tourAt >= TOUR.length - 1) tourDir = -1;
    else if (tourAt <= 0) tourDir = 1;
    tourAt += tourDir;
  }

  function startTour() {
    if (tourTimer || reduceMotion || !bgTiles.length) return;
    tourStep();
    tourTimer = window.setInterval(tourStep, TOUR_BEAT);
  }

  function stopTour() {
    if (tourTimer) window.clearInterval(tourTimer);
    tourTimer = 0;
  }

  // ── The frame loop ───────────────────────────────────────────────────
  // Pointer lamp, scroll progress, background parallax and the ticker all come
  // off one rAF tick. A single loop is what keeps the page cheap: every writer
  // touches compositor-only properties, and the one genuinely expensive read
  // (`scrollHeight`, which forces layout) is refreshed twice a second rather
  // than every frame.
  const lamp = $("hmLamp");
  const progress = $("hmProgress");
  const tickerTrack = root.querySelector(".hm-ticker-track");

  let lampX = 0;
  let lampY = 0;
  let rafId = 0;
  let looping = false;
  let lastFrameAt = 0;
  let docMax = 1;
  let docMaxAge = 0;

  // Ticker state: a base drift, plus whatever the page's own scroll velocity
  // adds, minus a near-stop while the pointer is over it.
  let tickerX = 0;
  let tickerSpan = 0;
  let tickerHover = false;
  let tickerGain = 1;
  let scrollVel = 0;
  let lastScrollY = 0;

  function measureTicker() {
    // The strip is authored as the same run of items twice, so half of it is
    // exactly one period.
    if (tickerTrack) tickerSpan = tickerTrack.scrollWidth / 2;
  }

  function paint(dt) {
    if (lamp) lamp.style.transform = `translate3d(${lampX}px, ${lampY}px, 0)`;

    const y = window.scrollY;
    docMaxAge -= dt;
    if (docMaxAge <= 0) {
      docMax = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
      docMaxAge = 500;
    }
    if (progress) progress.style.setProperty("--p", Math.min(1, y / docMax).toFixed(4));

    if (reduceMotion) return;

    const dy = y - lastScrollY;
    lastScrollY = y;
    scrollVel += (Math.abs(dy) - scrollVel) * 0.14;

    if (bgBoard) {
      bgBoard.style.setProperty("--bgy", `${(y * -0.09).toFixed(1)}px`);
      bgBoard.style.setProperty("--bgr", `${(y * 0.007).toFixed(2)}deg`);
    }
    root.style.setProperty("--hero-p", Math.min(1, y / (window.innerHeight * 0.9)).toFixed(3));

    if (tickerTrack && tickerSpan > 0) {
      tickerGain += ((tickerHover ? 0.06 : 1) - tickerGain) * 0.09;
      const speed = (34 + Math.min(240, scrollVel * 9)) * tickerGain;
      tickerX -= (speed * dt) / 1000;
      if (tickerX <= -tickerSpan) tickerX += tickerSpan;
      tickerTrack.style.setProperty("--x", `${tickerX.toFixed(1)}px`);
    }
  }

  function tick(now) {
    if (!looping) return;
    const dt = lastFrameAt ? Math.min(64, now - lastFrameAt) : 16;
    lastFrameAt = now;
    paint(dt);
    rafId = requestAnimationFrame(tick);
  }

  function startLoop() {
    if (looping) return;
    looping = true;
    lastFrameAt = 0;
    lastScrollY = window.scrollY;
    docMaxAge = 0;
    measureTicker();
    if (tickerTrack) tickerTrack.classList.add("is-live");
    rafId = requestAnimationFrame(tick);
  }

  function stopLoop() {
    looping = false;
    if (rafId) cancelAnimationFrame(rafId);
    rafId = 0;
  }

  if (!reduceMotion) {
    window.addEventListener("pointermove", (event) => {
      lampX = event.clientX;
      lampY = event.clientY;
      if (lamp) lamp.classList.add("on");
    }, { passive: true });

    const ticker = root.querySelector(".hm-ticker");
    if (ticker) {
      ticker.addEventListener("pointerenter", () => { tickerHover = true; });
      ticker.addEventListener("pointerleave", () => { tickerHover = false; });
    }
  }

  // ── Scroll reveal ────────────────────────────────────────────────────
  // `#home-root` starts hidden, so elements measure as out-of-viewport until
  // the route activates. `revealAll` is therefore also called from setActive.
  const revealTargets = () => Array.from(root.querySelectorAll(".hm-reveal"));
  let revealObserver = null;

  // Section kickers ("01 — The toolkit") resolve out of noise the first time
  // they are read. They are short fixed strings with no markup inside, so the
  // effect can rewrite textContent directly.
  const DECODE_GLYPHS = "ABCDEFGHKMNQRSTUVWXYZ0123456789+#=×τ";
  const decoded = new WeakSet();

  function decodeKicker(el) {
    if (decoded.has(el) || reduceMotion) return;
    decoded.add(el);
    const text = el.textContent;
    const frames = 24;
    let frame = 0;
    el.classList.add("is-typing");
    const run = () => {
      const settled = Math.floor((frame / frames) * text.length);
      let out = "";
      for (let i = 0; i < text.length; i += 1) {
        const ch = text[i];
        out += i < settled || ch === " " ? ch : DECODE_GLYPHS[(Math.random() * DECODE_GLYPHS.length) | 0];
      }
      el.textContent = out;
      frame += 1;
      if (frame <= frames) {
        window.setTimeout(run, 32);
      } else {
        el.textContent = text;
        el.classList.remove("is-typing");
      }
    };
    run();
  }

  function reveal(el) {
    el.classList.add("is-in");
    el.querySelectorAll(".hm-kicker").forEach(decodeKicker);
  }

  function revealAll() {
    revealTargets().forEach((el) => el.classList.add("is-in"));
  }

  if (typeof IntersectionObserver === "function" && !reduceMotion) {
    revealObserver = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          reveal(entry.target);
          revealObserver.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.08 },
    );
    revealTargets().forEach((el) => revealObserver.observe(el));
  } else {
    revealAll();
  }

  // Per-letter stagger on the wordmark.
  root.querySelectorAll(".hm-title-word > span").forEach((el, i) => {
    el.style.setProperty("--i", String(i));
  });

  // ── Cursor spotlight and tilt on cards ───────────────────────────────
  // One pointer read feeds four custom properties: the spotlight and the
  // follow-the-cursor border (`--mx`/`--my`, in percent), the card's own tilt
  // (`--rx`/`--ry`) and a counter-shift on the icon (`--px`/`--py`) that gives
  // the card a near plane. Everything is composited; nothing here lays out.
  const TILT = 5.5;

  root.querySelectorAll(".hm-card").forEach((card) => {
    if (reduceMotion) return;
    card.addEventListener("pointermove", (event) => {
      const rect = card.getBoundingClientRect();
      const nx = (event.clientX - rect.left) / rect.width;
      const ny = (event.clientY - rect.top) / rect.height;
      card.style.setProperty("--mx", `${(nx * 100).toFixed(1)}%`);
      card.style.setProperty("--my", `${(ny * 100).toFixed(1)}%`);
      card.style.setProperty("--ry", `${((nx - 0.5) * 2 * TILT).toFixed(2)}deg`);
      card.style.setProperty("--rx", `${((0.5 - ny) * 2 * TILT).toFixed(2)}deg`);
      card.style.setProperty("--px", `${((nx - 0.5) * -12).toFixed(1)}px`);
      card.style.setProperty("--py", `${((ny - 0.5) * -9).toFixed(1)}px`);
    }, { passive: true });

    card.addEventListener("pointerleave", () => {
      card.style.setProperty("--rx", "0deg");
      card.style.setProperty("--ry", "0deg");
      card.style.setProperty("--px", "0px");
      card.style.setProperty("--py", "0px");
    });
  });

  // ── Click ripple ─────────────────────────────────────────────────────
  // The buttons already clip their overflow, so the circle only has to be big
  // enough to cover the far corner from wherever it was pressed.
  root.querySelectorAll(".hm-btn").forEach((btn) => {
    if (reduceMotion) return;
    btn.addEventListener("pointerdown", (event) => {
      const rect = btn.getBoundingClientRect();
      const size = Math.hypot(rect.width, rect.height) * 2;
      const ink = document.createElement("span");
      ink.className = "hm-ripple";
      ink.style.width = `${size}px`;
      ink.style.height = `${size}px`;
      ink.style.left = `${event.clientX - rect.left}px`;
      ink.style.top = `${event.clientY - rect.top}px`;
      btn.appendChild(ink);
      window.setTimeout(() => ink.remove(), 660);
    });
  });

  // ── Magnetic buttons ─────────────────────────────────────────────────
  // A small pull toward the cursor. Capped well under the button's own
  // padding so the label never leaves its box.
  const MAGNET = 6;
  root.querySelectorAll("[data-magnetic]").forEach((btn) => {
    if (reduceMotion) return;
    btn.addEventListener("pointermove", (event) => {
      const rect = btn.getBoundingClientRect();
      const dx = (event.clientX - rect.left) / rect.width - 0.5;
      const dy = (event.clientY - rect.top) / rect.height - 0.5;
      btn.style.transform = `translate(${(dx * MAGNET * 2).toFixed(1)}px, ${(dy * MAGNET).toFixed(1)}px)`;
    }, { passive: true });
    btn.addEventListener("pointerleave", () => { btn.style.transform = ""; });
  });

  // ── Navigation ───────────────────────────────────────────────────────
  root.querySelectorAll("[data-home-go]").forEach((el) => {
    el.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const path = el.dataset.homeGo;
      if (path && window.__shellNavigate) window.__shellNavigate(path, { push: true });
    });
  });

  // ── Count-up stats ───────────────────────────────────────────────────
  const counters = Array.from(root.querySelectorAll("[data-count-to]"));
  const counted = new WeakSet();

  function formatCount(value, decimals, suffix) {
    const shown = decimals > 0 ? value.toFixed(decimals) : Math.round(value).toLocaleString("en-US");
    return `${shown}${suffix}`;
  }

  function runCounter(el) {
    if (counted.has(el)) return;
    counted.add(el);
    const target = Number(el.dataset.countTo || 0);
    const decimals = Number(el.dataset.decimals || 0);
    const suffix = el.dataset.suffix || "";
    if (reduceMotion || !target) {
      el.textContent = formatCount(target, decimals, suffix);
      return;
    }
    const duration = 1400;
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      // easeOutExpo: fast to a near-final value, then settles — reads as a
      // readout locking on rather than a linear ramp.
      const eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
      el.textContent = formatCount(target * eased, decimals, suffix);
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function maybeCount() {
    counters.forEach((el) => {
      const rect = el.getBoundingClientRect();
      if (rect.top < window.innerHeight - 40 && rect.bottom > 0) runCounter(el);
    });
  }

  window.addEventListener("scroll", maybeCount, { passive: true });

  // ── Shared animation helpers ─────────────────────────────────────────

  // Restart a one-shot CSS animation. Removing the class and reading a layout
  // property in between is the only reliable way to force the restart; without
  // the read the browser coalesces both writes and nothing happens.
  function restartAnim(el, cls, ms) {
    if (!el || reduceMotion) return;
    el.classList.remove(cls);
    void el.offsetWidth;
    el.classList.add(cls);
    if (ms) window.setTimeout(() => el.classList.remove(cls), ms);
  }

  // Readouts count to their new value instead of snapping to it. Three numbers
  // changing at once is the moment the panel either feels instrumented or
  // feels like a slideshow.
  function tweenNumber(el, from, to, { decimals = 0, suffix = "", duration = 460 } = {}) {
    if (!el) return;
    if (el.__hmTween) cancelAnimationFrame(el.__hmTween);
    const write = (v) => { el.textContent = `${v.toFixed(decimals)}${suffix}`; };
    // A background tab runs no rAF callbacks, so a tween scheduled there would
    // never write anything at all — the readout would sit on an em dash until
    // the next beat. Land on the value instead.
    if (reduceMotion || from === to || document.hidden) { write(to); return; }
    const started = performance.now();
    const run = (now) => {
      const t = Math.min(1, (now - started) / duration);
      write(from + (to - from) * (1 - Math.pow(1 - t, 3)));
      el.__hmTween = t < 1 ? requestAnimationFrame(run) : 0;
    };
    el.__hmTween = requestAnimationFrame(run);
  }

  // ══════════════════════════════════════════════════════════════════════
  //  The hero board
  // ══════════════════════════════════════════════════════════════════════

  const GAME = window.HM_OPERA;
  const BACK_RANK = ["rook", "knight", "bishop", "queen", "king", "bishop", "knight", "rook"];

  // Cool → hot, keyed on the upper edge of each band. Used for volatility
  // directly and for findability inverted, so a dangerous position and an
  // unfindable move are the same red.
  const RAMP = [
    [25, "#7bf765"], [45, "#c6ee4a"], [62, "#f2c84a"], [80, "#ff9d4a"], [101, "#ff6358"],
  ];
  const toneFor = (v) => (RAMP.find(([edge]) => v < edge) || RAMP[RAMP.length - 1])[1];

  // 0 = a1 … 63 = h8, matching UCI's file-then-rank ordering.
  const sqIndex = (name) => (name.charCodeAt(1) - 49) * 8 + (name.charCodeAt(0) - 97);

  const gridEl = $("hmBoardGrid");
  const piecesEl = $("hmPieces");

  if (gridEl && !gridEl.childElementCount) {
    const frag = document.createDocumentFragment();
    // Rendered top-left → bottom-right, i.e. a8 first, so white is at the foot.
    for (let row = 0; row < 8; row += 1) {
      for (let col = 0; col < 8; col += 1) {
        const sq = document.createElement("div");
        sq.className = (row + col) % 2 ? "hm-sq dark" : "hm-sq";
        frag.appendChild(sq);
      }
    }
    gridEl.appendChild(frag);
  }

  // `translate(file, 7 - rank)` in whole-square units; a <piece> is 12.5% wide,
  // so 100% of its own box is exactly one square.
  function squareTransform(sq) {
    const file = sq % 8;
    const rank = Math.floor(sq / 8);
    return `translate(${file * 100}%, ${(7 - rank) * 100}%)`;
  }

  const replay = (() => {
    if (!GAME || !piecesEl) return null;

    const hlFrom = $("hmHlFrom");
    const hlTo = $("hmHlTo");
    const evalFill = $("hmEvalFill");
    const volVal = $("hmVolVal");
    const volNote = $("hmVolNote");
    const gauge = $("hmGauge");
    const findVal = $("hmFindVal");
    const findBand = $("hmFindBand");
    const findFill = $("hmFindFill");
    const findBest = $("hmFindBest");
    const winVal = $("hmWinVal");
    const moveNo = $("hmMoveNo");
    const spineLine = $("hmSpineLine");
    const spineArea = $("hmSpineArea");
    const spineHead = $("hmSpineHead");
    const moveRow = $("hmMoveRow");
    const toggle = $("hmPlayToggle");
    const live = $("hmLiveText");
    const stage = $("hmStage");
    const spineHalo = $("hmSpineHalo");
    const think = $("hmThink");
    const arrow = $("hmArrow");
    const arrowShaft = $("hmArrowShaft");
    const arrowHead = $("hmArrowHead");
    const impact = $("hmImpact");
    const boardFlash = $("hmFlash");

    const plies = GAME.plies;
    const board = new Array(64).fill(null);
    let moveTrack = null;
    let chips = [];
    let index = 0;
    let timer = 0;
    // Someone who has asked for reduced motion does not want a board animating
    // itself the moment the page opens. The replay is still there — it just
    // waits to be asked, and every move remains reachable from the move list.
    let paused = reduceMotion;

    // ── Board setup ────────────────────────────────────────────────────
    function spawn(type, color, sq) {
      const el = document.createElement("piece");
      el.className = `${type} ${color}`;
      const t = squareTransform(sq);
      el.style.setProperty("--t", t);
      el.style.transform = t;
      piecesEl.appendChild(el);
      board[sq] = { el, type, color, sq };
    }

    function reset() {
      piecesEl.textContent = "";
      board.fill(null);
      BACK_RANK.forEach((type, file) => {
        spawn(type, "white", file);
        spawn(type, "black", 56 + file);
      });
      for (let file = 0; file < 8; file += 1) {
        spawn("pawn", "white", 8 + file);
        spawn("pawn", "black", 48 + file);
      }
      if (hlFrom) hlFrom.classList.remove("on");
      if (hlTo) hlTo.classList.remove("on");
    }

    function place(piece, sq) {
      const t = squareTransform(sq);
      piece.el.style.setProperty("--t", t);
      piece.el.style.transform = t;
      board[piece.sq] = null;
      board[sq] = piece;
      piece.sq = sq;
    }

    // The Opera Game contains one castle and no en passant or promotion, so
    // those are the only special cases this needs to know about.
    //
    // `effects` is off while a seek rebuilds the position: replaying twenty
    // impact rings and a check flash in one frame is not a jump cut, it is a
    // strobe.
    function applyMove(ply, effects = true) {
      const uci = ply.uci;
      const from = sqIndex(uci.slice(0, 2));
      const to = sqIndex(uci.slice(2, 4));
      const mover = board[from];
      if (!mover) return;

      const taken = board[to];
      if (taken) {
        board[to] = null;
        taken.el.classList.add("hm-gone");
        const el = taken.el;
        window.setTimeout(() => el.remove(), 420);
      }
      place(mover, to);

      if (mover.type === "king" && Math.abs((to % 8) - (from % 8)) === 2) {
        const rank = Math.floor(from / 8) * 8;
        const queenside = to % 8 === 2;
        const rook = board[rank + (queenside ? 0 : 7)];
        if (rook) place(rook, rank + (queenside ? 3 : 5));
      }

      if (hlFrom) {
        hlFrom.style.transform = squareTransform(from);
        hlFrom.classList.add("on");
      }
      if (hlTo) {
        hlTo.style.transform = squareTransform(to);
        hlTo.classList.add("on");
      }

      if (!effects) return;

      hideArrow();
      if (impact) {
        impact.style.setProperty("--t", squareTransform(to));
        impact.style.setProperty("--ring", taken ? "rgba(255, 99, 88, 0.95)" : "rgba(123, 247, 101, 0.9)");
        restartAnim(impact, "go", 640);
      }
      // Check and mate come straight off the SAN suffix, so the board flashes
      // on exactly the plies a scoresheet marks.
      if (boardFlash && /[+#]/.test(ply.san)) {
        boardFlash.style.setProperty("--flash", ply.san.includes("#") ? "#ff6358" : "#f2c84a");
        restartAnim(boardFlash, "go", 920);
      }
    }

    // ── The move about to be played ────────────────────────────────────
    // Drawn during the analysis beat and retracted as the piece sets off, so
    // each move is announced before it happens rather than just appearing.
    // One SVG unit is one square; the board is square, so no scaling is needed.
    function showArrow(uci) {
      if (!arrow || !arrowShaft || !arrowHead || reduceMotion) return;
      const centre = (sq) => [(sq % 8) + 0.5, 7 - Math.floor(sq / 8) + 0.5];
      const [x1, y1] = centre(sqIndex(uci.slice(0, 2)));
      const [x2, y2] = centre(sqIndex(uci.slice(2, 4)));
      const len = Math.hypot(x2 - x1, y2 - y1) || 1;
      const ux = (x2 - x1) / len;
      const uy = (y2 - y1) / len;
      const tipX = x2 - ux * 0.14;
      const tipY = y2 - uy * 0.14;
      const baseX = tipX - ux * 0.34;
      const baseY = tipY - uy * 0.34;

      arrowShaft.setAttribute("d", `M${x1} ${y1} L${baseX.toFixed(3)} ${baseY.toFixed(3)}`);
      arrowShaft.style.setProperty("--len", len.toFixed(3));
      arrowHead.setAttribute("d", [
        `M${tipX.toFixed(3)} ${tipY.toFixed(3)}`,
        `L${(baseX - uy * 0.17).toFixed(3)} ${(baseY + ux * 0.17).toFixed(3)}`,
        `L${(baseX + uy * 0.17).toFixed(3)} ${(baseY - ux * 0.17).toFixed(3)}`,
        "Z",
      ].join(" "));

      arrow.classList.remove("on");
      void arrow.getBoundingClientRect();
      arrow.classList.add("on");
    }

    function hideArrow() {
      if (arrow) arrow.classList.remove("on");
    }

    // ── Move list ──────────────────────────────────────────────────────
    // Real buttons, not spans: clicking one seeks the board to that move, so
    // the panel is a thing you can drive rather than a thing you watch.
    function buildMoveList() {
      if (!moveRow) return;
      moveRow.textContent = "";
      moveTrack = document.createElement("div");
      moveTrack.className = "hm-moverow-track";
      chips = plies.map((ply, i) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "hm-mv";
        chip.title = `Jump to ${ply.san}`;
        chip.innerHTML = i % 2 === 0
          ? `<b class="hm-mv-n">${i / 2 + 1}.</b>${ply.san}`
          : ply.san;
        chip.addEventListener("click", () => seek(i, { pause: true }));
        moveTrack.appendChild(chip);
        return chip;
      });
      moveRow.appendChild(moveTrack);
    }

    function markMove(i) {
      chips.forEach((chip, j) => {
        chip.classList.toggle("now", j === i);
        chip.classList.toggle("played", j < i);
      });
      if (!moveTrack || !chips[i]) return;
      // Keep the current move about a third of the way in, so the reader sees
      // both what just happened and what is coming.
      const offset = Math.max(0, chips[i].offsetLeft - moveRow.clientWidth * 0.32);
      moveTrack.style.transform = `translateX(${-offset}px)`;
    }

    // ── Readouts ───────────────────────────────────────────────────────
    const roVol = volVal && volVal.closest(".hm-ro");
    const roFind = findVal && findVal.closest(".hm-ro");

    function volCaption(v) {
      if (v >= 80) return "one move decides it";
      if (v >= 55) return "sharp — punishable";
      if (v >= 30) return "a real choice here";
      return "nothing much is at stake";
    }

    // Previous values, so each readout can be tweened from where it was and
    // can flare only when the swing is worth looking at.
    let shownVol = 0;
    let shownFind = 0;
    let shownWin = 50;

    function setReadouts(ply, i) {
      const vol = ply.vol;
      const hasVol = typeof vol === "number";
      const tone = hasVol ? toneFor(vol) : "#6d7783";
      if (volVal) {
        if (hasVol) {
          if (Math.abs(vol - shownVol) >= 8) restartAnim(volVal, "hm-tick", 620);
          tweenNumber(volVal, shownVol, Math.round(vol));
          shownVol = Math.round(vol);
        } else {
          volVal.textContent = "—";
        }
      }
      if (roVol) roVol.style.setProperty("--tone", tone);
      if (gauge) gauge.style.setProperty("--v", hasVol ? (vol / 100).toFixed(3) : "0");
      if (volNote) volNote.textContent = hasVol ? volCaption(vol) : "forced — no choice to make";

      // The panel itself takes the colour of the position: the rotating frame,
      // the inner haze, the arrow and the volatility trace all read `--tone`
      // and `--heat` off the stage, so a sharp position is visible from across
      // the room without a single new element appearing.
      if (stage) {
        stage.style.setProperty("--tone", tone);
        stage.style.setProperty("--heat", hasVol ? Math.min(1, vol / 85).toFixed(3) : "0");
      }

      const find = ply.find;
      const hasFind = typeof find === "number";
      if (findVal) {
        if (hasFind) {
          if (Math.abs(find - shownFind) >= 12) restartAnim(findVal, "hm-tick", 620);
          tweenNumber(findVal, shownFind, find);
          shownFind = find;
        } else {
          findVal.textContent = "—";
        }
      }
      // Inverted: a *low* findability is the alarming one.
      if (roFind) roFind.style.setProperty("--tone", hasFind ? toneFor(100 - find) : "#6d7783");
      if (findBand) {
        findBand.textContent = ply.band || "—";
        findBand.dataset.band = ply.band || "";
      }
      if (findFill) findFill.style.setProperty("--v", hasFind ? (find / 100).toFixed(3) : "0");
      if (findBest) findBest.textContent = ply.best || "—";

      if (winVal) {
        if (Math.abs(ply.win - shownWin) >= 2) restartAnim(winVal, "hm-tick", 620);
        tweenNumber(winVal, shownWin, ply.win, { suffix: "%" });
        shownWin = ply.win;
      }
      if (evalFill) evalFill.style.setProperty("--h", `${ply.win.toFixed(1)}%`);
      if (moveNo) {
        moveNo.textContent = `${i % 2 === 0 ? "White" : "Black"} to play, move ${Math.floor(i / 2) + 1}`;
      }
      drawSpine(i);
    }

    // ── Volatility trace ───────────────────────────────────────────────
    // Drawn progressively, so the curve is written by the game rather than
    // sitting there finished before the first move.
    const W = 320;
    const H = 46;
    const spineX = (i) => (i / (plies.length - 1)) * W;
    const spineY = (v) => H - 3 - (Math.max(0, Math.min(100, v)) / 100) * (H - 8);

    function drawSpine(upto) {
      if (!spineLine) return;
      let last = 0;
      const points = [];
      for (let i = 0; i <= upto && i < plies.length; i += 1) {
        const v = typeof plies[i].vol === "number" ? plies[i].vol : last;
        last = v;
        points.push([spineX(i), spineY(v)]);
      }
      if (!points.length) return;
      const d = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
      spineLine.setAttribute("d", d);
      if (spineArea) {
        const [lx] = points[points.length - 1];
        spineArea.setAttribute("d", `${d} L${lx.toFixed(1)} ${H} L0 ${H} Z`);
      }
      const [hx, hy] = points[points.length - 1];
      [spineHead, spineHalo].forEach((el) => {
        if (!el) return;
        el.setAttribute("cx", hx.toFixed(1));
        el.setAttribute("cy", hy.toFixed(1));
      });
    }

    // ── Loop ───────────────────────────────────────────────────────────
    const BEAT = reduceMotion ? 2600 : 1550;
    // How long the numbers sit on screen before the move they describe is
    // played. Every metric on this panel is a property of the position
    // *before* the move, so the readouts and the board have to be advanced
    // together — showing ply N's volatility over ply N's resulting position
    // would be quietly wrong for two thirds of every beat.
    const DWELL = 560;

    // The analysis beat, drawn as a line filling across the top of the panel.
    // Timed from JS rather than authored in CSS so the bar and the replay
    // clock cannot drift apart if `BEAT` or `DWELL` ever change.
    function runThink(ms) {
      if (!think || reduceMotion) return;
      think.style.transition = "none";
      think.style.transform = "scaleX(0)";
      void think.offsetWidth;
      think.style.transition = `transform ${ms}ms linear`;
      think.style.transform = "scaleX(1)";
    }

    function clearThink() {
      if (!think) return;
      think.style.transition = "transform 0.3s ease";
      think.style.transform = "scaleX(0)";
    }

    function showAt(i) {
      setReadouts(plies[i], i);
      markMove(i);
    }

    function step() {
      runThink(DWELL);
      showArrow(plies[index].uci);
      timer = window.setTimeout(() => {
        applyMove(plies[index]);
        index += 1;
        if (index >= plies.length) {
          // Hold the mate, then start over.
          clearThink();
          timer = window.setTimeout(() => {
            index = 0;
            reset();
            showAt(0);
            step();
          }, 3400);
          return;
        }
        showAt(index);
        timer = window.setTimeout(step, BEAT - DWELL);
      }, DWELL);
    }

    function stop() {
      if (timer) window.clearTimeout(timer);
      timer = 0;
      clearThink();
      hideArrow();
    }

    function start() {
      stop();
      if (paused) return;
      step();
    }

    function paintPausedState() {
      if (toggle) {
        toggle.setAttribute("aria-pressed", paused ? "true" : "false");
        toggle.setAttribute("aria-label", paused ? "Resume replay" : "Pause replay");
      }
      if (live) live.textContent = paused ? "Paused" : "Replaying";
      if (stage) stage.classList.toggle("is-paused", paused);
    }

    function setPaused(next) {
      paused = next;
      paintPausedState();
      if (paused) stop();
      else start();
    }

    // Jump straight to a ply. The position is rebuilt from the start rather
    // than stepped backwards — 33 plies is nothing, and it keeps `applyMove`
    // as the single place that knows how a move changes the board. Animation
    // is suppressed for the rebuild so the jump reads as a cut, not as 30
    // pieces sliding at once.
    function seek(i, { pause = false } = {}) {
      stop();
      const boardEl = piecesEl.parentElement;
      boardEl.classList.add("hm-instant");
      reset();
      for (let k = 0; k < i; k += 1) applyMove(plies[k], false);
      void boardEl.offsetWidth;
      boardEl.classList.remove("hm-instant");
      index = i;
      showAt(i);
      if (pause) setPaused(true);
      else start();
    }

    reset();
    buildMoveList();
    showAt(0);
    paintPausedState();

    if (toggle) toggle.addEventListener("click", () => setPaused(!paused));

    return { start, stop, isPaused: () => paused };
  })();

  // ── Game Review card: the same volatility series, as columns ─────────
  const colsEl = $("hmVizCols");
  if (colsEl && GAME && !colsEl.childElementCount) {
    const series = GAME.plies.map((p) => (typeof p.vol === "number" ? p.vol : 0));
    const peak = series.indexOf(Math.max(...series));
    const frag = document.createDocumentFragment();
    series.forEach((v, i) => {
      const col = document.createElement("div");
      col.className = i === peak ? "hm-col peak" : "hm-col";
      col.style.setProperty("--h", `${Math.max(4, v)}%`);
      col.style.setProperty("--i", String(i));
      col.style.setProperty("--tone", toneFor(v));
      col.appendChild(document.createElement("i"));
      frag.appendChild(col);
    });
    colsEl.appendChild(frag);
  }

  // ── Duels card: segmented control + the two showcases ────────────────
  const duelBody = root.querySelector(".hm-duel-body");
  const segBtns = Array.from(root.querySelectorAll(".hm-seg-btn"));
  const segThumb = root.querySelector(".hm-seg-thumb");

  function moveThumb(btn) {
    if (!segThumb || !btn) return;
    segThumb.style.setProperty("--thumb-w", `${btn.offsetWidth}px`);
    segThumb.style.setProperty("--thumb-x", `${btn.offsetLeft - 3}px`);
  }

  segBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      segBtns.forEach((other) => {
        const on = other === btn;
        other.classList.toggle("active", on);
        other.setAttribute("aria-selected", on ? "true" : "false");
      });
      if (duelBody) duelBody.dataset.duelActive = btn.dataset.duel;
      moveThumb(btn);
    });
  });

  // The two showcases cycle through a fixed script rather than random values,
  // so the page looks the same on every visit.
  const dialNeedle = root.querySelector(".hm-dial-needle");
  const dialVal = $("hmDialVal");
  const ebarFill = $("hmEbarFill");
  const ebarVal = $("hmEbarVal");
  const DIAL_SCRIPT = [1500, 1875, 1180, 2240, 1620];
  const EBAR_SCRIPT = [0, 2.6, -1.4, 5.1, -0.3];
  let showcaseAt = 0;
  let showcaseTimer = 0;

  function showcaseTick() {
    showcaseAt = (showcaseAt + 1) % DIAL_SCRIPT.length;
    const rating = DIAL_SCRIPT[showcaseAt];
    if (dialNeedle) {
      // 600–2600 across a half turn.
      dialNeedle.style.setProperty("--a", `${((rating - 600) / 2000) * 180 - 90}deg`);
    }
    if (dialVal) dialVal.textContent = String(rating);

    const ev = EBAR_SCRIPT[showcaseAt];
    if (ebarFill) {
      ebarFill.style.setProperty("--w", `${50 + (Math.max(-6, Math.min(6, ev)) / 12) * 100}%`);
    }
    if (ebarVal) ebarVal.textContent = `${ev >= 0 ? "+" : "−"}${Math.abs(ev).toFixed(1)}`;
  }

  function startShowcase() {
    if (showcaseTimer || reduceMotion) return;
    showcaseTimer = window.setInterval(showcaseTick, 2600);
  }

  function stopShowcase() {
    if (showcaseTimer) window.clearInterval(showcaseTimer);
    showcaseTimer = 0;
  }

  // ── "How it works": the step you are level with drives the visual ────
  const howViz = $("hmHowViz");
  const howSteps = Array.from(root.querySelectorAll(".hm-step"));
  const howScore = $("hmHowScore");
  const howBand = $("hmHowBand");
  const howLabel = root.querySelector(".hm-how-score .hm-ro-label");
  const HOW_OUT = {
    1: { label: "Acceptable moves", value: "3 of 6", band: "within τ" },
    2: { label: "Mean Cₐ", value: "0.62", band: "raw Maia" },
    3: { label: "Findability", value: "76", band: "Natural" },
  };

  let howStep = 0;

  function setHowStep(n) {
    if (!howViz || howStep === n) return;
    howStep = n;
    howViz.dataset.active = String(n);
    howSteps.forEach((el) => el.classList.toggle("active", el.dataset.step === String(n)));
    const out = HOW_OUT[n];
    if (out) {
      if (howLabel) howLabel.textContent = out.label;
      if (howScore) {
        howScore.textContent = out.value;
        restartAnim(howScore, "hm-tick", 620);
      }
      if (howBand) {
        howBand.textContent = out.band;
        restartAnim(howBand, "hm-tick", 620);
      }
    }
  }

  // Driven off scroll position rather than an IntersectionObserver with a thin
  // rootMargin band: a fling or an anchor jump can skip straight past a narrow
  // band without ever firing, which left the visual stuck on step 1. Picking
  // the step nearest the viewport centre is correct at any scroll velocity.
  function syncHowStep() {
    if (!howSteps.length || !homeActive) return;
    const mid = window.innerHeight * 0.46;
    let best = null;
    let bestDist = Infinity;
    howSteps.forEach((el) => {
      const rect = el.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) return;
      const dist = Math.abs((rect.top + rect.bottom) / 2 - mid);
      if (dist < bestDist) { bestDist = dist; best = el; }
    });
    if (best) setHowStep(Number(best.dataset.step));
  }

  window.addEventListener("scroll", syncHowStep, { passive: true });
  setHowStep(1);

  // ── Outro waves ──────────────────────────────────────────────────────
  // Built once, then translated by exactly one period in CSS. Both harmonics
  // divide the period, so the loop has no seam and no path is regenerated on
  // a frame tick.
  const WAVE_PERIOD = 400;

  function wavePath(mid, amp, phase) {
    const points = [];
    for (let x = 0; x <= WAVE_PERIOD * 4; x += 16) {
      const a = Math.sin((x / WAVE_PERIOD) * Math.PI * 2 + phase);
      const b = Math.sin((x / (WAVE_PERIOD / 3)) * Math.PI * 2 + phase * 1.7);
      points.push(`${x} ${(mid + a * amp + b * amp * 0.28).toFixed(1)}`);
    }
    return `M${points.join(" L")}`;
  }

  const outroWave = $("hmOutroWave");
  const outroWaveB = $("hmOutroWaveB");
  if (outroWave) outroWave.setAttribute("d", wavePath(132, 20, 0));
  if (outroWaveB) outroWaveB.setAttribute("d", wavePath(158, 30, 1.35));

  // ── Shell hook ───────────────────────────────────────────────────────

  // Everything here needs the root to have a layout box, which it does as soon
  // as `.hidden` comes off.
  function activate() {
    if (revealObserver) {
      revealTargets().forEach((el) => {
        if (!el.classList.contains("is-in")) revealObserver.observe(el);
      });
      // Anything already above the fold should not wait for a scroll.
      revealTargets().forEach((el) => {
        const rect = el.getBoundingClientRect();
        if (rect.top < window.innerHeight) reveal(el);
      });
    } else {
      revealAll();
    }
    maybeCount();
    syncHowStep();
    moveThumb(segBtns.find((b) => b.classList.contains("active")) || segBtns[0]);
    if (replay && !replay.isPaused() && !document.hidden) replay.start();
    startShowcase();
    startTour();
    startLoop();
  }

  // A background tab does not run rAF callbacks at all, so deferring to one
  // would leave the segmented control unpositioned and the counters at zero
  // for anyone who opens the site in a tab they have not looked at yet. Only
  // defer when there is a frame coming.
  function activateSoon() {
    if (document.hidden) activate();
    else requestAnimationFrame(activate);
  }

  window.__homeSetActive = (active) => {
    homeActive = active;
    root.classList.toggle("hidden", !active);
    if (!active) {
      idle();
      return;
    }
    activateSoon();
  };

  // Nothing runs off-screen: no replay timer, no showcase interval, no tour,
  // no frame loop. Coming back re-runs the whole activation, which is
  // idempotent.
  function idle() {
    if (replay) replay.stop();
    stopShowcase();
    stopTour();
    stopLoop();
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) idle();
    else if (homeActive) activate();
  });

  window.addEventListener("resize", () => {
    moveThumb(segBtns.find((b) => b.classList.contains("active")) || segBtns[0]);
    measureTicker();
  });
}
