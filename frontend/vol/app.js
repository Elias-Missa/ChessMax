/* Chess Volatility Bar — frontend controller */
/* eslint-disable no-undef */
(function () {
  "use strict";

  // ── Vendor check ────────────────────────────────────────────────────────── //
  const missing = [];
  if (typeof window.Chessground === "undefined") missing.push("Chessground");
  if (typeof window.Chessboard === "undefined") missing.push("chessboard-adapter");
  if (typeof window.Chess === "undefined") missing.push("chess.js");
  if (typeof window.Chart === "undefined") missing.push("Chart.js");
  if (typeof window.idb === "undefined") missing.push("idb");
  if (typeof window.JSZip === "undefined") missing.push("JSZip");
  if (typeof window.ChessVolLibrary === "undefined") missing.push("library.js");
  if (missing.length) {
    const msg = `Frontend failed to load: ${missing.join(", ")}. Check /vendor/* is served.`;
    const el = document.getElementById("bootError");
    if (el) { el.textContent = msg; el.classList.remove("hidden"); }
    console.error(msg);
    return;
  }

  // ── Constants ────────────────────────────────────────────────────────────  //
  const STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const DEFAULT_FEN_TAIL = "w KQkq - 0 1";
  const AUTO_DEBOUNCE_MS = 450;

  // ── DOM refs ─────────────────────────────────────────────────────────────  //
  const $ = (s) => document.querySelector(s);
  const fenInput = $("#fenInput");
  const copyFenBtn = $("#copyFen");
  const deepToggle = $("#deepToggle");
  const deepToggleGame = $("#deepToggleGame");
  const evalBarEl = $("#evalBar");
  const evalLabelEl = $("#evalLabel");
  const volBarEl = $("#volBar");
  const volLabelEl = $("#volLabel");

  const btnStart = $("#btnStart");
  const btnClear = $("#btnClear");
  const btnFlip = $("#btnFlip");
  const btnAnalyzeFen = $("#btnAnalyzeFen");
  const autoAnalyze = $("#autoAnalyze");
  const editorStatus = $("#editorStatus");
  const turnWhiteBtn = $("#turnWhite");
  const turnBlackBtn = $("#turnBlack");

  const pgnFileInput = $("#pgnFile");
  const pgnInput = $("#pgnInput");
  const btnLoadPgn = $("#btnLoadPgn");
  const btnAnalyzePgn = $("#btnAnalyzePgn");
  const btnStopPgn = $("#btnStopPgn");
  const btnFlipGame = $("#btnFlipGame");
  const gameStatus = $("#gameStatus");
  const plyStatus = $("#plyStatus");
  const moveListEl = $("#moveList");
  const chartWrap = $("#chartWrap");
  const moveListWrap = $("#moveListWrap");
  const chartCanvas = $("#chart");
  const gameStatsEl = $("#gameStats");
  const statWhiteAcc = $("#statWhiteAcc");
  const statBlackAcc = $("#statBlackAcc");
  const statAvgVol = $("#statAvgVol");
  const statClassWhite = $("#statClassWhite");
  const statClassBlack = $("#statClassBlack");
  const reviewPlayerHeader = $("#reviewPlayerHeader");
  const reviewWhiteName = $("#reviewWhiteName");
  const reviewBlackName = $("#reviewBlackName");
  const reviewWhiteElo = $("#reviewWhiteElo");
  const reviewBlackElo = $("#reviewBlackElo");
  const reviewWhiteArc = $("#reviewWhiteArc");
  const reviewBlackArc = $("#reviewBlackArc");
  const reviewCoach = $("#reviewCoach");
  const reviewCoachText = $("#reviewCoachText");
  const reviewOpening = $("#reviewOpening");
  const reviewKeyMoments = $("#reviewKeyMoments");
  const reviewMoveCard = $("#reviewMoveCard");
  const reviewMoveNumber = $("#reviewMoveNumber");
  const reviewMoveSan = $("#reviewMoveSan");
  const reviewMoveBadge = $("#reviewMoveBadge");
  const reviewMoveAccuracy = $("#reviewMoveAccuracy");
  const reviewMoveEval = $("#reviewMoveEval");
  const reviewMoveWin = $("#reviewMoveWin");
  const reviewMoveLoss = $("#reviewMoveLoss");
  const reviewBestMoveRow = $("#reviewBestMoveRow");
  const reviewBestMove = $("#reviewBestMove");
  const reviewNavigation = $("#reviewNavigation");
  const reviewFirst = $("#reviewFirst");
  const reviewPrevious = $("#reviewPrevious");
  const reviewNext = $("#reviewNext");
  const reviewLast = $("#reviewLast");
  const reviewMoveCounter = $("#reviewMoveCounter");
  const reviewWhiteStripName = $("#reviewWhiteStripName");
  const reviewBlackStripName = $("#reviewBlackStripName");
  const gameClassCard = $("#gameClassCard");
  const reviewClassTable = $("#reviewClassTable");
  const btnStartReview = $("#btnStartReview");

  const libraryDrop = $("#libraryDrop");
  const libraryFileInput = $("#libraryFileInput");
  const libraryProgress = $("#libraryProgress");
  const libraryTableBody = $("#libraryTableBody");
  const libraryDateFrom = $("#libraryDateFrom");
  const libraryDateTo = $("#libraryDateTo");
  const libraryOpponent = $("#libraryOpponent");
  const libraryMinV = $("#libraryMinV");
  const libraryMaxV = $("#libraryMaxV");
  const libraryClassKey = $("#libraryClassKey");
  const libraryClassMin = $("#libraryClassMin");

  const arrowToggle = $("#arrowToggle");
  const arrowToggleGame = $("#arrowToggleGame");
  const soundsToggle = $("#soundsToggle");
  const soundsToggleEditor = $("#soundsToggleEditor");
  const arrowLayer = $("#arrowLayer");
  const topLinesList = $("#topLinesList");
  const topLinesListGame = $("#topLinesListGame");
  const boardFrameEl = document.querySelector(".board-frame");
  const reviewBoardOverlay = document.getElementById("reviewBoardOverlay");
  const ReviewUI = window.ChessReviewUI || null;
  const SVG_NS = "http://www.w3.org/2000/svg";

  // The shell hides #vol-root while a Train tab is active; vol behaviour that
  // depends on "the analyzer is on screen" must check this, not just
  // body.dataset.tab (which keeps its last vol value across shell switches).
  const volRootEl = document.getElementById("vol-root");
  function volAppVisible() {
    return !volRootEl || !volRootEl.classList.contains("hidden");
  }

  // Explain panels — one per side panel; renderExplain() writes into the one
  // that belongs to whichever tab is active.
  const explainEls = {
    editor: {
      root: $("#volExplain"),
      summary: $("#volExplainSummary"),
      badges: $("#volExplainBadges"),
      stack: $("#volExplainStack"),
      bar: $("#volExplainStackBar"),
      legend: $("#volExplainStackLegend"),
      hint: $("#volExplainHint"),
    },
    game: {
      root: $("#volExplainGame"),
      summary: $("#volExplainSummaryGame"),
      badges: $("#volExplainBadgesGame"),
      stack: $("#volExplainStackGame"),
      bar: $("#volExplainStackBarGame"),
      legend: $("#volExplainStackLegendGame"),
      hint: $("#volExplainHintGame"),
    },
  };

  // ── Tab switching ─────────────────────────────────────────────────────── //
  function setTab(name) {
    document.querySelectorAll("[data-for]").forEach((el) => {
      const match = el.dataset.for === name;
      el.classList.toggle("hidden", !match);
    });
    // The main board row has no [data-for] because it is shared between the
    // editor and game tabs. Full-page tabs replace it entirely.
    const boardRow = document.querySelector(".board-row");
    const appMain = document.querySelector("main.app");
    if (boardRow) boardRow.classList.toggle("hidden", name === "about" || name === "library");
    if (appMain) appMain.classList.toggle("app--game-wide", name === "game");
    if (boardRow) boardRow.classList.toggle("board-row--game-wide", name === "game");
    document.body.dataset.tab = name;
    if (window.__onVolTabChange) window.__onVolTabChange(name);
    paintLastMoveDecor();
    // Re-show conditional children inside game-panel only if they have data
    if (name === "game") {
      if (loadedPlies && loadedPlies.length) moveListWrap.classList.remove("hidden");
      if (chart) chartWrap.classList.remove("hidden");
      if (gameStatsEl && plyResults.some(Boolean)) gameStatsEl.classList.remove("hidden");
      if (chart && chartWrap && !chartWrap.classList.contains("hidden")) {
        requestAnimationFrame(() => chart.resize());
      }
    }
    if (name === "about") ensureAboutDemos();
    if (name === "library") refreshLibraryTable();
    // Boot skips auto-analysis while the analyzer is hidden; kick it off when
    // the editor actually comes on screen instead.
    if (name === "editor") scheduleAutoAnalyze();
    // The explain panels are tab-scoped: each side panel has its own DOM. When
    // the user switches tabs we re-render the latest result into the now-
    // active panel so it doesn't show stale content.
    if (name === "editor" || name === "game") {
      if (lastVolJson) renderExplain(lastVolJson, lastClassificationJson);
    }
  }

  // ChessMax shell hook: the top-level tab bar drives this app's panels.
  window.__volSetTab = setTab;

  // ── About tab demos (lazy-init) ───────────────────────────────────────── //
  // The two mini-boards on the Why tab are static examples. We build them the
  // first time the user visits the tab so chessboard.js sees a non-zero width
  // container (it measures on init). Values are hardcoded — no backend call.
  let aboutDemosReady = false;

  const DEMO_DRY_FEN = "4k3/p2p1p1p/1p1b1p2/8/8/1P1B1P2/P2P1P1P/4K3 w - - 0 1";
  // Qxg7+!! Kxg7 is stalemate. Any other White move loses to ...Qxg2#
  // (black queen on g2 supported by bishop on b7 along the long diagonal).
  const DEMO_SHARP_FEN = "2r2rk1/pb1n2pp/8/4Q3/8/7q/5bPP/7K w - - 0 1";

  // Section 2 — winning side (+3.00)
  const DEMO_WIN_LOW_FEN = "4k3/8/8/4P3/4P3/4K3/8/8 w - - 0 1";     // low V, technical
  const DEMO_WIN_HIGH_FEN = "2r3k1/5ppp/4p3/3pP3/3PN3/PR4P1/5P1P/6K1 w - - 0 1"; // high V, only-move

  // Section 3 — losing side (−3.00)
  const DEMO_LOSE_LOW_FEN = "8/8/4k3/4p3/4p3/8/8/4K3 w - - 0 1";    // low V, decided
  const DEMO_LOSE_HIGH_FEN = "r5k1/pp3ppp/8/4N3/8/q6P/PP3PP1/3R3K w - - 0 1"; // high V, swindle

  // Section 4 — same position, two clocks
  const DEMO_CLOCK_FEN = "r3k2r/pbq2ppp/1pn1pn2/2pp4/3P1B2/2P1PN2/PPQNBPPP/R4RK1 b kq - 0 1";

  // Section 5 — Kasparov–Topalov, Wijk aan Zee 1999
  const FAMOUS_PGN = `[Event "Hoogovens A Tournament"]
[Site "Wijk aan Zee NED"]
[Date "1999.01.20"]
[White "Kasparov, Garry"]
[Black "Topalov, Veselin"]
[Result "1-0"]

1. e4 d6 2. d4 Nf6 3. Nc3 g6 4. Be3 Bg7 5. Qd2 c6 6. f3 b5 7. Nge2 Nbd7 8. Bh6 Bxh6 9. Qxh6 Bb7 10. a3 e5 11. O-O-O Qe7 12. Kb1 a6 13. Nc1 O-O-O 14. Nb3 exd4 15. Rxd4 c5 16. Rd1 Nb6 17. g3 Kb8 18. Na5 Ba8 19. Bh3 d5 20. Qf4+ Ka7 21. Rhe1 d4 22. Nd5 Nbxd5 23. exd5 Qd6 24. Rxd4 cxd4 25. Re7+ Kb6 26. Qxd4+ Kxa5 27. b4+ Ka4 28. Qc3 Qxd5 29. Ra7 Bb7 30. Rxb7 Qc4 31. Qxf6 Kxa3 32. Qxa6+ Kxb4 33. c3+ Kxc3 34. Qa1+ Kd2 35. Qb2+ Kd1 36. Bf1 Rd2 37. Rd7 Rxd7 38. Bxc4 bxc4 39. Qxh8 Rd3 40. Qa8 c3 41. Qa4+ Ke1 42. f4 f5 43. Kc1 Rd2 44. Qa7 1-0`;

  // Ply window for the famous-game slider: moves 18–28 = ply indices 34–55
  const FAMOUS_PLY_START = 34;
  const FAMOUS_PLY_END = 55;

  // Tiny FNV-1a hash for cache keying
  function fnv1a(str) {
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    return (h >>> 0).toString(36);
  }

  function paintEvalInto(barEl, labelEl, cpWhite) {
    if (!barEl || !labelEl) return;
    barEl.style.setProperty("--fill", evalCpToFill(cpWhite));
    if (cpWhite === 0) {
      labelEl.textContent = "0.00";
    } else {
      const sign = cpWhite > 0 ? "+" : "−";
      labelEl.textContent = sign + (Math.abs(cpWhite) / 100).toFixed(2);
    }
  }

  function paintVolInto(barEl, labelEl, score) {
    if (!barEl || !labelEl) return;
    const fill = Math.max(0, Math.min(1, score / 100));
    barEl.style.setProperty("--fill", fill);
    barEl.style.setProperty("--local", 0);
    barEl.style.setProperty("--split-visible", 0);
    barEl.dataset.color = scoreToColor(score);
    barEl.dataset.decided = "false";
    labelEl.textContent = score.toFixed(1);
  }

  function paintDemoBars() {
    // Section 1 — draws
    paintEvalInto($("#demoEvalA"), $("#demoEvalLabelA"), 0);
    paintVolInto($("#demoVolA"), $("#demoVolLabelA"), 4);
    paintEvalInto($("#demoEvalB"), $("#demoEvalLabelB"), 0);
    paintVolInto($("#demoVolB"), $("#demoVolLabelB"), 88);

    // Section 2 — winning
    paintEvalInto($("#demoEvalC"), $("#demoEvalLabelC"), 300);
    paintVolInto($("#demoVolC"), $("#demoVolLabelC"), 6);
    paintEvalInto($("#demoEvalD"), $("#demoEvalLabelD"), 300);
    paintVolInto($("#demoVolD"), $("#demoVolLabelD"), 78);

    // Section 3 — losing
    paintEvalInto($("#demoEvalE"), $("#demoEvalLabelE"), -300);
    paintVolInto($("#demoVolE"), $("#demoVolLabelE"), 8);
    // Mark the losing-low card as decided
    const volE = $("#demoVolE");
    if (volE) volE.dataset.decided = "true";
    paintEvalInto($("#demoEvalF"), $("#demoEvalLabelF"), -300);
    paintVolInto($("#demoVolF"), $("#demoVolLabelF"), 82);
  }

  function ensureAboutDemos() {
    if (aboutDemosReady) return;
    if (!document.getElementById("demoBoardA") || !document.getElementById("demoBoardB")) return;
    aboutDemosReady = true;

    // Section 1
    Chessboard("demoBoardA", {
      position: DEMO_DRY_FEN.split(" ")[0],
      draggable: false, showNotation: false,
      pieceTheme: "/vendor/img/pieces/{piece}.png",
    });
    Chessboard("demoBoardB", {
      position: DEMO_SHARP_FEN.split(" ")[0],
      draggable: false, showNotation: false,
      pieceTheme: "/vendor/img/pieces/{piece}.png",
    });

    // Section 2
    Chessboard("demoBoardC", {
      position: DEMO_WIN_LOW_FEN.split(" ")[0],
      draggable: false, showNotation: false,
      pieceTheme: "/vendor/img/pieces/{piece}.png",
    });
    Chessboard("demoBoardD", {
      position: DEMO_WIN_HIGH_FEN.split(" ")[0],
      draggable: false, showNotation: false,
      pieceTheme: "/vendor/img/pieces/{piece}.png",
    });

    // Section 3
    Chessboard("demoBoardE", {
      position: DEMO_LOSE_LOW_FEN.split(" ")[0],
      draggable: false, showNotation: false,
      pieceTheme: "/vendor/img/pieces/{piece}.png",
    });
    Chessboard("demoBoardF", {
      position: DEMO_LOSE_HIGH_FEN.split(" ")[0],
      draggable: false, showNotation: false,
      pieceTheme: "/vendor/img/pieces/{piece}.png",
    });

    // Section 4 — clock comparison (single board)
    Chessboard("demoBoardG", {
      position: DEMO_CLOCK_FEN.split(" ")[0],
      draggable: false, showNotation: false,
      pieceTheme: "/vendor/img/pieces/{piece}.png",
    });

    paintDemoBars();
    initFamousGame();
    initFindabilitySimulator();
  }

  function initFindabilitySimulator() {
    const slider = document.getElementById("whyEloSlider");
    const valLabel = document.getElementById("whyEloValue");
    if (!slider) return;

    const update = () => {
      const r = Number(slider.value);
      if (valLabel) valLabel.textContent = `${r} Elo`;

      const p1 = Math.min(99, Math.round(80 + (r - 600) * 0.012));
      const p2 = Math.max(12, Math.min(96, Math.round(12 + (r - 600) * 0.042)));
      const p3 = Math.max(3, Math.min(24, Math.round(3 + (r - 600) * 0.0105)));

      const v1 = document.getElementById("probVal1");
      const f1 = document.getElementById("probFill1");
      if (v1) v1.textContent = `${p1}%`;
      if (f1) f1.style.width = `${p1}%`;

      const v2 = document.getElementById("probVal2");
      const f2 = document.getElementById("probFill2");
      if (v2) v2.textContent = `${p2}%`;
      if (f2) f2.style.width = `${p2}%`;

      const v3 = document.getElementById("probVal3");
      const f3 = document.getElementById("probFill3");
      if (v3) v3.textContent = `${p3}%`;
      if (f3) f3.style.width = `${p3}%`;
    };

    slider.addEventListener("input", update);
    update();
  }

  // ── Famous-game scrubber ────────────────────────────────────────────── //
  let famousChart = null;
  let famousBoard = null;
  let famousPlies = [];

  function initFamousGame() {
    const boardEl = document.getElementById("famousBoard");
    if (!boardEl) return;

    // Parse PGN
    const plies = parseFamousPgn(FAMOUS_PGN);
    if (!plies || !plies.length) return;
    famousPlies = plies;

    // Init board at ply 34 (before move 18)
    const startPly = Math.min(FAMOUS_PLY_START, plies.length - 1);
    famousBoard = Chessboard("famousBoard", {
      position: plies[startPly].fen_before.split(" ")[0],
      draggable: false,
      showNotation: false,
      pieceTheme: "/vendor/img/pieces/{piece}.png",
    });

    // Slider setup
    const slider = document.getElementById("famousSlider");
    const sanLabel = document.getElementById("famousSanLabel");
    const evalLabel = document.getElementById("famousEvalLabel");
    const volLabel = document.getElementById("famousVolLabel");
    const statusEl = document.getElementById("famousStatus");

    if (slider) {
      slider.min = String(FAMOUS_PLY_START);
      slider.max = String(Math.min(FAMOUS_PLY_END, plies.length - 1));
      slider.value = String(startPly);
    }

    // Init the mini chart
    const chartCanvas = document.getElementById("famousChart");
    if (chartCanvas) {
      const ctx = chartCanvas.getContext("2d");
      famousChart = new Chart(ctx, {
        type: "line",
        data: {
          labels: [],
          datasets: [{
            label: "V",
            data: [],
            borderColor: "#39ff14",
            backgroundColor: "rgba(57,255,20,0.10)",
            borderWidth: 1.4,
            pointRadius: 1.5,
            pointHoverRadius: 4,
            pointBackgroundColor: "#39ff14",
            tension: 0.3,
            fill: true,
            spanGaps: true,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 0 },
          plugins: { legend: { display: false }, tooltip: { enabled: false } },
          scales: {
            y: { min: 0, max: 100, display: false },
            x: { display: false },
          },
        },
      });
    }

    // Cache check
    const cacheKey = "famousGameV2:" + fnv1a(FAMOUS_PGN);
    let cached = null;
    try {
      const raw = localStorage.getItem(cacheKey);
      if (raw) cached = JSON.parse(raw);
    } catch (_) { /* ignore */ }

    if (cached && Array.isArray(cached.scores)) {
      applyFamousScores(cached.scores, cached.evalsCp || []);
      if (statusEl) statusEl.textContent = "Cached";
      updateFamousSliderPos(startPly, cached.scores, cached.evalsCp || []);
    } else {
      if (statusEl) statusEl.textContent = "Analyzing… (one-time)";
      fetchFamousAnalysis(cacheKey, statusEl);
    }

    // Slider scrub
    if (slider) {
      slider.addEventListener("input", () => {
        const idx = Number(slider.value);
        const scores = famousChart ? famousChart.data.datasets[0].data : [];
        // Try to pull evalsCp from the cache
        let evalsCp = [];
        try {
          const raw = localStorage.getItem(cacheKey);
          if (raw) { const c = JSON.parse(raw); evalsCp = c.evalsCp || []; }
        } catch (_) { /* ignore */ }
        updateFamousSliderPos(idx, scores, evalsCp);
      });
    }
  }

  function parseFamousPgn(text) {
    try {
      const g = new Chess();
      if (!g.load_pgn(text, { sloppy: true })) return null;
      const history = g.history({ verbose: true });
      const replay = new Chess();
      const plies = [];
      for (const mv of history) {
        const fenBefore = replay.fen();
        const san = replay.move({ from: mv.from, to: mv.to, promotion: mv.promotion }).san;
        plies.push({ san, fen_before: fenBefore, fen_after: replay.fen() });
      }
      return plies;
    } catch (_) { return null; }
  }

  function applyFamousScores(scores, evalsCp) {
    if (!famousChart) return;
    const labels = [];
    const data = [];
    for (let i = FAMOUS_PLY_START; i <= Math.min(FAMOUS_PLY_END, famousPlies.length - 1); i++) {
      const ply = famousPlies[i];
      const moveNum = Math.floor(i / 2) + 1;
      const side = i % 2 === 0 ? "" : "…";
      labels.push(`${moveNum}.${side}${ply.san}`);
      data.push(scores[i] != null ? scores[i] : null);
    }
    famousChart.data.labels = labels;
    famousChart.data.datasets[0].data = data;
    famousChart.update("none");
  }

  function updateFamousSliderPos(idx, scores, evalsCp) {
    if (idx < 0 || idx >= famousPlies.length) return;
    const ply = famousPlies[idx];
    if (famousBoard) famousBoard.position(ply.fen_before.split(" ")[0]);

    const sanLabel = document.getElementById("famousSanLabel");
    const evalLabel = document.getElementById("famousEvalLabel");
    const volLabel = document.getElementById("famousVolLabel");

    const moveNum = Math.floor(idx / 2) + 1;
    const side = idx % 2 === 0 ? "." : "…";
    if (sanLabel) sanLabel.textContent = `${moveNum}${side} ${ply.san}`;

    if (evalLabel && evalsCp && evalsCp[idx] != null) {
      const turn = ply.fen_before.split(/\s+/)[1] || "w";
      evalLabel.textContent = formatEval(evalsCp[idx], turn);
    } else if (evalLabel) {
      evalLabel.textContent = "—";
    }

    const v = scores[idx];
    if (volLabel) {
      if (v != null) {
        volLabel.textContent = Math.round(v).toString();
        volLabel.dataset.color = scoreToColor(v);
      } else {
        volLabel.textContent = "—";
        volLabel.dataset.color = "low";
      }
    }

    // Highlight point on famous chart
    if (famousChart) {
      const chartIdx = idx - FAMOUS_PLY_START;
      famousChart.data.datasets[0].pointRadius =
        famousChart.data.datasets[0].data.map((_, i) => (i === chartIdx ? 5 : 1.5));
      famousChart.update("none");
    }
  }

  async function fetchFamousAnalysis(cacheKey, statusEl) {
    try {
      const resp = await fetch("/analyze/pgn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pgn: FAMOUS_PGN, deep: false, max_plies: 56 }),
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      const splitRe = /\r\n\r\n|\n\n/;
      const scores = [];
      const evalsCp = [];

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let m;
        while ((m = splitRe.exec(buf))) {
          const chunk = buf.slice(0, m.index);
          buf = buf.slice(m.index + m[0].length);
          // Parse SSE chunk
          let event = "message";
          const dataLines = [];
          for (const line of chunk.split(/\r?\n/)) {
            if (!line || line.startsWith(":")) continue;
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
          }
          if (!dataLines.length) continue;
          let payload;
          try { payload = JSON.parse(dataLines.join("\n")); } catch (_) { continue; }

          if (event === "ply" && payload.ply) {
            const p = payload.ply;
            const i = p.ply - 1;
            scores[i] = p.volatility && p.volatility.score != null ? p.volatility.score : null;
            evalsCp[i] = p.eval_cp != null ? p.eval_cp : null;
            // Update chart in-flight
            applyFamousScores(scores, evalsCp);
            if (statusEl) statusEl.textContent = `Analyzing… ${p.ply} plies`;
          } else if (event === "done" && payload.plies) {
            payload.plies.forEach((p, i) => {
              scores[i] = p.volatility && p.volatility.score != null ? p.volatility.score : null;
              evalsCp[i] = p.eval_cp != null ? p.eval_cp : null;
            });
            applyFamousScores(scores, evalsCp);
          }
        }
      }

      // Save to cache
      try { localStorage.setItem(cacheKey, JSON.stringify({ scores, evalsCp })); } catch (_) { /* quota */ }
      if (statusEl) statusEl.textContent = "Cached";
    } catch (err) {
      if (statusEl) statusEl.textContent = "Engine unavailable — no V trace";
      console.warn("Famous game analysis failed:", err);
    }
  }

  // ── Shared deep toggle state ──────────────────────────────────────────── //
  function syncDeep(source) {
    const val = source.checked;
    if (deepToggle && deepToggle !== source) deepToggle.checked = val;
    if (deepToggleGame && deepToggleGame !== source) deepToggleGame.checked = val;
  }
  if (deepToggle) deepToggle.addEventListener("change", () => syncDeep(deepToggle));
  if (deepToggleGame) deepToggleGame.addEventListener("change", () => syncDeep(deepToggleGame));

  function deepEnabled() {
    return !!(deepToggle && deepToggle.checked);
  }

  // ── Shared arrow toggle state ─────────────────────────────────────────── //
  function syncArrow(source) {
    const val = source.checked;
    if (arrowToggle && arrowToggle !== source) arrowToggle.checked = val;
    if (arrowToggleGame && arrowToggleGame !== source) arrowToggleGame.checked = val;
    refreshArrow();
  }
  if (arrowToggle) arrowToggle.addEventListener("change", () => syncArrow(arrowToggle));
  if (arrowToggleGame) arrowToggleGame.addEventListener("change", () => syncArrow(arrowToggleGame));

  function arrowEnabled() {
    return !!(arrowToggle && arrowToggle.checked);
  }

  // ── Sound effects ─────────────────────────────────────────────────────── //
  // Sounds come from the lichess "standard" set, preloaded once and cloned
  // per play (see web/audio.js for the LichessAudio module). Enabled state
  // and volume persist to localStorage; the in-page toggles below mirror it.
  function ensureAudioResume() { /* HTMLAudio doesn't need an autoplay gate */ }

  // Initialise the sound toggles from persisted state.
  if (window.LichessAudio) {
    const persistedOn = window.LichessAudio.isEnabled();
    if (soundsToggle) soundsToggle.checked = persistedOn;
    if (soundsToggleEditor) soundsToggleEditor.checked = persistedOn;
  }

  function soundsEnabled() {
    return !!(soundsToggle && soundsToggle.checked);
  }

  function syncSounds(source) {
    const val = source.checked;
    if (soundsToggle && soundsToggle !== source) soundsToggle.checked = val;
    if (soundsToggleEditor && soundsToggleEditor !== source) soundsToggleEditor.checked = val;
    if (window.LichessAudio) window.LichessAudio.setEnabled(val);
  }
  if (soundsToggle) soundsToggle.addEventListener("change", () => syncSounds(soundsToggle));
  if (soundsToggleEditor) soundsToggleEditor.addEventListener("change", () => syncSounds(soundsToggleEditor));

  function soundMove() { if (window.LichessAudio) window.LichessAudio.play("move"); }
  function soundCapture() { if (window.LichessAudio) window.LichessAudio.play("capture"); }
  function soundCheck() { if (window.LichessAudio) window.LichessAudio.play("check"); }
  // No dedicated checkmate file in the standard lichess set — fall back to
  // the notify chime, which is the closest "terminal" cue available.
  function soundCheckmate() { if (window.LichessAudio) window.LichessAudio.play("notify"); }

  function classifyMove(san) {
    if (!san) return "move";
    if (san.endsWith("#")) return "checkmate";
    if (san.endsWith("+")) return "check";
    if (san.includes("x")) return "capture";
    return "move";
  }

  function playMoveSound(san) {
    if (!soundsEnabled()) return;
    switch (classifyMove(san)) {
      case "checkmate": soundCheckmate(); break;
      case "check": soundCheck(); break;
      case "capture": soundCapture(); break;
      default: soundMove(); break;
    }
  }

  // ── Board ─────────────────────────────────────────────────────────────── //
  let suppressSync = false;

  // Game scrubber state (declared before Chessboard() so onMoveEnd can close
  // over the live bindings without temporal-dead-zone issues).
  let loadedPlies = [];
  let plyResults = [];
  let currentPlyIdx = -1;

  function gameTabActive() {
    return volAppVisible() && document.body.dataset.tab === "game";
  }

  function squaresFromPly(prev) {
    if (!prev) return null;
    if (prev.from && prev.to) return { from: prev.from, to: prev.to };
    const uci = prev.move_uci || "";
    if (uci.length < 4) return null;
    return { from: uci.slice(0, 2), to: uci.slice(2, 4) };
  }

  function clearLastMoveDecor() {
    if (board && board.setLastMove) board.setLastMove(null, null);
  }

  // Highlight the last move on the board using Chessground's built-in lastMove
  // styling (the adapter exposes setLastMove). The editor tab has no concept
  // of a "previous move," so we always clear there.
  function paintLastMoveDecor() {
    if (!board || !board.setLastMove) return;
    if (!gameTabActive() || !loadedPlies.length || currentPlyIdx < 0) {
      clearLastMoveDecor();
      return;
    }
    const sq = squaresFromPly(loadedPlies[currentPlyIdx]);
    if (!sq) { clearLastMoveDecor(); return; }
    board.setLastMove(sq.from, sq.to);
  }

  // Hoisted shared state for the analyze/invalidate helpers below. These are
  // consumed by scheduleAutoAnalyze() and analyzeFen() later in the module.
  let autoTimer = null;
  let inflightFen = null;
  let lastAnalyzedFen = null;

  // Single source of truth for "the board changed; any pending or in-flight
  // analysis is now stale". Cancels the debounce timer, aborts the current
  // fetch (so a late response cannot overwrite the cleared UI), and wipes the
  // engine-lines panel + the top-move arrow.
  function invalidateAnalysis() {
    if (autoTimer) {
      clearTimeout(autoTimer);
      autoTimer = null;
    }
    if (inflightFen) {
      try { inflightFen.abort(); } catch (_) { /* ignore */ }
      inflightFen = null;
    }
    lastAnalyzedFen = null;
    setTopMove(null);
    clearTopLinesLists();
  }

  const board = Chessboard("volBoard", {
    draggable: true,
    sparePieces: true,
    dropOffBoard: "trash",
    position: "start",
    pieceTheme: "/vendor/img/pieces/{piece}.png",
    // NOTE: we intentionally do NOT auto-flip the side-to-move on drop. The
    // explicit White/Black segmented control owns turn state. Auto-flipping
    // conflated "set up pieces" with "play a move" and was a frequent source
    // of the engine being fed a position that disagrees with the board.
    onChange: () => {
      if (suppressSync) return;
      syncFenFromBoard();
      scheduleAutoAnalyze();
    },
    // Sound feedback on direct piece drags. Only fires for square→square
    // movement (skips spare-piece placements and trash-drops, since those
    // are setup actions, not "moves"). Using oldPos lets us distinguish a
    // capture (target was occupied by a different piece) from a quiet move.
    onDrop: (source, target, piece, _newPos, oldPos) => {
      if (!soundsEnabled()) return;
      if (suppressSync) return;  // programmatic position set
      if (source === "spare") return;  // dragged from spare tray
      if (target === "offboard" || target === "trash") return;
      if (source === target) return;  // snap-back (illegal/no-op)
      ensureAudioResume();
      const captured = oldPos && oldPos[target] && oldPos[target] !== piece;
      if (captured) soundCapture();
      else soundMove();
    },
    onMoveEnd: () => {
      paintLastMoveDecor();
      refreshArrow();
    },
  });

  window.addEventListener("resize", () => {
    board.resize();
    refreshArrow();
    paintLastMoveDecor();
    if (chart && gameTabActive()) requestAnimationFrame(() => chart.resize());
  });

  // Chess.com-style keyboard navigation through PGN plies. Runs on the game
  // tab only and yields to any text input so PGN/FEN paste & edit still work.
  window.addEventListener("keydown", (e) => {
    if (!loadedPlies.length) return;

    if (!gameTabActive()) return;

    const t = e.target;
    const tag = t && t.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || (t && t.isContentEditable)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    const last = loadedPlies.length - 1;
    let next = null;
    switch (e.key) {
      case "ArrowRight": next = Math.min(last, (currentPlyIdx < 0 ? -1 : currentPlyIdx) + 1); break;
      case "ArrowLeft": next = Math.max(0, (currentPlyIdx < 0 ? 1 : currentPlyIdx) - 1); break;
      case "Home": next = 0; break;
      case "End": next = last; break;
      default: return;
    }
    e.preventDefault();
    if (next !== currentPlyIdx) jumpToPly(next);
  });

  if (reviewFirst) reviewFirst.addEventListener("click", () => jumpToPly(0));
  if (reviewPrevious) {
    reviewPrevious.addEventListener("click", () => jumpToPly(Math.max(0, currentPlyIdx - 1)));
  }
  if (reviewNext) {
    reviewNext.addEventListener("click", () =>
      jumpToPly(Math.min(loadedPlies.length - 1, currentPlyIdx + 1))
    );
  }
  if (reviewLast) {
    reviewLast.addEventListener("click", () => jumpToPly(loadedPlies.length - 1));
  }
  if (btnStartReview) {
    btnStartReview.addEventListener("click", () => {
      if (!loadedPlies.length) return;
      if (moveListWrap) moveListWrap.classList.remove("hidden");
      if (reviewNavigation) reviewNavigation.classList.remove("hidden");
      jumpToPly(0);
      if (reviewMoveCard) {
        reviewMoveCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });
  }

  // Expand a FEN rank (e.g. "r3k2r" or "4P3") into an 8-char string where
  // empty squares are "." This lets us index the rank by file (a=0…h=7).
  function expandRank(r) {
    let out = "";
    for (const ch of r) {
      if (ch >= "1" && ch <= "8") out += ".".repeat(ch.charCodeAt(0) - 48);
      else out += ch;
    }
    return out.padEnd(8, ".").slice(0, 8);
  }

  // Recompute castling rights from the current placement. A right survives
  // only if the king and the relevant rook are still on their home squares.
  // Any board edit that moves either piece cancels the matching right.
  function computeCastling(placement) {
    const ranks = placement.split("/");
    if (ranks.length !== 8) return "-";
    const rank1 = expandRank(ranks[7]);
    const rank8 = expandRank(ranks[0]);
    const whiteKingHome = rank1[4] === "K";
    const blackKingHome = rank8[4] === "k";
    let rights = "";
    if (whiteKingHome && rank1[7] === "R") rights += "K";
    if (whiteKingHome && rank1[0] === "R") rights += "Q";
    if (blackKingHome && rank8[7] === "r") rights += "k";
    if (blackKingHome && rank8[0] === "r") rights += "q";
    return rights || "-";
  }

  // Produce a fresh, self-consistent FEN from (placement on the board) +
  // (side-to-move from the previous FEN). We deliberately drop the inherited
  // en-passant square (any edit invalidates it) and reset the halfmove clock
  // to 0. Castling rights are recomputed from placement. This is the only
  // place the UI hands a FEN to the backend.
  function assembleFen() {
    const parts = (fenInput.value || "").trim().split(/\s+/);
    const placement = board.fen();
    const turn = parts[1] === "b" ? "b" : "w";
    const castling = computeCastling(placement);
    const fullmove = Math.max(1, parseInt(parts[5], 10) || 1);
    return `${placement} ${turn} ${castling} - 0 ${fullmove}`;
  }

  function syncFenFromBoard() {
    const fen = assembleFen();
    fenInput.value = fen;
    editorStatus.textContent = validateFen(fen) ? "" : "⚠ Incomplete or illegal position";
    syncTurnToggleFromFen();
    invalidateAnalysis();
  }

  function syncBoardFromFen(fen) {
    const parts = fen.trim().split(/\s+/);
    if (parts.length >= 1) {
      suppressSync = true;
      try { board.position(parts[0], false); } finally { suppressSync = false; }
    }
    clearLastMoveDecor();
    // suppressSync swallowed onChange, so the shared cleanup path didn't run.
    // Do it explicitly — otherwise a FEN-paste edit leaves stale engine lines
    // and arrows on the screen until the next re-analysis lands.
    invalidateAnalysis();
  }

  function validateFen(fen) {
    try { const g = new Chess(); return g.load(fen); } catch (_) { return false; }
  }

  function getTurn() {
    const parts = (fenInput.value || "").trim().split(/\s+/);
    return parts[1] === "b" ? "b" : "w";
  }

  function setTurn(color) {
    const next = color === "b" ? "b" : "w";
    const parts = (fenInput.value || "").trim().split(/\s+/);
    const placement = parts[0] || board.fen();
    const castling = computeCastling(placement);
    const fullmove = Math.max(1, parseInt(parts[5], 10) || 1);
    fenInput.value = `${placement} ${next} ${castling} - 0 ${fullmove}`;
    editorStatus.textContent = validateFen(fenInput.value) ? "" : "⚠ Incomplete or illegal position";
    // Any side-to-move change makes a prior analysis stale by definition.
    invalidateAnalysis();
  }

  function syncTurnToggleFromFen() {
    const turn = getTurn();
    if (turnWhiteBtn) {
      const white = turn === "w";
      turnWhiteBtn.classList.toggle("active", white);
      turnWhiteBtn.setAttribute("aria-checked", white ? "true" : "false");
    }
    if (turnBlackBtn) {
      const black = turn === "b";
      turnBlackBtn.classList.toggle("active", black);
      turnBlackBtn.setAttribute("aria-checked", black ? "true" : "false");
    }
  }

  function onTurnBtnClick(color) {
    if (getTurn() === color) return;
    setTurn(color);
    syncTurnToggleFromFen();
    scheduleAutoAnalyze();
  }

  if (turnWhiteBtn) turnWhiteBtn.addEventListener("click", () => onTurnBtnClick("w"));
  if (turnBlackBtn) turnBlackBtn.addEventListener("click", () => onTurnBtnClick("b"));

  // ── Auto-analyze (debounced) ─────────────────────────────────────────── //
  // `autoTimer` is declared near the top of the module alongside
  // `inflightFen` so that `invalidateAnalysis()` can own both.

  function scheduleAutoAnalyze() {
    // Don't burn Stockfish time analyzing a hidden board (e.g. page boot
    // while the Train tabs or the auth overlay are showing).
    if (!volAppVisible()) return;
    if (!autoAnalyze || !autoAnalyze.checked) return;
    if (autoTimer) clearTimeout(autoTimer);
    autoTimer = setTimeout(() => {
      autoTimer = null;
      const fen = fenInput.value.trim();
      if (!validateFen(fen)) return;
      if (fen === lastAnalyzedFen) return; // e.g. re-entering the editor tab
      analyzeFen(fen).catch((err) => {
        editorStatus.textContent = `Error: ${err.message || err}`;
      });
    }, AUTO_DEBOUNCE_MS);
  }

  btnStart.addEventListener("click", () => {
    syncBoardFromFen(STARTING_FEN);
    fenInput.value = STARTING_FEN;
    syncTurnToggleFromFen();
    invalidateAnalysis();
    scheduleAutoAnalyze();
  });

  btnClear.addEventListener("click", () => {
    suppressSync = true;
    board.clear(false);
    suppressSync = false;
    syncFenFromBoard();
    scheduleAutoAnalyze();
  });

  btnFlip.addEventListener("click", () => {
    board.flip();
    setTimeout(() => {
      refreshArrow();
      paintLastMoveDecor();
      refreshReviewBoardOverlay();
    }, 0);
  });

  function flipReviewBoard() {
    board.flip();
    setTimeout(() => {
      refreshArrow();
      paintLastMoveDecor();
      refreshReviewBoardOverlay();
    }, 0);
  }

  if (btnFlipGame) btnFlipGame.addEventListener("click", flipReviewBoard);
  const reviewFlipBtn = $("#reviewFlip");
  if (reviewFlipBtn) reviewFlipBtn.addEventListener("click", flipReviewBoard);

  btnAnalyzeFen.addEventListener("click", () => {
    const fen = fenInput.value.trim();
    if (!validateFen(fen)) { editorStatus.textContent = "Invalid FEN."; return; }
    analyzeFen(fen).catch((err) => { editorStatus.textContent = `Error: ${err.message || err}`; });
  });

  fenInput.addEventListener("change", () => {
    const fen = fenInput.value.trim();
    if (validateFen(fen)) {
      syncBoardFromFen(fen);
      syncTurnToggleFromFen();
      editorStatus.textContent = "";
      scheduleAutoAnalyze();
    } else {
      syncTurnToggleFromFen();
      editorStatus.textContent = "Invalid FEN.";
    }
  });

  copyFenBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(fenInput.value);
      copyFenBtn.title = "Copied!";
      setTimeout(() => (copyFenBtn.title = "Copy FEN to clipboard"), 1200);
    } catch (_) { /* denied */ }
  });

  // ── Bar rendering ─────────────────────────────────────────────────────── //

  function evalCpToFill(cpWhitePov) {
    if (cpWhitePov == null) return 0.5;
    const clamped = Math.max(-10000, Math.min(10000, cpWhitePov));
    return 1 / (1 + Math.exp(-0.00368208 * clamped));
  }

  function formatEval(cp, turn) {
    if (cp == null) return "—";
    const w = turn === "b" ? -cp : cp;
    if (Math.abs(w) >= 1000) return (w > 0 ? "+" : "−") + "M";
    const abs = (Math.abs(w) / 100).toFixed(2);
    return (w >= 0 ? "+" : "−") + abs;
  }

  function scoreToColor(score) {
    if (score < 25) return "low";
    if (score < 60) return "medium";
    return "high";
  }

  function renderEvalBar(cpSideToMove, turn) {
    const w = turn === "b" ? -cpSideToMove : cpSideToMove;
    evalBarEl.style.setProperty("--fill", evalCpToFill(w));
    evalLabelEl.textContent = formatEval(cpSideToMove, turn);
  }

  // Last rendered volatility JSON — re-used on tab switch so the active
  // tab's explain panel always shows the current explanation (instead of
  // silently going stale because it was hidden when the data arrived).
  let lastVolJson = null;
  let lastClassificationJson = null;

  function renderVolBar(result, classification) {
    lastVolJson = result || null;
    lastClassificationJson = classification || null;
    if (!result || result.score == null) {
      volBarEl.style.setProperty("--fill", 0);
      volBarEl.style.setProperty("--local", 0);
      volBarEl.style.setProperty("--split-visible", 0);
      volBarEl.dataset.color = "low";
      volBarEl.dataset.decided = "false";
      volLabelEl.textContent = result && result.reason ? `— ${result.reason}` : "—";
      renderExplain(result, classification);
      return;
    }

    const score = result.score;
    const fill = Math.max(0, Math.min(1, score / 100));
    volBarEl.style.setProperty("--fill", fill);
    volBarEl.dataset.color = scoreToColor(score);
    volBarEl.dataset.decided = result.decided ? "true" : "false";

    const deep = result.recurse_depth_used > 0 && result.raw_cp > 0 && result.local_raw_cp != null;
    if (deep) {
      const localFrac = Math.max(0, Math.min(1, result.local_raw_cp / result.raw_cp));
      volBarEl.style.setProperty("--local", fill * localFrac);
      volBarEl.style.setProperty("--split-visible", 1);
      const lPct = Math.round(100 * fill * localFrac);
      const rPct = Math.round(100 * fill * (1 - localFrac));
      volLabelEl.textContent = `${score.toFixed(1)} L${lPct}/R${rPct}`;
    } else {
      volBarEl.style.setProperty("--local", 0);
      volBarEl.style.setProperty("--split-visible", 0);
      volLabelEl.textContent = score.toFixed(1);
    }

    renderExplain(result, classification);
  }

  // ── Explain panel ─────────────────────────────────────────────────────── //
  // Pretty labels for pattern badges. Names match `chess_vol.explain`
  // PATTERN_* constants — keep in sync if those identifiers change.
  const PATTERN_LABELS = {
    only_move: "only move",
    checkmate: "checkmate",
    stalemate: "stalemate",
    mate_available: "mate available",
    decided: "decided",
    knife_edge: "knife edge",
    few_good_moves: "few good moves",
    forgiving: "forgiving",
    reply_dominates: "reply dominates",
    scale_dampened: "winning · dampened",
    defensive_crisis: "defensive crisis",
  };

  // One-line "what to do about it" hints — appended below the summary so the
  // user gets actionable advice, not just diagnosis. Keyed off headline.
  const PATTERN_HINTS = {
    knife_edge:
      "Slow down — there's only one good move and the alternatives lose meaningfully.",
    defensive_crisis:
      "Calculate concretely. The position holds with one specific move; everything else loses.",
    few_good_moves:
      "Pick from the top engine lines — most other moves give back ground.",
    forgiving:
      "Play naturally. Many reasonable moves keep the evaluation.",
    reply_dominates:
      "The current move is easy, but the resulting position is the hard one — think a move ahead.",
    mate_available:
      "Look for the mating sequence rather than picking up material.",
    scale_dampened:
      "Winning is winning — pick whichever path you find most clearly.",
    decided:
      "Position is technically over; convert with the simplest plan.",
  };

  // Markdown-lite: convert **text** → <strong>text</strong>. Not full
  // markdown — the explainer only ever emits this one inline emphasis form.
  function renderInlineEmphasis(text) {
    const span = document.createElement("span");
    const parts = text.split(/(\*\*[^*]+\*\*)/g);
    for (const part of parts) {
      if (part.startsWith("**") && part.endsWith("**") && part.length >= 4) {
        const strong = document.createElement("strong");
        strong.textContent = part.slice(2, -2);
        span.appendChild(strong);
      } else if (part) {
        span.appendChild(document.createTextNode(part));
      }
    }
    return span;
  }

  // Which set of explain elements to write to — depends on the active tab.
  function activeExplainEls() {
    return gameTabActive() ? explainEls.game : explainEls.editor;
  }

  // Reset BOTH explain panels — used when clearing analysis state. Keeps the
  // inactive tab from showing stale content if the user switches back.
  function clearExplainPanels(message) {
    for (const els of Object.values(explainEls)) {
      if (!els.summary) continue;
      els.summary.textContent = message || "";
      els.badges.innerHTML = "";
      els.stack.hidden = true;
      els.bar.innerHTML = "";
      els.legend.innerHTML = "";
      els.hint.hidden = true;
      els.hint.textContent = "";
    }
  }

  function renderExplain(volJson, classification) {
    const els = activeExplainEls();
    if (!els.summary) return;

    if (!volJson || !volJson.explanation) {
      els.summary.textContent = "Analyze a position to see why its volatility is what it is.";
      els.badges.innerHTML = "";
      els.stack.hidden = true;
      els.bar.innerHTML = "";
      els.legend.innerHTML = "";
      els.hint.hidden = true;
      return;
    }

    const ex = volJson.explanation;

    // Summary
    els.summary.innerHTML = "";
    if (classification && classification.summary) {
      const classSummary = document.createElement("span");
      classSummary.className = "move-class-summary";
      classSummary.textContent = classification.summary;
      els.summary.appendChild(classSummary);
    }
    els.summary.appendChild(renderInlineEmphasis(ex.summary || ""));

    // Badges — headline first, then any other patterns. Skip duplicates.
    els.badges.innerHTML = "";
    const seen = new Set();
    const badgeOrder = [];
    if (ex.headline_pattern) badgeOrder.push(ex.headline_pattern);
    for (const p of ex.patterns || []) {
      if (!seen.has(p) && p !== ex.headline_pattern) badgeOrder.push(p);
      seen.add(p);
    }
    for (const p of badgeOrder) {
      const b = document.createElement("span");
      b.className = "vol-explain-badge";
      b.dataset.pattern = p;
      b.textContent = PATTERN_LABELS[p] || p;
      els.badges.appendChild(b);
    }

    // Components: stacked bar + legend. Only render if we have at least one
    // additive component with a non-zero value.
    const components = (ex.components || []).filter((c) => c && c.value > 0);
    if (components.length === 0) {
      els.stack.hidden = true;
      els.bar.innerHTML = "";
      els.legend.innerHTML = "";
    } else {
      els.stack.hidden = false;
      els.bar.innerHTML = "";
      els.legend.innerHTML = "";

      const adds = components.filter((c) => c.direction === "adds");
      const removes = components.filter((c) => c.direction === "removes");
      const totalAdds = adds.reduce((a, c) => a + c.value, 0) || 1;

      // Stacked bar shows additive components proportionally.
      for (const c of adds) {
        const seg = document.createElement("div");
        seg.className = "vol-explain-stack-seg";
        seg.dataset.name = c.name;
        seg.style.width = `${(c.value / totalAdds) * 100}%`;
        seg.title = `${c.label}: ${c.value.toFixed(1)}`;
        els.bar.appendChild(seg);
      }

      // Legend shows ALL components — adds *and* the dampening "removed" one
      // so the user understands why the bar isn't bigger than it is.
      for (const c of [...adds, ...removes]) {
        const li = document.createElement("li");

        const dot = document.createElement("span");
        dot.className = "vol-explain-legend-dot";
        dot.dataset.name = c.name;

        const name = document.createElement("span");
        name.className = "vol-explain-legend-name";
        name.textContent = c.label;
        name.title = c.detail || "";

        const value = document.createElement("span");
        value.className = "vol-explain-legend-value";
        const sign = c.direction === "removes" ? "−" : "+";
        value.textContent = `${sign}${c.value.toFixed(1)}`;

        li.appendChild(dot);
        li.appendChild(name);
        li.appendChild(value);
        els.legend.appendChild(li);
      }
    }

    // Optional "what to do" hint — only when we have a headline pattern with
    // a registered hint. Keeps the panel quiet for the generic case.
    const hint = PATTERN_HINTS[ex.headline_pattern];
    if (hint) {
      els.hint.textContent = hint;
      els.hint.hidden = false;
    } else {
      els.hint.hidden = true;
      els.hint.textContent = "";
    }
  }

  // Click the bar → flash the explanation panel so users discover where the
  // explanation lives. The panel is always visible, so we don't toggle it,
  // we just draw attention to it.
  volBarEl.addEventListener("click", () => {
    const els = activeExplainEls();
    if (!els.root) return;
    els.root.classList.remove("flash");
    // Force reflow so re-adding the class restarts the animation.
    void els.root.offsetWidth;
    els.root.classList.add("flash");
    els.root.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  function setBarsLoading(on) {
    [evalBarEl, volBarEl].forEach((el) =>
      el.classList.toggle("bar-loading", on)
    );
  }

  // ── Arrow overlay ─────────────────────────────────────────────────────── //
  let lastTopMoveUci = null;

  function squareCenter(sq) {
    if (!boardFrameEl || !sq || sq.length < 2) return null;
    // Must be the vol board's wrap — `#board` is the (hidden) puzzles board.
    const cgWrap = boardFrameEl.querySelector(".cg-wrap");
    if (!cgWrap) return null;
    const file = sq.charCodeAt(0) - 97; // 'a' → 0 .. 'h' → 7
    const rank = parseInt(sq[1], 10);
    if (file < 0 || file > 7 || !Number.isFinite(rank)) return null;
    const orientation = (board && board._cg && board._cg.state && board._cg.state.orientation) || "white";
    const frameRect = boardFrameEl.getBoundingClientRect();
    const wrapRect = cgWrap.getBoundingClientRect();
    const cellW = wrapRect.width / 8;
    const cellH = wrapRect.height / 8;
    const colFromLeft = orientation === "white" ? file : 7 - file;
    const rowFromTop = orientation === "white" ? 8 - rank : rank - 1;
    return {
      x: wrapRect.left - frameRect.left + (colFromLeft + 0.5) * cellW,
      y: wrapRect.top - frameRect.top + (rowFromTop + 0.5) * cellH,
      size: cellW,
    };
  }

  function clearArrow() {
    if (!arrowLayer) return;
    while (arrowLayer.firstChild) arrowLayer.removeChild(arrowLayer.firstChild);
  }

  // The review arrow always points at the BEST move, so it is always drawn in
  // the accent green ("play this") — never tinted by how the actual move scored,
  // which previously made the best-move arrow turn red after a blunder.
  function currentReviewArrowColor() {
    return "#44d62c";
  }

  function drawArrow(uci, color) {
    if (!arrowLayer || !uci || uci.length < 4) { clearArrow(); return; }
    const from = uci.slice(0, 2);
    const to = uci.slice(2, 4);
    const a = squareCenter(from);
    const b = squareCenter(to);
    if (!a || !b) { clearArrow(); return; }

    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len = Math.hypot(dx, dy);
    if (len < 1) { clearArrow(); return; }
    const ux = dx / len;
    const uy = dy / len;

    const sq = a.size;
    const w = Math.max(6, sq * 0.18);
    const head = Math.max(10, sq * 0.34);
    const inset = sq * 0.22;

    const sx = a.x + ux * inset;
    const sy = a.y + uy * inset;
    const ex = b.x - ux * inset;
    const ey = b.y - uy * inset;

    const shaftEndX = ex - ux * head;
    const shaftEndY = ey - uy * head;

    const px = uy;
    const py = -ux;

    const hw = w * 0.5;
    const shaft = document.createElementNS(SVG_NS, "polygon");
    shaft.setAttribute(
      "points",
      [
        `${sx + px * hw},${sy + py * hw}`,
        `${shaftEndX + px * hw},${shaftEndY + py * hw}`,
        `${shaftEndX - px * hw},${shaftEndY - py * hw}`,
        `${sx - px * hw},${sy - py * hw}`,
      ].join(" ")
    );
    shaft.setAttribute("class", "arrow-shaft");

    const hhw = w * 1.1;
    const headPoly = document.createElementNS(SVG_NS, "polygon");
    headPoly.setAttribute(
      "points",
      [
        `${ex},${ey}`,
        `${shaftEndX + px * hhw},${shaftEndY + py * hhw}`,
        `${shaftEndX - px * hhw},${shaftEndY - py * hhw}`,
      ].join(" ")
    );
    headPoly.setAttribute("class", "arrow-head");

    if (color) {
      shaft.setAttribute("style", `fill:${color};stroke:${color}`);
      headPoly.setAttribute("style", `fill:${color};stroke:${color}`);
    }

    clearArrow();
    arrowLayer.appendChild(shaft);
    arrowLayer.appendChild(headPoly);
  }

  function refreshArrow() {
    if (!arrowEnabled() || !lastTopMoveUci) { clearArrow(); return; }
    drawArrow(lastTopMoveUci, currentReviewArrowColor());
  }

  function setTopMove(uci) {
    lastTopMoveUci = uci || null;
    refreshArrow();
  }

  // ── Engine lines panel ────────────────────────────────────────────────── //
  function formatEvalSigned(cp, turn) {
    if (cp == null) return "—";
    const w = turn === "b" ? -cp : cp;
    if (Math.abs(w) >= 1000) return (w > 0 ? "+" : "−") + "M";
    const abs = (Math.abs(w) / 100).toFixed(2);
    return (w >= 0 ? "+" : "−") + abs;
  }

  function activeTopLinesEl() {
    return gameTabActive() ? topLinesListGame : topLinesList;
  }

  function clearTopLinesLists() {
    [topLinesList, topLinesListGame].forEach((el) => {
      if (el) el.innerHTML = "";
    });
  }

  function renderTopLines(volJson, turn) {
    clearTopLinesLists();
    const target = activeTopLinesEl();
    if (!target) return;
    const lines = (volJson && volJson.top_lines) || [];
    if (!lines.length) return;

    lines.forEach((line, idx) => {
      const li = document.createElement("li");
      li.className = "top-line" + (idx === 0 ? " best" : "");

      const evalSpan = document.createElement("span");
      evalSpan.className = "top-line-eval";
      evalSpan.textContent = formatEvalSigned(line.eval_cp, turn);
      const w = turn === "b" ? -line.eval_cp : line.eval_cp;
      evalSpan.dataset.sign = w > 30 ? "pos" : w < -30 ? "neg" : "neutral";

      const pvSpan = document.createElement("span");
      pvSpan.className = "top-line-pv";
      const pv = Array.isArray(line.pv_san) ? line.pv_san.slice(0, 6) : [line.san];
      pvSpan.textContent = pv.join(" ");
      pvSpan.title = Array.isArray(line.pv_san) ? line.pv_san.join(" ") : line.san;

      li.appendChild(evalSpan);
      li.appendChild(pvSpan);
      target.appendChild(li);
    });
  }

  // ── Analyze FEN ──────────────────────────────────────────────────────── //
  // `inflightFen` is hoisted near the top so invalidateAnalysis() can abort it.

  async function analyzeFen(fen) {
    if (inflightFen) {
      try { inflightFen.abort(); } catch (_) { /* ignore */ }
    }
    const ctrl = new AbortController();
    inflightFen = ctrl;
    editorStatus.textContent = "Analyzing…";
    setBarsLoading(true);

    let fullArrived = false;

    // Fast eval probe — shallow depth, no recursion → ~50ms.
    // Shows the eval bar instantly while the full analysis runs.
    const quickEval = fetch("/analyze/fen", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen, deep: false, depth: 8, multipv: 1, recurse_depth: 0 }),
      signal: ctrl.signal,
    }).then(async (resp) => {
      if (!resp.ok || ctrl.signal.aborted || ctrl !== inflightFen) return;
      const data = await resp.json();
      if (fullArrived || ctrl.signal.aborted || ctrl !== inflightFen) return;
      const turn = fen.trim().split(/\s+/)[1] || "w";
      renderEvalBar(data.volatility.best_eval_cp, turn);
      editorStatus.textContent = "Eval ready · computing volatility…";
    }).catch(() => { /* swallow — full request is the source of truth */ });

    try {
      const resp = await fetch("/analyze/fen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fen, deep: deepEnabled() }),
        signal: ctrl.signal,
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      const data = await resp.json();
      fullArrived = true;
      // Stale-response guard: if the user edited the board (or a newer
      // analysis started) while this request was in flight, our controller
      // was replaced in inflightFen and/or aborted. Do not let this response
      // overwrite the cleared UI with lines that belong to an older FEN.
      if (ctrl !== inflightFen || ctrl.signal.aborted) return;
      const turn = fen.trim().split(/\s+/)[1] || "w";
      renderEvalBar(data.volatility.best_eval_cp, turn);
      renderVolBar(data.volatility);
      renderTopLines(data.volatility, turn);
      const topUci = data.volatility.top_lines && data.volatility.top_lines[0]
        ? data.volatility.top_lines[0].uci
        : null;
      setTopMove(topUci);
      const modeStr = data.mode === "deep" ? "deep" : "shallow";
      editorStatus.textContent =
        `${modeStr} · ${data.volatility.analyses} engine call${data.volatility.analyses !== 1 ? "s" : ""}`;
      lastAnalyzedFen = fen;
    } catch (err) {
      if (err.name === "AbortError") return;
      throw err;
    } finally {
      setBarsLoading(false);
      if (inflightFen === ctrl) inflightFen = null;
    }
  }

  // ── PGN / Game ───────────────────────────────────────────────────────── //
  let chart = null;
  let pgnController = null;
  let gameReviewSummary = null;

  function resetGame() {
    loadedPlies = [];
    plyResults = [];
    currentPlyIdx = -1;
    moveListEl.innerHTML = "";
    gameStatus.textContent = "";
    plyStatus.textContent = "";
    chartWrap.classList.add("hidden");
    moveListWrap.classList.add("hidden");
    if (gameStatsEl) gameStatsEl.classList.add("hidden");
    if (reviewPlayerHeader) reviewPlayerHeader.classList.add("hidden");
    if (reviewCoachText) reviewCoachText.textContent = "Analyze a game to receive coaching feedback.";
    if (reviewOpening) { reviewOpening.innerHTML = ""; reviewOpening.classList.add("hidden"); }
    if (reviewKeyMoments) { reviewKeyMoments.innerHTML = ""; reviewKeyMoments.classList.add("hidden"); }
    if (reviewMoveCard) reviewMoveCard.classList.add("hidden");
    if (reviewNavigation) reviewNavigation.classList.add("hidden");
    if (gameClassCard) gameClassCard.classList.add("hidden");
    if (reviewWhiteStripName) reviewWhiteStripName.textContent = "White";
    if (reviewBlackStripName) reviewBlackStripName.textContent = "Black";
    if (boardFrameEl) delete boardFrameEl.dataset.reviewClass;
    if (ReviewUI) ReviewUI.clearBoardOverlay(reviewBoardOverlay);
    gameReviewSummary = null;
    resetGameStats();
    clearTopLinesLists();
    setTopMove(null);
    clearLastMoveDecor();
    destroyChart();
    // Clear heat band
    const hb = document.getElementById("heatBand");
    if (hb) { hb.innerHTML = ""; hb.classList.add("hidden"); }
    lastVolJson = null;
    lastClassificationJson = null;
    clearExplainPanels(
      "Load and analyze a game to see why each move's volatility is what it is.",
    );
    expandPgnDrawer();
  }

  // ── Game stats (accuracy + avg volatility) ───────────────────────────── //
  function resetGameStats() {
    if (statWhiteAcc) statWhiteAcc.textContent = "—";
    if (statBlackAcc) statBlackAcc.textContent = "—";
    if (statAvgVol) statAvgVol.textContent = "—";
    if (statAvgVol) statAvgVol.removeAttribute("data-color");
    setAccuracyDonut(reviewWhiteArc, null);
    setAccuracyDonut(reviewBlackArc, null);
    renderClassCountSide(statClassWhite, "white", {});
    renderClassCountSide(statClassBlack, "black", {});
    renderClassTable({}, {});
  }

  function setAccuracyDonut(circle, accuracy) {
    if (!circle) return;
    const circumference = 2 * Math.PI * 16;
    const pct = typeof accuracy === "number" ? Math.max(0, Math.min(100, accuracy)) : 0;
    circle.style.strokeDasharray = `${circumference}`;
    circle.style.strokeDashoffset = `${circumference * (1 - pct / 100)}`;
  }

  function renderReviewSummary(summary) {
    if (!summary) return;
    const accuracy = summary.accuracy || {};
    const players = summary.players || {};
    const estimated = summary.estimated_elo || {};
    if (reviewWhiteName) reviewWhiteName.textContent = players.white || "White";
    if (reviewBlackName) reviewBlackName.textContent = players.black || "Black";
    if (reviewWhiteStripName) reviewWhiteStripName.textContent = players.white || "White";
    if (reviewBlackStripName) reviewBlackStripName.textContent = players.black || "Black";
    if (reviewWhiteElo) {
      reviewWhiteElo.textContent = `Estimated performance ${estimated.white || "—"}`;
    }
    if (reviewBlackElo) {
      reviewBlackElo.textContent = `Estimated performance ${estimated.black || "—"}`;
    }
    setAccuracyDonut(reviewWhiteArc, accuracy.white);
    setAccuracyDonut(reviewBlackArc, accuracy.black);
    if (reviewCoachText) reviewCoachText.textContent = summary.coach || "";
    renderOpening(summary.opening);
    renderKeyMoments(summary.key_moments);
    if (reviewPlayerHeader) reviewPlayerHeader.classList.remove("hidden");
    if (reviewCoach) reviewCoach.classList.remove("hidden");
    if (gameClassCard) gameClassCard.classList.remove("hidden");
  }

  function renderOpening(opening) {
    if (!reviewOpening) return;
    if (!opening || !opening.name) {
      reviewOpening.classList.add("hidden");
      reviewOpening.innerHTML = "";
      return;
    }
    reviewOpening.innerHTML = "";
    const label = document.createElement("span");
    label.className = "rs-opening-label";
    label.textContent = "Opening";
    const name = document.createElement("strong");
    name.className = "rs-opening-name";
    name.textContent = opening.eco ? `${opening.name} · ${opening.eco}` : opening.name;
    reviewOpening.append(label, name);
    reviewOpening.classList.remove("hidden");
  }

  // Reveal the move-by-move view (as "Start Review" does) and land on a ply.
  function goToReviewPly(idx) {
    if (!loadedPlies.length) return;
    if (moveListWrap) moveListWrap.classList.remove("hidden");
    if (reviewNavigation) reviewNavigation.classList.remove("hidden");
    jumpToPly(idx);
    if (reviewMoveCard) {
      reviewMoveCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function renderKeyMoments(moments) {
    if (!reviewKeyMoments) return;
    reviewKeyMoments.innerHTML = "";
    if (!moments || !moments.length) {
      reviewKeyMoments.classList.add("hidden");
      return;
    }
    const title = document.createElement("div");
    title.className = "rs-key-moments-title";
    title.textContent = "Key moments";
    reviewKeyMoments.appendChild(title);

    const list = document.createElement("div");
    list.className = "rs-key-moments-list";
    for (const m of moments) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "rs-key-moment";
      chip.dataset.kind = m.classification || "";
      const style = ReviewUI ? ReviewUI.getClassificationStyle(m.classification) : null;
      if (style) chip.style.setProperty("--km-color", style.backgroundColor);
      chip.title = m.reason || "";

      const badge = ReviewUI ? ReviewUI.renderBadgeIcon(m.classification, "rs-key-moment-badge") : null;
      if (badge) chip.appendChild(badge);

      const moveNo = Math.floor((m.ply - 1) / 2) + 1;
      const sep = m.side === "black" ? "…" : ".";
      const move = document.createElement("span");
      move.className = "rs-key-moment-move";
      move.textContent = `${moveNo}${sep}${m.san}`;
      chip.appendChild(move);

      const swing = document.createElement("span");
      swing.className = "rs-key-moment-swing";
      swing.textContent = `−${m.swing_pct}%`;
      chip.appendChild(swing);

      const targetIdx = typeof m.index === "number" ? m.index : m.ply - 1;
      chip.addEventListener("click", () => goToReviewPly(targetIdx));
      list.appendChild(chip);
    }
    reviewKeyMoments.appendChild(list);
    reviewKeyMoments.classList.remove("hidden");
  }

  const CLASS_LABELS = {
    brilliant: "brilliant",
    great: "great",
    best: "best",
    excellent: "excellent",
    good: "good",
    book: "book",
    inaccuracy: "inaccuracy",
    mistake: "mistake",
    miss: "miss",
    blunder: "blunder",
    routine_miss: "routine miss",
    critical_miss: "critical miss",
    practical: "practical",
    simplification: "simplification",
    defusal: "defusal",
    complication: "complication",
  };

  const CLASS_ORDER = [
    "brilliant",
    "great",
    "best",
    "excellent",
    "good",
    "book",
    "inaccuracy",
    "mistake",
    "miss",
    "blunder",
    "routine_miss",
    "critical_miss",
    "practical",
    "simplification",
    "defusal",
    "complication",
  ];

  function formatClassCounts(side, counts) {
    const parts = [];
    for (const key of CLASS_ORDER) {
      const n = counts[key] || 0;
      if (!n) continue;
      parts.push(`${n} ${CLASS_LABELS[key] || key}`);
    }
    const label = side === "white" ? "White" : "Black";
    return `${label}: ${parts.length ? parts.join(", ") : "—"}`;
  }

  // Chess.com-style breakdown: one row per classification with the icon in the
  // middle and each side's count flanking it.
  const CLASS_TABLE_ORDER = [
    "brilliant",
    "great",
    "book",
    "best",
    "excellent",
    "good",
    "inaccuracy",
    "mistake",
    "miss",
    "blunder",
  ];

  function renderClassTable(whiteCounts, blackCounts) {
    if (!reviewClassTable) return;
    const w = whiteCounts || {};
    const b = blackCounts || {};
    reviewClassTable.innerHTML = "";
    for (const kind of CLASS_TABLE_ORDER) {
      const style = ReviewUI ? ReviewUI.getClassificationStyle(kind) : null;
      const color = style ? style.backgroundColor : "#9c9891";

      const row = document.createElement("div");
      row.className = "rs-class-row";
      row.dataset.kind = kind;

      const label = document.createElement("span");
      label.className = "rs-class-label";
      label.textContent = style ? style.label : kind;

      const wc = document.createElement("span");
      wc.className = "rs-cc rs-cc--white";
      wc.textContent = String(w[kind] || 0);
      wc.style.color = color;

      const icon = document.createElement("span");
      icon.className = "rs-cc-icon";
      if (ReviewUI) {
        const img = ReviewUI.renderBadgeIcon(kind, "rs-class-badge");
        if (img) icon.appendChild(img);
      }

      const bc = document.createElement("span");
      bc.className = "rs-cc rs-cc--black";
      bc.textContent = String(b[kind] || 0);
      bc.style.color = color;

      row.append(label, wc, icon, bc);
      reviewClassTable.appendChild(row);
    }
  }

  function renderClassCountSide(el, side, counts) {
    if (!el) return;
    const labelEl = document.getElementById(
      side === "white" ? "statClassWhiteLabel" : "statClassBlackLabel",
    );
    if (labelEl) labelEl.textContent = side === "white" ? "White" : "Black";
    if (ReviewUI && typeof ReviewUI.renderClassificationPills === "function") {
      ReviewUI.renderClassificationPills(el, counts);
      return;
    }
    el.textContent = formatClassCounts(side, counts || {});
  }

  // Recompute over whatever plies have streamed in so far. Cheap (just an
  // array sweep) so we run it on every onPly tick — the panel updates live
  // as analysis progresses instead of waiting for done.
  function recomputeGameStats() {
    if (!gameStatsEl) return;
    const stats = window.ChessVolLibrary.computeGameStats(plyResults);
    const fmtAcc = (value) => (typeof value === "number" ? value.toFixed(1) : "—");
    const reviewAccuracy = gameReviewSummary && gameReviewSummary.accuracy;

    if (statWhiteAcc) statWhiteAcc.textContent = fmtAcc(reviewAccuracy ? reviewAccuracy.white : stats.whiteAcc);
    if (statBlackAcc) statBlackAcc.textContent = fmtAcc(reviewAccuracy ? reviewAccuracy.black : stats.blackAcc);
    setAccuracyDonut(reviewWhiteArc, reviewAccuracy ? reviewAccuracy.white : stats.whiteAcc);
    setAccuracyDonut(reviewBlackArc, reviewAccuracy ? reviewAccuracy.black : stats.blackAcc);

    if (statAvgVol) {
      if (typeof stats.avgV === "number") {
        statAvgVol.textContent = stats.avgV.toFixed(1);
        statAvgVol.dataset.color = scoreToColor(stats.avgV);
      } else {
        statAvgVol.textContent = "—";
        statAvgVol.removeAttribute("data-color");
      }
    }
    const reviewCounts = gameReviewSummary && gameReviewSummary.classification_counts;
    const whiteClassCounts = (reviewCounts && reviewCounts.white) || stats.classificationCounts.white;
    const blackClassCounts = (reviewCounts && reviewCounts.black) || stats.classificationCounts.black;
    renderClassCountSide(statClassWhite, "white", whiteClassCounts);
    renderClassCountSide(statClassBlack, "black", blackClassCounts);
    renderClassTable(whiteClassCounts, blackClassCounts);

    if (gameReviewSummary) renderReviewSummary(gameReviewSummary);
    gameStatsEl.classList.remove("hidden");
  }

  function destroyChart() {
    if (chart) { chart.destroy(); chart = null; }
  }

  function parsePgn(text) {
    try {
      const g = new Chess();
      if (!g.load_pgn(text, { sloppy: true })) return null;
      const history = g.history({ verbose: true });
      const replay = new Chess();
      const plies = [];
      for (const mv of history) {
        const fenBefore = replay.fen();
        const san = replay.move({ from: mv.from, to: mv.to, promotion: mv.promotion }).san;
        const move_uci = mv.from + mv.to + (mv.promotion || "");
        plies.push({
          san,
          fen_before: fenBefore,
          fen_after: replay.fen(),
          from: mv.from,
          to: mv.to,
          move_uci,
        });
      }
      return plies;
    } catch (_) { return null; }
  }

  function renderMoveList() {
    moveListEl.innerHTML = "";
    if (!loadedPlies.length) return;

    const table = document.createElement("table");
    table.className = "move-table";

    const pairCount = Math.ceil(loadedPlies.length / 2);
    for (let i = 0; i < pairCount; i++) {
      const tr = document.createElement("tr");

      const numTd = document.createElement("td");
      numTd.className = "move-num";
      numTd.textContent = `${i + 1}.`;
      tr.appendChild(numTd);

      tr.appendChild(makeMoveCell(i * 2));

      if (loadedPlies[i * 2 + 1]) {
        tr.appendChild(makeMoveCell(i * 2 + 1));
      } else {
        tr.appendChild(document.createElement("td"));
      }

      table.appendChild(tr);
    }

    moveListEl.appendChild(table);
    moveListWrap.classList.remove("hidden");
  }

  function makeMoveCell(idx) {
    const ply = loadedPlies[idx];
    const td = document.createElement("td");
    td.className = "move-cell";
    td.dataset.idx = String(idx);

    const sanSpan = document.createElement("span");
    sanSpan.className = "move-san";
    sanSpan.textContent = ply.san;

    const vSpan = document.createElement("span");
    vSpan.className = "move-vscore";
    vSpan.id = `mv-v-${idx}`;
    vSpan.textContent = "—";

    const classSpan = document.createElement("span");
    classSpan.className = "move-class-icon";
    classSpan.id = `mv-class-${idx}`;

    td.appendChild(sanSpan);
    td.appendChild(vSpan);
    td.appendChild(classSpan);
    td.addEventListener("click", () => jumpToPly(idx));
    return td;
  }

  function classificationGlyph(classification) {
    if (!classification) return null;
    const primaryGlyphs = {
      brilliant: "!!",
      great: "!",
      best: "★",
      excellent: "👍",
      good: "✓",
      book: "📖",
      inaccuracy: "?!",
      mistake: "?",
      miss: "❌",
      blunder: "??",
    };
    const secondaryGlyphs = {
      routine_miss: "⚠",
      critical_miss: "✗",
      practical: "↑",
      simplification: "↓",
      defusal: "⊘",
      complication: "⚡",
    };
    const reviewKind = classification.classification;
    const primaryKind = reviewKind || classification.primary;
    const primary = classification.symbol || primaryGlyphs[primaryKind];
    const secondary = secondaryGlyphs[classification.secondary];
    const text = [primary, secondary].filter(Boolean).join(" ");
    if (!text) return null;
    return {
      text,
      kind: primary ? primaryKind : classification.secondary,
      color: classification.color || null,
    };
  }

  function updateMoveClassification(idx, classification) {
    const span = document.getElementById(`mv-class-${idx}`);
    if (!span) return;
    const glyph = classificationGlyph(classification);
    if (!glyph) {
      span.textContent = "";
      delete span.dataset.kind;
      span.style.color = "";
      span.removeAttribute("title");
      return;
    }
    span.innerHTML = "";
    span.dataset.kind = glyph.kind;
    span.style.color = glyph.color || "";
    span.title = classification.coach || classification.summary || glyph.kind;
    const badge =
      ReviewUI && ReviewUI.renderBadgeIcon(glyph.kind, "review-badge-icon review-badge-icon--list");
    if (badge) {
      span.appendChild(badge);
      const secondary = glyph.text.replace(/^[^\s]+\s*/, "").trim();
      if (secondary && secondary !== glyph.text) {
        const extra = document.createElement("span");
        extra.className = "move-class-extra";
        extra.textContent = secondary;
        span.appendChild(extra);
      }
      return;
    }
    span.textContent = glyph.text;
  }

  function updateMoveVol(idx, score) {
    const span = document.getElementById(`mv-v-${idx}`);
    if (!span) return;
    if (score == null) { span.textContent = "—"; delete span.dataset.color; return; }
    span.textContent = Math.round(score).toString();
    span.dataset.color = scoreToColor(score);
  }

  function renderCurrentMoveReview(idx, result, entry) {
    if (!reviewMoveCard || !result || !result.ply) return;
    const review = result.ply.review;
    if (!review) {
      reviewMoveCard.classList.add("hidden");
      return;
    }
    const moveNumber = Math.floor(idx / 2) + 1;
    const side = idx % 2 === 0 ? "White" : "Black";
    reviewMoveNumber.textContent = `${side} · Move ${moveNumber}`;
    reviewMoveSan.textContent = entry.san || result.ply.san || "—";
    reviewMoveBadge.innerHTML = "";
    reviewMoveBadge.dataset.kind = review.classification || "";
    reviewMoveBadge.style.backgroundColor = review.color || "";
    const badgeIcon =
      ReviewUI &&
      ReviewUI.renderBadgeIcon(review.classification, "review-badge-icon review-badge-icon--card");
    if (badgeIcon) reviewMoveBadge.appendChild(badgeIcon);
    const badgeLabel = document.createElement("span");
    badgeLabel.textContent = `${review.symbol || ""} ${review.classification || ""}`.trim();
    reviewMoveBadge.appendChild(badgeLabel);
    reviewMoveAccuracy.textContent =
      typeof review.accuracy === "number" ? `${review.accuracy.toFixed(1)}%` : "—";
    const evalCp = review.eval_after_cp_white;
    reviewMoveEval.textContent =
      typeof evalCp === "number"
        ? `${evalCp >= 0 ? "+" : "−"}${(Math.abs(evalCp) / 100).toFixed(2)}`
        : "—";
    reviewMoveWin.textContent =
      typeof review.expected_points_after === "number"
        ? `${(review.expected_points_after * 100).toFixed(1)}%`
        : "—";
    reviewMoveLoss.textContent =
      typeof review.expected_points_loss === "number"
        ? `${(review.expected_points_loss * 100).toFixed(1)} pts`
        : "—";
    const findability = result.ply.findability;
    // The findability panel now surfaces the best move prominently, so the small
    // "Top engine move" row is only needed as a fallback when findability is
    // gated out (book / terminal / decided positions).
    if (!findability && review.best_move_san && review.best_move_san !== entry.san) {
      reviewBestMove.textContent = review.best_move_san;
      reviewBestMoveRow.classList.remove("hidden");
    } else {
      reviewBestMoveRow.classList.add("hidden");
    }
    const findPanel = ensureFindabilityPanel();
    if (findPanel && ReviewUI && ReviewUI.renderFindability) {
      ReviewUI.renderFindability(findPanel, findability, review.best_move_san);
    }
    reviewMoveCard.classList.remove("hidden");
  }

  let findabilityPanelEl = null;
  function ensureFindabilityPanel() {
    if (findabilityPanelEl && findabilityPanelEl.isConnected) return findabilityPanelEl;
    if (!reviewMoveCard) return null;
    let el = reviewMoveCard.querySelector("#reviewFindability");
    if (!el) {
      el = document.createElement("div");
      el.id = "reviewFindability";
      el.className = "review-find hidden";
      reviewMoveCard.appendChild(el);
    }
    findabilityPanelEl = el;
    return el;
  }

  function updateReviewNavigation(idx) {
    if (!reviewNavigation) return;
    const total = loadedPlies.length;
    reviewMoveCounter.textContent = total ? `${idx + 1} / ${total}` : "— / —";
    reviewFirst.disabled = idx <= 0;
    reviewPrevious.disabled = idx <= 0;
    reviewNext.disabled = idx >= total - 1;
    reviewLast.disabled = idx >= total - 1;
    reviewNavigation.classList.toggle("hidden", !total);
  }

  function refreshReviewBoardOverlay() {
    if (!ReviewUI || !reviewBoardOverlay) return;
    if (currentPlyIdx < 0 || currentPlyIdx >= loadedPlies.length) {
      ReviewUI.clearBoardOverlay(reviewBoardOverlay);
      return;
    }
    const entry = loadedPlies[currentPlyIdx];
    const result = plyResults[currentPlyIdx];
    const review = result && result.ply && result.ply.review;
    const uci = entry && entry.move_uci;
    const dest = uci && uci.length >= 4 ? uci.slice(2, 4) : null;
    const orientation =
      (board && typeof board.orientation === "function" && board.orientation()) || "white";
    if (review && review.classification && dest) {
      ReviewUI.renderBoardOverlay(
        reviewBoardOverlay,
        review.classification,
        dest,
        orientation,
      );
    } else {
      ReviewUI.clearBoardOverlay(reviewBoardOverlay);
    }
  }

  function jumpToPly(idx) {
    if (idx < 0 || idx >= loadedPlies.length) return;
    const prevIdx = currentPlyIdx;
    currentPlyIdx = idx;
    const entry = loadedPlies[idx];

    suppressSync = true;
    try { board.position(entry.fen_after.split(/\s+/)[0], true); }
    finally { suppressSync = false; }

    // A review move is shown after it was played, with its classification color.
    const prevUci = entry.move_uci;
    if (board.setLastMove) {
      if (prevUci && prevUci.length >= 4) {
        board.setLastMove(prevUci.slice(0, 2), prevUci.slice(2, 4));
      } else {
        board.setLastMove(null, null);
      }
    }

    fenInput.value = entry.fen_after;
    syncTurnToggleFromFen();

    const r = plyResults[idx];
    if (r) {
      const turn = entry.fen_before.split(/\s+/)[1] || "w";
      const review = r.ply.review;
      if (review && typeof review.eval_after_cp_white === "number") {
        renderEvalBar(review.eval_after_cp_white, "w");
      } else {
        renderEvalBar(r.ply.volatility.best_eval_cp, turn);
      }
      renderVolBar(r.ply.volatility, r.ply.classification);
      renderTopLines(r.ply.volatility, turn);
      const tl = r.ply.volatility.top_lines;
      setTopMove(tl && tl[0] ? tl[0].uci : null);
      if (boardFrameEl) {
        if (review && review.classification) {
          boardFrameEl.dataset.reviewClass = review.classification;
        } else {
          delete boardFrameEl.dataset.reviewClass;
        }
      }
      if (reviewCoachText && review && review.coach) reviewCoachText.textContent = review.coach;
      if (reviewCoach && review) reviewCoach.classList.remove("hidden");
      renderCurrentMoveReview(idx, r, entry);
    } else {
      clearTopLinesLists();
      setTopMove(null);
      if (boardFrameEl) delete boardFrameEl.dataset.reviewClass;
      if (reviewMoveCard) reviewMoveCard.classList.add("hidden");
    }
    refreshReviewBoardOverlay();
    updateReviewNavigation(idx);

    document.querySelectorAll(".move-cell").forEach((c) =>
      c.classList.toggle("active", Number(c.dataset.idx) === idx)
    );

    const active = moveListEl.querySelector(".move-cell.active");
    if (active && moveListWrap && moveListWrap.contains(active)) {
      // Scroll only within the move-list's own container (.move-list-wrap is
      // overflow-y:auto with max-height). Using scrollIntoView() would also
      // scroll the window, which pushes the board off-screen when arrow-keying
      // through a game.
      const cRect = moveListWrap.getBoundingClientRect();
      const aRect = active.getBoundingClientRect();
      if (aRect.top < cRect.top) {
        moveListWrap.scrollTop += aRect.top - cRect.top;
      } else if (aRect.bottom > cRect.bottom) {
        moveListWrap.scrollTop += aRect.bottom - cRect.bottom;
      }
    }

    if (chart) {
      chart.$currentIdx = idx;
      chart.data.datasets.forEach((ds) => {
        ds.pointRadius = ds.data.map((_, i) => (i === idx ? 5 : 2));
        ds.pointHoverRadius = ds.data.map((_, i) => (i === idx ? 7 : 4));
      });
      chart.update("none");
    }

    // Heat band — highlight active cell
    const heatBand = document.getElementById("heatBand");
    if (heatBand) {
      heatBand.querySelectorAll(".active").forEach(c => c.classList.remove("active"));
      const cell = heatBand.children[idx];
      if (cell) cell.classList.add("active");
    }

    // The board shows the position after the selected review move. Play that
    // move's sound only while scrubbing forward.
    if (idx > prevIdx) {
      playMoveSound(loadedPlies[idx].san);
    }
  }

  // ── PGN drawer (collapsible) ─────────────────────────────────────────── //
  const pgnDrawer = document.getElementById("pgnDrawer");
  const pgnDrawerToggle = document.getElementById("pgnDrawerToggle");
  const pgnDrawerSummary = document.getElementById("pgnDrawerSummary");

  function extractPgnNames(text) {
    const wm = text.match(/\[White\s+"([^"]*)"\]/);
    const bm = text.match(/\[Black\s+"([^"]*)"\]/);
    return {
      white: wm ? wm[1] : "White",
      black: bm ? bm[1] : "Black",
    };
  }

  function collapsePgnDrawer(pgnText, plyCount) {
    if (!pgnDrawer) return;
    const { white, black } = extractPgnNames(pgnText || "");
    const moves = Math.ceil((plyCount || 0) / 2);
    pgnDrawerSummary.textContent = `${white} vs ${black} · ${moves} move${moves !== 1 ? "s" : ""}`;
    pgnDrawer.classList.add("collapsed");
  }

  function expandPgnDrawer() {
    if (!pgnDrawer) return;
    pgnDrawer.classList.remove("collapsed");
    pgnDrawerSummary.textContent = "Load a PGN to begin";
  }

  function togglePgnDrawer() {
    if (!pgnDrawer) return;
    if (pgnDrawer.classList.contains("collapsed")) {
      pgnDrawer.classList.remove("collapsed");
    } else {
      pgnDrawer.classList.add("collapsed");
    }
  }

  if (pgnDrawerToggle) {
    pgnDrawerToggle.addEventListener("click", togglePgnDrawer);
  }

  // ── PGN load / analyze ───────────────────────────────────────────────── //
  btnLoadPgn.addEventListener("click", async () => {
    let text = pgnInput.value.trim();
    if (!text && pgnFileInput.files && pgnFileInput.files[0]) {
      text = await pgnFileInput.files[0].text();
      pgnInput.value = text;
    }
    if (!text) { gameStatus.textContent = "Paste a PGN or pick a file first."; return; }
    const plies = parsePgn(text);
    if (!plies) { gameStatus.textContent = "Could not parse that PGN."; return; }
    resetGame();
    loadedPlies = plies;
    renderMoveList();
    gameStatus.textContent = `Loaded ${plies.length} plies — click Analyze to compute volatility.`;
    if (plies.length) jumpToPly(0);
    collapsePgnDrawer(text, plies.length);
  });

  pgnFileInput.addEventListener("change", async () => {
    if (!pgnFileInput.files || !pgnFileInput.files[0]) return;
    pgnInput.value = await pgnFileInput.files[0].text();
  });

  btnAnalyzePgn.addEventListener("click", () => {
    const text = pgnInput.value.trim();
    if (!text) { gameStatus.textContent = "Paste a PGN or pick a file first."; return; }
    if (!loadedPlies.length) {
      const plies = parsePgn(text);
      if (plies) { loadedPlies = plies; renderMoveList(); }
    }
    collapsePgnDrawer(text, loadedPlies.length);
    startReviewOrStream(text);
  });

  btnStopPgn.addEventListener("click", () => {
    if (pgnController) pgnController.abort();
    reviewPollCancelled = true;
  });

  let reviewPollCancelled = false;

  function reviewUserColor() {
    return document.body.dataset.reviewColor === "black" ||
      window.__volUserColor === "black"
      ? "black"
      : "white";
  }

  async function startAndPollReview(pgnText, depthTier, { signal } = {}) {
    const start = await fetch("/api/review", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pgn: pgnText,
        source: "pgn",
        user_color: reviewUserColor(),
        depth_tier: depthTier,
      }),
      signal,
    });
    if (start.status === 401 || start.status === 403) return null;
    if (!start.ok) {
      const err = await start.json().catch(() => ({}));
      throw new Error(err.detail || `Review start failed (${start.status})`);
    }
    const { review_id: reviewId, cached, status } = await start.json();
    if (cached && status === "complete") {
      const full = await fetch(`/api/review/${encodeURIComponent(reviewId)}`, {
        credentials: "same-origin",
        signal,
      });
      if (!full.ok) throw new Error("Could not load cached review");
      return full.json();
    }
    for (let i = 0; i < 900; i++) {
      if (reviewPollCancelled || (signal && signal.aborted)) {
        const err = new Error("aborted");
        err.name = "AbortError";
        throw err;
      }
      await new Promise((r) => setTimeout(r, 1000));
      const got = await fetch(`/api/review/${encodeURIComponent(reviewId)}`, {
        credentials: "same-origin",
        signal,
      });
      if (!got.ok) continue;
      const data = await got.json();
      const pct = Math.round((data.progress || 0) * 100);
      gameStatus.textContent =
        `Analyzing (${depthTier})… ${pct}%` +
        (data.status === "running" || data.status === "pending" ? "" : "");
      plyStatus.textContent = data.status || "";
      if (data.status === "complete") return data;
      if (data.status === "error") {
        throw new Error((data.detail && data.detail.error) || "Review failed");
      }
    }
    throw new Error("Review timed out");
  }

  function applyPersistedReview(review, { upgrading = false } = {}) {
    if (!window.ChessVolLibrary || !window.ChessVolLibrary.reviewToReport) return;
    const report = window.ChessVolLibrary.reviewToReport(review);
    const mode = review.depth_tier || report.mode || "full";
    if (review.pgn && pgnInput && !pgnInput.value.trim()) {
      pgnInput.value = review.pgn;
    }
    if ((!loadedPlies || !loadedPlies.length) && report.plies && report.plies.length) {
      loadedPlies = report.plies.map((ply) => {
        const uci = ply.move_uci || "";
        return {
          san: ply.san,
          fen_before: ply.fen_before,
          fen_after: ply.fen_after,
          from: uci.length >= 2 ? uci.slice(0, 2) : "",
          to: uci.length >= 4 ? uci.slice(2, 4) : "",
          move_uci: uci,
        };
      });
      renderMoveList();
    }
    ensureChart();
    chartWrap.classList.remove("hidden");
    onDone({
      mode: upgrading ? `${mode} · placeholder` : mode,
      plies_analysed: (report.plies || []).length,
      total_analyses: (report.plies || []).length,
      plies: report.plies,
      game_review: report.game_review,
    });
    if (upgrading) {
      gameStatus.textContent =
        `Shallow ready (${report.plies.length} plies) — upgrading to full…`;
    }
  }

  async function startReviewOrStream(pgnText) {
    if (pgnController) pgnController.abort();
    const ctrl = new AbortController();
    pgnController = ctrl;
    reviewPollCancelled = false;
    plyResults = [];
    destroyChart();
    btnAnalyzePgn.disabled = true;
    btnStopPgn.disabled = false;
    gameStatus.textContent = "Starting…";
    plyStatus.classList.remove("hidden");

    try {
      // Prefer durable job+poll: shallow placeholder, then full upgrade.
      let shallow;
      try {
        shallow = await startAndPollReview(pgnText, "shallow", { signal: ctrl.signal });
      } catch (err) {
        if (err.name === "AbortError") throw err;
        shallow = null;
      }
      if (shallow) {
        applyPersistedReview(shallow, { upgrading: true });
        const full = await startAndPollReview(pgnText, "full", { signal: ctrl.signal });
        if (full) {
          applyPersistedReview(full, { upgrading: false });
          gameStatus.textContent =
            `Done (full) · ${(full.moves || []).length} plies · persisted`;
        }
        return;
      }
      // Anonymous / API unavailable — live SSE (not persisted).
      await startPgnStream(pgnText, ctrl);
    } catch (err) {
      if (err.name === "AbortError") {
        gameStatus.textContent = "Analysis stopped (job continues server-side if started).";
      } else {
        gameStatus.textContent = `Error: ${err.message || err}`;
      }
    } finally {
      btnAnalyzePgn.disabled = false;
      btnStopPgn.disabled = true;
      if (pgnController === ctrl) pgnController = null;
    }
  }

  async function startPgnStream(pgnText, existingCtrl) {
    const ctrl = existingCtrl || new AbortController();
    if (!existingCtrl) {
      if (pgnController) pgnController.abort();
      pgnController = ctrl;
      plyResults = [];
      destroyChart();
      btnAnalyzePgn.disabled = true;
      btnStopPgn.disabled = false;
      gameStatus.textContent = "Starting…";
      plyStatus.classList.remove("hidden");
    }

    try {
      const resp = await fetch("/analyze/pgn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pgn: pgnText, deep: deepEnabled() }),
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
      await consumeSse(resp.body, ctrl);
    } catch (err) {
      if (err.name === "AbortError") {
        gameStatus.textContent = "Analysis stopped.";
      } else {
        gameStatus.textContent = `Error: ${err.message || err}`;
      }
      throw err;
    } finally {
      if (!existingCtrl) {
        btnAnalyzePgn.disabled = false;
        btnStopPgn.disabled = true;
        if (pgnController === ctrl) pgnController = null;
      }
    }
  }

  // ── SSE streaming ────────────────────────────────────────────────────── //
  async function consumeSse(stream, ctrl) {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    const splitRe = /\r\n\r\n|\n\n/;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let m;
      while ((m = splitRe.exec(buf))) {
        const chunk = buf.slice(0, m.index);
        buf = buf.slice(m.index + m[0].length);
        handleChunk(chunk);
      }
      if (ctrl.signal.aborted) {
        try { reader.cancel(); } catch (_) { }
        return;
      }
    }
  }

  function handleChunk(chunk) {
    let event = "message";
    const dataLines = [];
    for (const line of chunk.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return;
    let payload;
    try { payload = JSON.parse(dataLines.join("\n")); } catch (_) { return; }
    if (event === "start") onStart(payload);
    else if (event === "ply") onPly(payload);
    else if (event === "done") onDone(payload);
    else if (event === "error") onErr(payload);
  }

  function onStart(p) {
    gameStatus.textContent = `Analyzing (${p.mode})…`;
    ensureChart();
    chartWrap.classList.remove("hidden");
  }

  function onPly(p) {
    const plyData = p.ply;
    plyResults[plyData.ply - 1] = p;
    plyStatus.textContent = `${p.done} / ${p.total} plies`;
    updateMoveVol(plyData.ply - 1, plyData.volatility.score);
    updateMoveClassification(plyData.ply - 1, plyData.review || plyData.classification);
    appendChartPoint(plyData);
    recomputeGameStats();
    jumpToPly(plyData.ply - 1);
  }

  function onDone(p) {
    gameStatus.textContent =
      `Done (${p.mode}) · ${p.plies_analysed} plies · ${p.total_analyses} engine calls`;
    plyStatus.textContent = "";
    gameReviewSummary = p.game_review || null;
    if (Array.isArray(p.plies)) {
      plyResults = p.plies.map((ply, i) => ({
        done: i + 1,
        total: p.plies_analysed,
        ply,
      }));
      p.plies.forEach((ply, i) => {
        updateMoveVol(i, ply.volatility && ply.volatility.score);
        updateMoveClassification(i, ply.review || ply.classification);
      });
      // Rebuild heat band from full results
      const hb = document.getElementById("heatBand");
      if (hb) {
        hb.innerHTML = "";
        p.plies.forEach((ply, i) => {
          const d = document.createElement("div");
          const v = ply.volatility && ply.volatility.score;
          d.dataset.color = v != null ? scoreToColor(v) : "none";
          d.addEventListener("click", () => jumpToPly(i));
          hb.appendChild(d);
        });
        hb.classList.remove("hidden");
      }
      recomputeGameStats();
      if (currentPlyIdx >= 0) jumpToPly(currentPlyIdx);
      if (chart) requestAnimationFrame(() => chart.resize());
    }
  }

  function onErr(p) {
    gameStatus.textContent = `Server error: ${p.message}`;
  }

  // ── Library ──────────────────────────────────────────────────────────── //
  let libraryGames = [];

  function setLibraryProgress(message) {
    if (!libraryProgress) return;
    if (!message) {
      libraryProgress.classList.add("hidden");
      libraryProgress.textContent = "";
      return;
    }
    libraryProgress.textContent = message;
    libraryProgress.classList.remove("hidden");
  }

  function dateValue(timestamp) {
    return new Date(timestamp).toISOString().slice(0, 10);
  }

  function fmtNumber(value, digits = 1) {
    return typeof value === "number" ? value.toFixed(digits) : "—";
  }

  function libraryClassTotal(game, key) {
    if (!key) return 0;
    const counts = game.derivedStats && game.derivedStats.classificationCounts;
    if (!counts) return 0;
    return (counts.white && counts.white[key] || 0) + (counts.black && counts.black[key] || 0);
  }

  function passesLibraryFilters(game) {
    const importedDate = dateValue(game.importedAt);
    if (libraryDateFrom && libraryDateFrom.value && importedDate < libraryDateFrom.value) return false;
    if (libraryDateTo && libraryDateTo.value && importedDate > libraryDateTo.value) return false;

    const opponent = (libraryOpponent && libraryOpponent.value || "").trim().toLowerCase();
    if (opponent) {
      const meta = game.metadata || {};
      const haystack = `${meta.white || ""} ${meta.black || ""}`.toLowerCase();
      if (!haystack.includes(opponent)) return false;
    }

    const avgV = game.derivedStats ? game.derivedStats.avgV : null;
    const minV = libraryMinV && libraryMinV.value !== "" ? Number(libraryMinV.value) : null;
    const maxV = libraryMaxV && libraryMaxV.value !== "" ? Number(libraryMaxV.value) : null;
    if (typeof minV === "number" && !Number.isNaN(minV) && (avgV == null || avgV < minV)) return false;
    if (typeof maxV === "number" && !Number.isNaN(maxV) && (avgV == null || avgV > maxV)) return false;

    const key = libraryClassKey && libraryClassKey.value;
    if (key) {
      const min = libraryClassMin && libraryClassMin.value !== "" ? Number(libraryClassMin.value) : 1;
      if (libraryClassTotal(game, key) < min) return false;
    }
    return true;
  }

  async function refreshLibraryTable() {
    if (!libraryTableBody || !window.ChessVolLibrary) return;
    try {
      libraryGames = await window.ChessVolLibrary.getAllGames();
    } catch (err) {
      libraryTableBody.innerHTML =
        `<tr><td colspan="13" class="library-empty">Library unavailable: ${err.message || err}</td></tr>`;
      return;
    }

    const games = libraryGames.filter(passesLibraryFilters);
    libraryTableBody.innerHTML = "";
    if (!games.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 13;
      td.className = "library-empty";
      td.textContent = libraryGames.length ? "No games match these filters." : "No saved games yet.";
      tr.appendChild(td);
      libraryTableBody.appendChild(tr);
      return;
    }

    function sparklineSvg(values) {
      const pts = (values || []).filter((v) => typeof v === "number" && !Number.isNaN(v));
      if (pts.length < 2) return null;
      const w = 72;
      const h = 22;
      const min = Math.min(...pts);
      const max = Math.max(...pts);
      const span = max - min || 1;
      const path = pts
        .map((v, i) => {
          const x = (i / (pts.length - 1)) * w;
          const y = h - ((v - min) / span) * (h - 2) - 1;
          return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
        })
        .join(" ");
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
      svg.setAttribute("width", String(w));
      svg.setAttribute("height", String(h));
      svg.setAttribute("class", "library-sparkline");
      svg.setAttribute("aria-hidden", "true");
      const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
      line.setAttribute("d", path);
      line.setAttribute("fill", "none");
      line.setAttribute("stroke", "currentColor");
      line.setAttribute("stroke-width", "1.5");
      line.setAttribute("stroke-linecap", "round");
      line.setAttribute("stroke-linejoin", "round");
      svg.appendChild(line);
      return svg;
    }

    for (const game of games) {
      const meta = game.metadata || {};
      const stats = game.derivedStats || {};
      const tr = document.createElement("tr");
      const cells = [
        new Date(game.importedAt).toLocaleString(),
        meta.white || "Unknown",
        meta.whiteElo || "—",
        meta.black || "Unknown",
        meta.blackElo || "—",
        meta.result || "*",
        String(stats.plyCount || 0),
        fmtNumber(stats.avgV),
      ];
      cells.forEach((value) => {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      });
      const sparkTd = document.createElement("td");
      sparkTd.className = "library-spark-cell";
      const spark = sparklineSvg(stats.sparkline);
      if (spark) sparkTd.appendChild(spark);
      else sparkTd.textContent = "—";
      tr.appendChild(sparkTd);
      [
        typeof stats.whiteAcc === "number" ? `${stats.whiteAcc.toFixed(1)}%` : "—",
        typeof stats.blackAcc === "number" ? `${stats.blackAcc.toFixed(1)}%` : "—",
        String(stats.blunders || 0),
      ].forEach((value) => {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      });

      const actions = document.createElement("td");
      const wrap = document.createElement("div");
      wrap.className = "library-actions";
      const openBtn = document.createElement("button");
      openBtn.className = "btn";
      openBtn.type = "button";
      openBtn.textContent = "Open";
      openBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        openSavedGame(game);
      });
      const delBtn = document.createElement("button");
      delBtn.className = "btn btn-stop";
      delBtn.type = "button";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", async (event) => {
        event.stopPropagation();
        try {
          await window.ChessVolLibrary.deleteGame(game.id);
          await refreshLibraryTable();
        } catch (err) {
          setLibraryProgress(err.message || String(err));
        }
      });
      if (game.fromReviewsApi) {
        delBtn.disabled = true;
        delBtn.title = "Persisted reviews are kept for Insights";
      }
      wrap.appendChild(openBtn);
      wrap.appendChild(delBtn);
      actions.appendChild(wrap);
      tr.appendChild(actions);
      tr.addEventListener("click", () => openSavedGame(game));
      libraryTableBody.appendChild(tr);
    }
  }

  function reportPliesToResults(report) {
    const plies = report && Array.isArray(report.plies) ? report.plies : [];
    return plies.map((ply, i) => ({
      done: i + 1,
      total: plies.length,
      ply,
    }));
  }

  async function openSavedGame(summary) {
    // The list omits the heavy report; fetch the full record on open.
    let game = summary;
    if (!game.report) {
      try {
        game = await window.ChessVolLibrary.getGame(summary.id);
      } catch (err) {
        gameStatus.textContent = `Could not open game: ${err.message || err}`;
        return;
      }
    }
    resetGame();
    pgnInput.value = game.pgn || "";
    collapsePgnDrawer(game.pgn, (game.report.plies || []).length);
    loadedPlies = (game.report.plies || []).map((ply) => {
      const uci = ply.move_uci || "";
      const from = uci.length >= 2 ? uci.slice(0, 2) : "";
      const to = uci.length >= 4 ? uci.slice(2, 4) : "";
      return {
        san: ply.san,
        fen_before: ply.fen_before,
        fen_after: ply.fen_after,
        from,
        to,
        move_uci: uci,
      };
    });
    plyResults = reportPliesToResults(game.report);
    gameReviewSummary = game.report.game_review || null;
    renderMoveList();
    ensureChart();
    chartWrap.classList.remove("hidden");
    for (const entry of plyResults) {
      appendChartPoint(entry.ply);
      updateMoveVol(entry.ply.ply - 1, entry.ply.volatility && entry.ply.volatility.score);
      updateMoveClassification(entry.ply.ply - 1, entry.ply.review || entry.ply.classification);
    }
    recomputeGameStats();
    gameStatus.textContent = `Opened saved game: ${game.metadata.white} - ${game.metadata.black}`;
    setTab("game");
    if (loadedPlies.length) jumpToPly(0);
    if (chart) requestAnimationFrame(() => chart.resize());
  }

  function parseSseChunk(chunk) {
    let event = "message";
    const dataLines = [];
    for (const line of chunk.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return null;
    try {
      return { event, payload: JSON.parse(dataLines.join("\n")) };
    } catch (_) {
      return null;
    }
  }

  async function analyzePgnForLibrary(pgnText, onProgress) {
    const resp = await fetch("/analyze/pgn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pgn: pgnText, deep: deepEnabled() }),
    });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    const splitRe = /\r\n\r\n|\n\n/;
    let buf = "";
    let startPayload = null;
    let donePayload = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let m;
      while ((m = splitRe.exec(buf))) {
        const parsed = parseSseChunk(buf.slice(0, m.index));
        buf = buf.slice(m.index + m[0].length);
        if (!parsed) continue;
        if (parsed.event === "start") startPayload = parsed.payload;
        else if (parsed.event === "ply" && onProgress) onProgress(parsed.payload);
        else if (parsed.event === "done") donePayload = parsed.payload;
        else if (parsed.event === "error") throw new Error(parsed.payload.message || "analysis failed");
      }
    }
    if (!donePayload || !Array.isArray(donePayload.plies)) {
      throw new Error("analysis finished without a serialized report");
    }
    return {
      mode: donePayload.mode || (startPayload && startPayload.mode) || "shallow",
      params: startPayload && startPayload.params || {},
      plies: donePayload.plies,
      game_review: donePayload.game_review || null,
    };
  }

  async function importLibraryFiles(files) {
    if (!files || !files.length) return;
    try {
      const items = await window.ChessVolLibrary.pgnsFromFiles(Array.from(files));
      if (!items.length) {
        setLibraryProgress("No PGNs found in those files.");
        return;
      }
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const meta = window.ChessVolLibrary.gameRecordFromReport(
          item.pgn,
          { mode: "pending", params: {}, plies: [] },
          item.sourceName,
        ).metadata;
        setLibraryProgress(`Analyzing ${i + 1}/${items.length}: ${meta.white} - ${meta.black}`);
        const report = await analyzePgnForLibrary(item.pgn, (progress) => {
          setLibraryProgress(
            `Analyzing ${i + 1}/${items.length}: ${meta.white} - ${meta.black} (${progress.done}/${progress.total})`,
          );
        });
        const record = window.ChessVolLibrary.gameRecordFromReport(item.pgn, report, item.sourceName);
        await window.ChessVolLibrary.putGame(record);
      }
      setLibraryProgress(`Imported ${items.length} game${items.length === 1 ? "" : "s"}.`);
      await refreshLibraryTable();
    } catch (err) {
      setLibraryProgress(`Import failed: ${err.message || err}`);
    }
  }

  // ── Paste PGN import ────────────────────────────────────────────────── //
  async function importPastedPgn() {
    const textarea = document.getElementById("libraryPgnInput");
    if (!textarea) return;
    const text = textarea.value.trim();
    if (!text) {
      setLibraryProgress("Paste a PGN first.");
      return;
    }
    try {
      const games = window.ChessVolLibrary.splitPgnGames(text);
      if (!games.length) {
        setLibraryProgress("Could not find any games in the pasted text.");
        return;
      }
      for (let i = 0; i < games.length; i++) {
        const pgn = games[i];
        const meta = window.ChessVolLibrary.gameRecordFromReport(
          pgn,
          { mode: "pending", params: {}, plies: [] },
          `paste#${i + 1}`,
        ).metadata;
        setLibraryProgress(`Analyzing ${i + 1}/${games.length}: ${meta.white} - ${meta.black}`);
        const report = await analyzePgnForLibrary(pgn, (progress) => {
          setLibraryProgress(
            `Analyzing ${i + 1}/${games.length}: ${meta.white} - ${meta.black} (${progress.done}/${progress.total})`,
          );
        });
        const record = window.ChessVolLibrary.gameRecordFromReport(pgn, report, `paste#${i + 1}`);
        await window.ChessVolLibrary.putGame(record);
      }
      setLibraryProgress(`Imported ${games.length} game${games.length === 1 ? "" : "s"} from paste.`);
      textarea.value = "";
      await refreshLibraryTable();
    } catch (err) {
      setLibraryProgress(`Import failed: ${err.message || err}`);
    }
  }

  const btnLibraryPaste = document.getElementById("btnLibraryPaste");
  if (btnLibraryPaste) {
    btnLibraryPaste.addEventListener("click", importPastedPgn);
  }

  if (libraryFileInput) {
    libraryFileInput.addEventListener("change", async () => {
      await importLibraryFiles(libraryFileInput.files);
      libraryFileInput.value = "";
    });
  }
  if (libraryDrop) {
    ["dragenter", "dragover"].forEach((eventName) => {
      libraryDrop.addEventListener(eventName, (event) => {
        event.preventDefault();
        libraryDrop.classList.add("drag-over");
      });
    });
    ["dragleave", "drop"].forEach((eventName) => {
      libraryDrop.addEventListener(eventName, (event) => {
        event.preventDefault();
        libraryDrop.classList.remove("drag-over");
      });
    });
    libraryDrop.addEventListener("drop", (event) => {
      importLibraryFiles(event.dataTransfer && event.dataTransfer.files);
    });
  }
  [
    libraryDateFrom,
    libraryDateTo,
    libraryOpponent,
    libraryMinV,
    libraryMaxV,
    libraryClassKey,
    libraryClassMin,
  ].forEach((el) => {
    if (el) el.addEventListener("input", refreshLibraryTable);
  });

  // ── Chart ────────────────────────────────────────────────────────────── //

  // D2 — Vertical dashed line + subtle glow at the current ply
  const verticalLinePlugin = {
    id: "verticalLine",
    afterDatasetsDraw(chart) {
      const idx = chart.$currentIdx;
      if (idx == null || idx < 0) return;
      const meta = chart.getDatasetMeta(0);
      const point = meta.data[idx];
      if (!point) return;
      const { ctx, chartArea } = chart;
      ctx.save();
      ctx.strokeStyle = "rgba(57, 255, 20, 0.55)";
      ctx.lineWidth = 1.2;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(point.x, chartArea.top);
      ctx.lineTo(point.x, chartArea.bottom);
      ctx.stroke();
      // Subtle glow ring around the active point
      const phase = chart.$pulsePhase || 0;
      const radius = 6 + 3 * Math.sin(phase);
      const alpha = 0.25 + 0.15 * Math.sin(phase);
      ctx.beginPath();
      ctx.setLineDash([]);
      ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(57, 255, 20, ${alpha})`;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.restore();
    },
  };

  // Pulse animation loop — updates chart.$pulsePhase at ~1.5 Hz
  let pulseRaf = null;
  function startPulse() {
    if (pulseRaf) return;
    let last = performance.now();
    function tick(now) {
      pulseRaf = requestAnimationFrame(tick);
      if (!chart) return;
      const dt = (now - last) / 1000;
      last = now;
      chart.$pulsePhase = ((chart.$pulsePhase || 0) + dt * Math.PI * 3) % (Math.PI * 2);
      // Only redraw if a ply is selected
      if (chart.$currentIdx != null && chart.$currentIdx >= 0) {
        chart.draw();
      }
    }
    pulseRaf = requestAnimationFrame(tick);
  }

  function ensureChart() {
    if (chart) return chart;

    Chart.defaults.color = "#8a8a8a";
    Chart.defaults.borderColor = "#262626";
    Chart.defaults.font.family = "system-ui, sans-serif";
    Chart.defaults.font.size = 11;

    const ctx = chartCanvas.getContext("2d");

    chart = new Chart(ctx, {
      type: "line",
      plugins: [verticalLinePlugin],
      data: {
        labels: [],
        datasets: [
          {
            // Volatility overlay (dataset[0]), kept subtle so the advantage area
            // dominates. Drawn on top via a lower `order`.
            label: "Volatility",
            data: [],
            yAxisID: "yV",
            borderColor: "rgba(129, 182, 76, 0.85)",
            backgroundColor: "transparent",
            pointBackgroundColor: "rgba(129, 182, 76, 0.9)",
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 4,
            tension: 0.3,
            fill: false,
            spanGaps: true,
            order: 1,
          },
          {
            // Chess.com-style advantage area (dataset[1]): white fills the region
            // where White is ahead (eval > 0); the dark background shows through
            // below. Drawn behind via a higher `order`.
            label: "Eval (white, cp)",
            data: [],
            yAxisID: "yE",
            borderColor: "rgba(255, 255, 255, 0.9)",
            backgroundColor: "rgba(255, 255, 255, 0.92)",
            borderWidth: 1,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: "#81b64c",
            tension: 0.25,
            fill: "start",
            spanGaps: true,
            order: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 },
        interaction: { mode: "index", intersect: false },
        onClick: (_evt, elements) => {
          if (elements && elements.length) jumpToPly(elements[0].index);
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#141414",
            borderColor: "#81b64c",
            borderWidth: 1,
            titleColor: "#ececec",
            bodyColor: "#a0a0a0",
            padding: 10,
            callbacks: {
              label: (ctx) =>
                `${ctx.dataset.label}: ${ctx.parsed.y != null ? ctx.parsed.y.toFixed(1) : "—"}`,
            },
          },
        },
        scales: {
          yV: {
            type: "linear",
            position: "left",
            min: 0, max: 100,
            display: false,
            grid: { display: false },
          },
          yE: {
            type: "linear",
            position: "right",
            min: -900, max: 900,
            display: false,
            grid: { display: false },
          },
          x: {
            display: false,
            grid: { display: false },
          },
        },
      },
    });
    startPulse();
    return chart;
  }

  function appendChartPoint(plyJson) {
    ensureChart();
    const label = `${plyJson.ply}. ${plyJson.san}`;
    chart.data.labels.push(label);
    // Single volatility line (dataset 0) + Eval (dataset 1)
    const vScore = plyJson.volatility.score ?? null;
    chart.data.datasets[0].data.push(vScore);
    const turn = plyJson.fen_before.split(/\s+/)[1] || "w";
    const cpWhite = turn === "b" ? -plyJson.eval_cp : plyJson.eval_cp;
    chart.data.datasets[1].data.push(cpWhite);
    chart.update("none");

    // D3 — heat band: append a cell
    const hb = document.getElementById("heatBand");
    if (hb) {
      const d = document.createElement("div");
      d.dataset.color = vScore != null ? scoreToColor(vScore) : "none";
      const idx = plyJson.ply - 1;
      d.addEventListener("click", () => jumpToPly(idx));
      hb.appendChild(d);
      hb.classList.remove("hidden");
    }
  }

  // ── Bootstrap ────────────────────────────────────────────────────────── //
  try {
    setTab("editor");
    syncFenFromBoard();
    scheduleAutoAnalyze();
  } catch (err) {
    const msg = `Startup failed: ${err && err.message ? err.message : err}`;
    if (editorStatus) editorStatus.textContent = msg;
    console.error(msg, err);
  }
})();
