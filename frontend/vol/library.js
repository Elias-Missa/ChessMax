/* Chess Volatility Bar - saved-games library (server-backed, per account).

Storage moved from browser IndexedDB to the server (`/api/vol/games`) so saved
games belong to the logged-in account and follow the user across browsers. The
old IndexedDB store is migrated up once, on first authenticated load.

Manual smoke test plan:
- Import 1 PGN -> visible in library; reload -> persists (now from the server)
- Log in as a different account -> library empty; original account -> games back
- Delete a game -> removed from server and table
- Existing IndexedDB games migrate up once on first login
*/
/* eslint-disable no-undef */
(function () {
  "use strict";

  const API = "/api/vol/games";
  const MIGRATION_FLAG = "chessvol_library_migrated";

  // Legacy IndexedDB (read-only now — only used for the one-time migration).
  const DB_NAME = "chess-vol-library";
  const DB_VERSION = 1;
  const STORE = "games";

  const CLASS_KEYS = [
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

  // ── Server storage layer ────────────────────────────────────────────────

  async function apiFetch(path, options) {
    const resp = await fetch(path, options);
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || `Request failed (${resp.status})`);
    }
    return resp.status === 204 ? null : resp.json();
  }

  function fromSummary(row) {
    return {
      id: row.id,
      importedAt: row.imported_at,
      sourceName: row.source_name,
      metadata: row.metadata || {},
      derivedStats: row.derived_stats || {},
    };
  }

  function reviewToReport(review) {
    const moves = review.moves || [];

    // Stored `eval_cp` is side-to-move POV. Normalize to white so the eval bar
    // can show the eval *after* a move — which is the eval before the next one.
    const whiteEval = moves.map((m) => {
      const d = m.detail || {};
      if (typeof d.eval_cp !== "number") return null;
      const turn = (d.fen_before || "").split(/\s+/)[1] || "w";
      return turn === "b" ? -d.eval_cp : d.eval_cp;
    });

    const plies = moves.map((m, i) => {
      const d = m.detail || {};
      const lines = d.top_lines || [];
      const afterWhite = i + 1 < whiteEval.length ? whiteEval[i + 1] : null;
      return {
        ply: m.ply,
        san: m.san,
        fen_before: d.fen_before || "",
        fen_after: d.fen_after || "",
        eval_cp: d.eval_cp,
        move_uci: d.move_uci || "",
        volatility: {
          score: m.volatility,
          // The live analysis path carries the engine's best-line eval; without
          // rebuilding it here the eval bar reads `undefined` on every ply of a
          // stored review and sits frozen at even.
          best_eval_cp:
            lines[0] && typeof lines[0].eval_cp === "number"
              ? lines[0].eval_cp
              : (typeof d.eval_cp === "number" ? d.eval_cp : null),
          top_lines: lines,
        },
        review: m.classification
          ? {
              classification: m.classification,
              ...(typeof afterWhite === "number"
                ? { eval_after_cp_white: afterWhite }
                : {}),
            }
          : null,
        findability:
          m.findability != null
            ? {
                score: m.findability,
                personal: m.findability_personal,
                r_find: m.r_find,
              }
            : null,
        classification: null,
      };
    });
    const summary =
      (review.detail && review.detail.summary) ||
      (typeof review.detail === "object" && review.detail) ||
      null;
    return {
      mode: review.depth_tier || "full",
      plies,
      game_review: summary && summary.accuracy ? summary : summary,
    };
  }

  function fromReviewSummary(row) {
    const importedAt = row.created_at
      ? Date.parse(row.created_at) || Date.now()
      : Date.now();
    return {
      id: `review:${row.review_id}`,
      reviewId: row.review_id,
      gameId: row.game_id,
      depthTier: row.depth_tier,
      importedAt,
      sourceName: `review:${row.depth_tier || "full"}`,
      pgn: row.pgn || "",
      metadata: {
        white: row.white_name || "Unknown",
        black: row.black_name || "Unknown",
        result: row.result || "*",
        date: row.played_at || "",
        whiteElo: row.white_rating != null ? String(row.white_rating) : "",
        blackElo: row.black_rating != null ? String(row.black_rating) : "",
        timeControl: row.time_class || "",
      },
      derivedStats: {
        avgV: row.avg_v,
        whiteAcc: row.user_color === "white" ? row.accuracy : null,
        blackAcc: row.user_color === "black" ? row.accuracy : null,
        plyCount: row.ply_count || 0,
        blunders: row.blunders || 0,
        classificationCounts: null,
        fixableLoss: row.fixable_loss,
        totalLoss: row.total_loss,
        lossType: row.loss_type,
        sparkline: row.sparkline || [],
      },
      fromReviewsApi: true,
    };
  }

  async function getAllGames() {
    const data = await apiFetch(API);
    const volGames = (data.games || []).map(fromSummary);
    let reviewGames = [];
    try {
      const rev = await apiFetch("/api/reviews?limit=100");
      // Prefer full-tier rows; fall back to shallow when that's all we have.
      const byGame = new Map();
      for (const row of rev.reviews || []) {
        const key = row.game_id || row.review_id;
        const prev = byGame.get(key);
        if (!prev || (row.depth_tier === "full" && prev.depthTier !== "full")) {
          byGame.set(key, fromReviewSummary(row));
        }
      }
      reviewGames = Array.from(byGame.values());
    } catch (err) {
      reviewGames = [];
    }
    // Merge: reviews first (durable analysis), then vol_games not already covered.
    const seenPgn = new Set(
      reviewGames.map((g) => (g.pgn || "").trim()).filter(Boolean),
    );
    const extras = volGames.filter((g) => !seenPgn.has((g.pgn || "").trim()));
    return [...reviewGames, ...extras];
  }

  async function getGame(id) {
    if (String(id).startsWith("review:")) {
      const reviewId = String(id).slice("review:".length);
      const review = await apiFetch(`/api/review/${encodeURIComponent(reviewId)}`);
      const game = fromReviewSummary(review);
      game.pgn = review.pgn || game.pgn || "";
      // pgn lives on games table — fetch via list fields if missing
      if (!game.pgn && review.game_id) {
        /* GET /api/review doesn't include pgn today; patch below on server */
      }
      game.report = reviewToReport(review);
      return game;
    }
    const row = await apiFetch(`${API}/${encodeURIComponent(id)}`);
    const game = fromSummary(row);
    game.pgn = row.pgn || "";
    game.report = row.report || {};
    return game;
  }

  async function putGame(game) {
    await apiFetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: game.id,
        imported_at: game.importedAt,
        source_name: game.sourceName,
        pgn: game.pgn || "",
        metadata: game.metadata || {},
        report: game.report || {},
        derived_stats: game.derivedStats || {},
      }),
    });
  }

  async function deleteGame(id) {
    if (String(id).startsWith("review:")) {
      throw new Error("Persisted reviews can't be deleted from Library yet.");
    }
    await apiFetch(`${API}/${encodeURIComponent(id)}`, { method: "DELETE" });
  }

  // ── One-time migration from the old IndexedDB store ─────────────────────

  let dbPromise = null;
  function openLegacyDb() {
    if (!window.idb || !window.idb.openDB) return null;
    if (!dbPromise) {
      dbPromise = window.idb
        .openDB(DB_NAME, DB_VERSION, {
          upgrade(db) {
            if (!db.objectStoreNames.contains(STORE)) {
              const store = db.createObjectStore(STORE, { keyPath: "id" });
              store.createIndex("importedAt", "importedAt");
            }
          },
        })
        .catch(() => null);
    }
    return dbPromise;
  }

  async function migrateLegacyLibrary() {
    if (localStorage.getItem(MIGRATION_FLAG)) return;
    let legacy = [];
    try {
      const db = await openLegacyDb();
      if (db) legacy = await db.getAll(STORE);
    } catch (err) {
      legacy = [];
    }
    for (const game of legacy) {
      try {
        if (game && game.derivedStats) {
          game.derivedStats = {
            ...game.derivedStats,
            plyCount: ((game.report && game.report.plies) || []).length,
          };
        }
        await putGame(game);
      } catch (err) {
        /* skip individual failures (e.g. duplicates) */
      }
    }
    localStorage.setItem(MIGRATION_FLAG, "1");
  }

  // Migrate once the session is established (auth.js fires this).
  document.addEventListener("chessmax:authenticated", () => {
    migrateLegacyLibrary();
  });

  // ── Pure helpers (unchanged) ────────────────────────────────────────────

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return `game-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function parseHeaders(pgn) {
    const headers = {};
    const re = /^\[(\w+)\s+"((?:\\"|[^"])*)"\]\s*$/gm;
    let match;
    while ((match = re.exec(pgn))) {
      headers[match[1]] = match[2].replace(/\\"/g, "\"");
    }
    return headers;
  }

  function metadataFromPgn(pgn) {
    const h = parseHeaders(pgn);
    return {
      white: h.White || "Unknown",
      black: h.Black || "Unknown",
      result: h.Result || "*",
      date: h.Date || "",
      event: h.Event || "",
      site: h.Site || "",
      whiteElo: h.WhiteElo || "",
      blackElo: h.BlackElo || "",
      timeControl: h.TimeControl || "",
      termination: h.Termination || "",
    };
  }

  function splitPgnGames(text) {
    const clean = text.replace(/\r\n?/g, "\n").trim();
    if (!clean) return [];
    const starts = [];
    const re = /^\s*\[Event\s+"/gm;
    let match;
    while ((match = re.exec(clean))) starts.push(match.index);
    if (starts.length <= 1) return [clean];
    const games = [];
    for (let i = 0; i < starts.length; i++) {
      const end = i + 1 < starts.length ? starts[i + 1] : clean.length;
      const game = clean.slice(starts[i], end).trim();
      if (game) games.push(game);
    }
    return games;
  }

  async function pgnsFromFiles(files) {
    const items = [];
    for (const file of files) {
      const name = file.name || "import.pgn";
      if (/\.zip$/i.test(name)) {
        if (!window.JSZip) throw new Error("ZIP support failed to load.");
        const zip = await window.JSZip.loadAsync(file);
        const entries = Object.values(zip.files).filter(
          (entry) => !entry.dir && /\.pgn$/i.test(entry.name),
        );
        for (const entry of entries) {
          const text = await entry.async("text");
          splitPgnGames(text).forEach((pgn, idx) => {
            items.push({ pgn, sourceName: `${entry.name}#${idx + 1}` });
          });
        }
      } else {
        const text = await file.text();
        splitPgnGames(text).forEach((pgn, idx) => {
          items.push({ pgn, sourceName: `${name}#${idx + 1}` });
        });
      }
    }
    return items;
  }

  function whiteCpFromPly(ply) {
    const turn = (ply.fen_before || "").split(/\s+/)[1] || "w";
    return turn === "b" ? -ply.eval_cp : ply.eval_cp;
  }

  function winPercent(cpWhite) {
    return 50 + 50 * (2 / (1 + Math.exp(-0.00368208 * cpWhite)) - 1);
  }

  function accuracyFromDrop(winBefore, winAfter) {
    const drop = Math.max(0, winBefore - winAfter);
    const acc = 103.1668 * Math.exp(-0.04354 * drop) - 3.1669;
    return Math.max(0, Math.min(100, acc));
  }

  function emptyCounts() {
    return CLASS_KEYS.reduce((acc, key) => {
      acc[key] = 0;
      return acc;
    }, {});
  }

  function addCount(counts, key) {
    if (!key) return;
    counts[key] = (counts[key] || 0) + 1;
  }

  function avg(values) {
    return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  }

  function computeGameStats(plies) {
    const whiteAccs = [];
    const blackAccs = [];
    const volScores = [];
    const classificationCounts = {
      white: emptyCounts(),
      black: emptyCounts(),
    };

    for (let i = 0; i < plies.length; i++) {
      const cur = plies[i];
      if (!cur || !cur.ply) continue;
      const ply = cur.ply;
      const v = ply.volatility;
      if (v && typeof v.score === "number") volScores.push(v.score);

      const turn = (ply.fen_before || "").split(/\s+/)[1] || "w";
      const side = turn === "w" ? "white" : "black";
      const classification = ply.classification;
      const review = ply.review;
      if (review && review.classification) {
        addCount(classificationCounts[side], review.classification);
      } else if (classification) {
        addCount(classificationCounts[side], classification.primary);
        addCount(classificationCounts[side], classification.secondary);
      }

      const next = plies[i + 1];
      if (!next || !next.ply) continue;
      const cpWhiteBefore = whiteCpFromPly(ply);
      const cpWhiteAfter = whiteCpFromPly(next.ply);
      const winWBefore = winPercent(cpWhiteBefore);
      const winWAfter = winPercent(cpWhiteAfter);
      if (turn === "w") {
        whiteAccs.push(accuracyFromDrop(winWBefore, winWAfter));
      } else {
        blackAccs.push(accuracyFromDrop(100 - winWBefore, 100 - winWAfter));
      }
    }

    return {
      avgV: avg(volScores),
      whiteAcc: avg(whiteAccs),
      blackAcc: avg(blackAccs),
      plyCount: plies.length,
      blunders:
        (classificationCounts.white.blunder || 0) +
        (classificationCounts.black.blunder || 0),
      classificationCounts,
    };
  }

  function gameRecordFromReport(pgn, report, sourceName) {
    const plies = (report.plies || []).map((ply, i) => ({
      done: i + 1,
      total: report.plies.length,
      ply,
    }));
    const derivedStats = computeGameStats(plies);
    if (report.game_review && report.game_review.accuracy) {
      derivedStats.whiteAcc = report.game_review.accuracy.white;
      derivedStats.blackAcc = report.game_review.accuracy.black;
    }
    return {
      id: uuid(),
      importedAt: Date.now(),
      pgn,
      sourceName,
      metadata: metadataFromPgn(pgn),
      report,
      derivedStats,
    };
  }

  window.ChessVolLibrary = {
    CLASS_KEYS,
    computeGameStats,
    deleteGame,
    gameRecordFromReport,
    getAllGames,
    getGame,
    pgnsFromFiles,
    putGame,
    reviewToReport,
    splitPgnGames,
  };
})();
