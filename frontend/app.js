import { Chess } from "/vendor/chess.js/dist/esm/chess.js";
import { Chessground } from "/vendor/chessground/dist/chessground.js";

window.addEventListener("error", (event) => {
  const status = document.querySelector("#status");
  if (!status) return;
  status.textContent = `UI error: ${event.message}`;
});
window.addEventListener("unhandledrejection", (event) => {
  const status = document.querySelector("#status");
  if (!status) return;
  status.textContent = `UI promise error: ${String(event.reason)}`;
});

const OPPONENT_REPLY_DELAY_MS = 500;
const MAIA_RATINGS = [1100, 1300, 1500, 1700, 1900];
const sounds = {
  move: new Audio("/static/sounds/Move.mp3"),
  capture: new Audio("/static/sounds/Capture.mp3"),
  check: new Audio("/static/sounds/Check.mp3"),
  end: new Audio("/static/sounds/GenericNotify.mp3"),
};
for (const audio of Object.values(sounds)) {
  audio.preload = "auto";
  audio.volume = 0.7;
}

const state = {
  puzzle: null,
  chess: new Chess(),
  locked: true,
  rating: 1500,
  step: 0,
  userColor: "w",
  view: "train",
  canPlayOut: false,
  capabilities: null,
  playout: {
    id: null,
    active: false,
    engine: "-",
    initialFen: null,
    moveList: [],
    historyFens: [],
    reviewIndex: 0,
    reviewingSaved: false,
  },
  calc: { active: false, chess: null },
  hold: {
    evalhold: makeHoldState(),
    defense: makeHoldState(),
  },
  guess: {
    chess: null,
    positionId: null,
    revealed: false,
    busy: false,
    loaded: false,
    charts: { eval: null, sharp: null },
  },
  forced: {
    chess: null,
    positionId: null,
    startFen: null,
    sideToMove: "w",
    plyCount: 0,
    line: [],
    submitted: false,
    busy: false,
    loaded: false,
  },
  mistakes: {
    chess: null,
    positionId: null,
    fen: null,
    sideToMove: "w",
    bucket: null,
    plyCount: 0,
    solved: false,
    busy: false,
    loaded: false,
    generating: false,
  },
};

function makeHoldState() {
  return {
    sessionId: null,
    active: false,
    chess: null,
    userColor: "w",
    target: 5,
    threshold: 100,
    baseline: 0,
    survived: 0,
    currentEval: 0,
    busy: false,
  };
}

const boardElement = document.querySelector("#board");
const ratingEl = document.querySelector("#rating");
const sideToMove = document.querySelector("#side-to-move");
const statusText = document.querySelector("#status");
const feedback = document.querySelector("#feedback");
const nextButton = document.querySelector("#next-button");
const openingsFieldset = document.querySelector("#openings");
const tabs = Array.from(document.querySelectorAll(".tabs button"));
const views = {
  train: document.querySelector("#view-train"),
  evalhold: document.querySelector("#view-evalhold"),
  defense: document.querySelector("#view-defense"),
  forced: document.querySelector("#view-forced"),
  guess: document.querySelector("#view-guess"),
  mistakes: document.querySelector("#view-mistakes"),
  playout: document.querySelector("#view-playout"),
  stats: document.querySelector("#view-stats"),
};
const MODE_VIEWS = ["evalhold", "defense", "forced", "guess", "mistakes"];
const playoutStart = document.querySelector("#playout-start");
const maiaRatingSelect = document.querySelector("#maia-rating");
const playoutButton = document.querySelector("#playout-button");
const playoutEngine = document.querySelector("#playout-engine");
const playoutStatus = document.querySelector("#playout-status");
const playoutMoveCounter = document.querySelector("#playout-move-counter");
const playoutPrevButton = document.querySelector("#playout-prev-button");
const playoutNextButton = document.querySelector("#playout-next-button");
const playoutTakebackButton = document.querySelector("#playout-takeback-button");
const playoutEndButton = document.querySelector("#playout-end-button");
const playoutBackButton = document.querySelector("#playout-back-button");
const playoutMovesList = document.querySelector("#playout-moves");
const statsOverall = document.querySelector("#stats-overall");
const statsQuiet = document.querySelector("#stats-quiet");
const statsTactical = document.querySelector("#stats-tactical");
const statsPlayouts = document.querySelector("#stats-playouts");
const statsThemes = document.querySelector("#stats-themes");
const statsOpenings = document.querySelector("#stats-openings");
const statsPlayoutGames = document.querySelector("#stats-playout-games");
const statsChart = document.querySelector("#stats-chart");

const board = Chessground(boardElement, {
  coordinates: true,
  turnColor: "white",
  viewOnly: false,
  disableContextMenu: true,
  draggable: { enabled: true, showGhost: true },
  selectable: { enabled: true },
  drawable: {
    enabled: true,
    visible: true,
    defaultSnapToValidMove: true,
    eraseOnClick: true,
  },
  movable: {
    free: false,
    color: "white",
    dests: new Map(),
    events: { after: onMove },
  },
});

nextButton.addEventListener("click", loadNextPuzzle);
openingsFieldset.addEventListener("change", onOpeningsChange);
playoutButton.addEventListener("click", startPlayout);
if (playoutTakebackButton) playoutTakebackButton.addEventListener("click", takebackPlayout);
playoutEndButton.addEventListener("click", endPlayout);
playoutBackButton.addEventListener("click", () => switchTab("train"));
if (playoutPrevButton) playoutPrevButton.addEventListener("click", () => stepPlayoutReview(-1));
if (playoutNextButton) playoutNextButton.addEventListener("click", () => stepPlayoutReview(1));
tabs.forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
});
if (statsPlayoutGames) statsPlayoutGames.addEventListener("click", onStatsReviewClick);
document.addEventListener("keydown", onPlayoutKeyDown);

let rightClickTimes = [];
boardElement.addEventListener("contextmenu", () => {
  const now = Date.now();
  rightClickTimes = rightClickTimes.filter((t) => now - t < 700);
  rightClickTimes.push(now);
  if (rightClickTimes.length >= 3) {
    rightClickTimes = [];
    toggleCalcMode();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.calc.active) exitCalcMode();
});

init();

async function init() {
  try {
    const [user, openings, capabilities] = await Promise.all([
      request("/api/user"),
      request("/api/openings"),
      request("/api/playout/capabilities").catch(() => null),
    ]);
    state.rating = user.rating;
    state.capabilities = capabilities;
    renderRating();
    renderOpenings(openings.selected);
    maiaRatingSelect.value = String(nearestRating(state.rating));
    await loadNextPuzzle();
  } catch (error) {
    statusText.textContent = error.message;
  }
}

async function loadNextPuzzle() {
  if (state.calc.active) exitCalcMode();
  state.puzzle = await request("/api/puzzle/next");
  state.chess = new Chess(state.puzzle.fen);
  state.locked = false;
  state.step = 0;
  state.canPlayOut = false;
  state.userColor = state.puzzle.side_to_move;
  resetPlayoutState();

  feedback.className = "feedback hidden";
  feedback.textContent = "";
  playoutStart.classList.add("hidden");
  nextButton.disabled = true;
  statusText.textContent = "Find the best move, or play something solid.";
  sideToMove.textContent = state.puzzle.side_to_move === "w" ? "White" : "Black";

  const color = state.puzzle.side_to_move === "w" ? "white" : "black";
  board.set({
    fen: state.puzzle.fen,
    orientation: color,
    turnColor: color,
    lastMove: undefined,
    movable: {
      color,
      dests: legalDests(),
      events: { after: onMove },
    },
  });
  board.setShapes([]);
  switchTab("train");
}

function resetPlayoutState() {
  state.playout = {
    id: null,
    active: false,
    engine: "-",
    initialFen: null,
    moveList: [],
    historyFens: [],
    reviewIndex: 0,
    reviewingSaved: false,
  };
  renderPlayoutTimeline();
}

async function onMove(source, target) {
  if (state.calc.active) {
    submitCalcMove(source, target);
    return;
  }
  if (state.view === "playout") {
    await submitPlayoutMove(source, target);
    return;
  }
  if (state.view === "evalhold" || state.view === "defense") {
    await submitHoldMove(state.view, source, target);
    return;
  }
  if (state.view === "forced") {
    forcedApplyMove(source, target);
    return;
  }
  if (state.view === "guess") {
    return;
  }
  if (state.view === "mistakes") {
    await submitMistakeMove(source, target);
    return;
  }
  await submitTrainingMove(source, target);
}

function toggleCalcMode() {
  if (state.calc.active) exitCalcMode();
  else enterCalcMode();
}

function enterCalcMode() {
  state.calc.active = true;
  state.calc.chess = new Chess(state.chess.fen());
  document.querySelector(".board-panel").classList.add("calc-mode");
  board.setShapes([]);
  renderCalcBoard();
}

function exitCalcMode() {
  state.calc.active = false;
  state.calc.chess = null;
  document.querySelector(".board-panel").classList.remove("calc-mode");
  board.setShapes([]);
  board.set({ fen: state.chess.fen(), lastMove: undefined });
  syncBoardInteractivity();
}

function renderCalcBoard() {
  const c = state.calc.chess;
  const color = c.turn() === "w" ? "white" : "black";
  board.set({
    fen: c.fen(),
    turnColor: color,
    movable: { free: false, color: "both", dests: legalDests(c), events: { after: onMove } },
  });
}

function submitCalcMove(source, target) {
  const c = state.calc.chess;
  const move = tryApplyMove(c, { from: source, to: target, promotion: "q" });
  if (!move) {
    board.set({ fen: c.fen(), movable: { dests: legalDests(c) } });
    return;
  }
  playMoveSound(move, c);
  renderCalcBoard();
  if (c.isGameOver()) {
    statusText.textContent = c.isCheckmate() ? "Checkmate (calc)." : "Game over (calc).";
  }
}

async function submitTrainingMove(source, target) {
  if (state.locked || !state.puzzle) return;
  const move = tryApplyMove(state.chess, { from: source, to: target, promotion: "q" });
  if (!move) {
    board.set({ fen: state.chess.fen(), movable: { dests: legalDests() } });
    return;
  }
  playMoveSound(move);
  state.locked = true;
  board.set({ fen: state.chess.fen(), movable: { dests: new Map() } });
  statusText.textContent = "Checking...";

  let result;
  try {
    result = await request(`/api/puzzle/${state.puzzle.position_id}/attempt`, {
      method: "POST",
      body: JSON.stringify({ move: moveToUci(move), step: state.step }),
    });
  } catch (error) {
    statusText.textContent = error.message;
    resetToPuzzleStart();
    return;
  }

  state.rating = result.user_rating_after;
  renderRating();
  if (result.opponent_move_uci) {
    await delay(OPPONENT_REPLY_DELAY_MS);
    playOpponentMove(result.opponent_move_uci);
  }

  if (result.status === "continue") {
    state.step = result.next_step;
    state.locked = false;
    syncBoardInteractivity();
    statusText.textContent = "Find the next move.";
    return;
  }

  state.canPlayOut = Boolean(result.can_play_out);
  renderFeedback(result);
  playSound("end");
}

async function startPlayout() {
  if (state.calc.active) exitCalcMode();
  if (!state.canPlayOut || !state.puzzle) return;
  playoutButton.disabled = true;
  statusText.textContent = "Starting play-out...";
  try {
    const response = await request("/api/playout/start", {
      method: "POST",
      body: JSON.stringify({
        position_id: state.puzzle.position_id,
        maia_rating: Number(maiaRatingSelect.value),
        fen: state.chess.fen(),
        user_color: state.userColor,
      }),
    });
    state.playout.id = response.playout_id;
    state.playout.active = response.status === "active";
    state.playout.engine = response.engine;
    state.playout.reviewingSaved = false;
    setPlayoutTimeline(response.initial_fen, response.move_list || []);
    jumpPlayoutReview(state.playout.historyFens.length - 1);
    state.locked = !state.playout.active;
    playoutEngine.textContent = response.engine === "maia"
      ? `Maia ${response.maia_rating}`
      : "Stockfish fallback";
    if (response.maia_move) {
      playMoveSoundFromUci(response.maia_move, penultimatePlayoutFen());
    }
    switchTab("playout");
    updatePlayoutStatus(response.status, response.result);
    playoutEndButton.disabled = !state.playout.active;
    if (playoutTakebackButton) {
      playoutTakebackButton.disabled = !state.playout.active || state.playout.moveList.length === 0;
    }
    syncBoardInteractivity();
  } catch (error) {
    statusText.textContent = error.message;
  } finally {
    playoutButton.disabled = false;
  }
}

async function submitPlayoutMove(source, target) {
  if (!state.playout.active || !state.playout.id) return;
  if (!isAtLatestPlayoutPosition()) {
    playoutStatus.textContent = "Jump to latest position before making moves.";
    return;
  }
  const move = tryApplyMove(state.chess, { from: source, to: target, promotion: "q" });
  if (!move) {
    board.set({ fen: state.chess.fen(), movable: { dests: legalDests() } });
    return;
  }
  playMoveSound(move);
  state.locked = true;
  syncBoardInteractivity();
  try {
    const response = await request(`/api/playout/${state.playout.id}/move`, {
      method: "POST",
      body: JSON.stringify({ move: moveToUci(move) }),
    });
    setPlayoutTimeline(response.initial_fen, response.move_list || []);
    jumpPlayoutReview(state.playout.historyFens.length - 1);
    if (response.maia_move) {
      await delay(OPPONENT_REPLY_DELAY_MS);
      playMoveSoundFromUci(response.maia_move, penultimatePlayoutFen());
    }
    state.playout.active = response.status === "active";
    updatePlayoutStatus(response.status, response.result);
    playoutEndButton.disabled = !state.playout.active;
    if (playoutTakebackButton) {
      playoutTakebackButton.disabled = !state.playout.active || state.playout.moveList.length === 0;
    }
    state.locked = !state.playout.active;
    syncBoardInteractivity();
  } catch (error) {
    playoutStatus.textContent = error.message;
    state.locked = false;
    syncBoardInteractivity();
  }
}

async function takebackPlayout() {
  if (!state.playout.id || !state.playout.active) return;
  if (!isAtLatestPlayoutPosition()) {
    playoutStatus.textContent = "Jump to latest position before taking back.";
    return;
  }
  if (playoutTakebackButton) playoutTakebackButton.disabled = true;
  try {
    const response = await request(`/api/playout/${state.playout.id}/takeback`, {
      method: "POST",
    });
    setPlayoutTimeline(response.initial_fen, response.move_list || []);
    jumpPlayoutReview(state.playout.historyFens.length - 1);
    state.playout.active = response.status === "active";
    state.locked = false;
    playoutStatus.textContent = `Took back ${response.undone_plies} ply.`;
    if (playoutTakebackButton) playoutTakebackButton.disabled = state.playout.moveList.length === 0;
    syncBoardInteractivity();
  } catch (error) {
    playoutStatus.textContent = error.message;
    if (playoutTakebackButton) playoutTakebackButton.disabled = false;
  }
}

async function endPlayout() {
  if (!state.playout.id || !state.playout.active) return;
  playoutEndButton.disabled = true;
  try {
    const response = await request(`/api/playout/${state.playout.id}/end`, { method: "POST" });
    state.playout.active = false;
    state.locked = true;
    playoutStatus.textContent = `Game ended (${response.result}).`;
    statusText.textContent = "Play-out saved.";
    if (playoutTakebackButton) playoutTakebackButton.disabled = true;
    syncBoardInteractivity();
  } catch (error) {
    playoutStatus.textContent = error.message;
    playoutEndButton.disabled = false;
  }
}

const TAGLINES = {
  train: "Decide if there is a tactic — or just make a solid move.",
  evalhold: "Hold the eval for N moves straight. One big drop ends the run.",
  defense: "You start worse. The goal isn't to win — it's to survive.",
  forced: "See the whole line before you touch a piece.",
  guess: "Train your evaluation sense — no bars, no hints.",
  mistakes: "Your own missed wins and blunders, replayed as puzzles.",
  playout: "Convert the position against a human-like opponent.",
  stats: "Your progress at a glance.",
};

function switchTab(tabId) {
  if (state.calc.active) exitCalcMode();
  state.view = tabId;
  const tagline = document.querySelector("#topbar-tagline");
  if (tagline && TAGLINES[tabId]) tagline.textContent = TAGLINES[tabId];
  tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === tabId));
  Object.entries(views).forEach(([name, element]) => {
    element.classList.toggle("hidden", name !== tabId);
  });
  if (tabId === "stats") {
    void loadStats();
  }
  if (tabId === "playout") {
    renderPlayoutTimeline();
  }
  if (tabId === "evalhold" || tabId === "defense") {
    void loadHoldSummary(tabId);
  }
  if (tabId === "guess") {
    void enterGuessView();
  }
  if (tabId === "forced") {
    void enterForcedView();
  }
  if (tabId === "mistakes") {
    void enterMistakesView();
  }
  syncBoardInteractivity();
  if (window.__onPuzzlesTabChange) window.__onPuzzlesTabChange(tabId);
}

// ChessMax shell hook: the top-level tab bar drives this app's views.
window.__puzzlesSwitchTab = switchTab;

async function loadStats() {
  statsOverall.textContent = "Loading…";
  try {
    const [stats, recent] = await Promise.all([
      request("/api/stats"),
      request("/api/playout/recent"),
    ]);
    statsOverall.textContent = formatPct(stats.overall.accuracy_pct, stats.overall.attempts);
    statsQuiet.textContent = formatPct(stats.quiet.accuracy_pct, stats.quiet.attempts);
    statsTactical.textContent = formatPct(stats.tactical.accuracy_pct, stats.tactical.attempts);
    statsPlayouts.textContent = `${stats.playouts.wins}-${stats.playouts.losses}-${stats.playouts.draws}`;
    renderStatsList(statsThemes, stats.theme_accuracy, "theme");
    renderStatsList(statsOpenings, stats.opening_accuracy, "opening");
    if (statsPlayoutGames) renderRecentPlayouts(recent.playouts || []);
    drawRatingChart(stats.rating_history || []);
  } catch (error) {
    statsOverall.textContent = error.message;
  }
}

function renderStatsList(target, rows, key) {
  if (!rows || rows.length === 0) {
    target.innerHTML = "<li><span>No data yet</span><strong>-</strong></li>";
    return;
  }
  target.innerHTML = rows
    .map((row) => `<li><span>${escapeHtml(row[key])}</span><strong>${row.accuracy_pct}% (${row.attempts})</strong></li>`)
    .join("");
}

function renderRecentPlayouts(playouts) {
  if (!statsPlayoutGames) return;
  if (!playouts.length) {
    statsPlayoutGames.innerHTML = "<li><span>No playouts yet</span><strong>-</strong></li>";
    return;
  }
  statsPlayoutGames.innerHTML = playouts
    .map((game) => (
      `<li>
        <span>${escapeHtml(game.timestamp)} ${escapeHtml(game.engine)} ${game.maia_rating}</span>
        <strong>${escapeHtml(game.result)}</strong>
        <button type="button" class="review-playout" data-initial-fen="${escapeHtml(game.initial_fen)}" data-move-list="${escapeHtml(JSON.stringify(game.move_list || []))}">Review</button>
      </li>`
    ))
    .join("");
}

function onStatsReviewClick(event) {
  const button = event.target.closest("button.review-playout");
  if (!button) return;
  const initialFen = button.dataset.initialFen || "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  let moveList = [];
  try {
    moveList = JSON.parse(button.dataset.moveList || "[]");
  } catch {
    moveList = [];
  }
  state.playout.active = false;
  state.playout.reviewingSaved = true;
  state.playout.id = null;
  state.playout.engine = "review";
  setPlayoutTimeline(initialFen, moveList);
  jumpPlayoutReview(state.playout.historyFens.length - 1);
  playoutEngine.textContent = "Saved game review";
  playoutStatus.textContent = "Review mode (use arrow keys or Prev/Next).";
  playoutEndButton.disabled = true;
  if (playoutTakebackButton) playoutTakebackButton.disabled = true;
  switchTab("playout");
}

function drawRatingChart(history) {
  const ctx = statsChart.getContext("2d");
  if (!ctx) return;
  ctx.clearRect(0, 0, statsChart.width, statsChart.height);
  if (!history || history.length < 2) {
    ctx.fillStyle = "#9aa6b2";
    ctx.font = "12px sans-serif";
    ctx.fillText("Not enough data", 10, 20);
    return;
  }
  const padding = 12;
  const ratings = history.map((entry) => Number(entry.rating));
  const min = Math.min(...ratings);
  const max = Math.max(...ratings);
  const range = Math.max(1, max - min);
  ctx.strokeStyle = "#8ab4f8";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ratings.forEach((rating, index) => {
    const x = padding + (index / (ratings.length - 1)) * (statsChart.width - padding * 2);
    const y = statsChart.height - padding - ((rating - min) / range) * (statsChart.height - padding * 2);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function setPlayoutTimeline(initialFen, moveList) {
  state.playout.initialFen = initialFen;
  state.playout.moveList = Array.isArray(moveList) ? [...moveList] : [];
  state.playout.historyFens = buildHistoryFens(initialFen, state.playout.moveList);
}

function buildHistoryFens(initialFen, moves) {
  const boardState = new Chess(initialFen);
  const fens = [boardState.fen()];
  for (const uci of moves) {
    const move = tryApplyMove(boardState, {
      from: uci.slice(0, 2),
      to: uci.slice(2, 4),
      promotion: uci.length > 4 ? uci.slice(4) : undefined,
    });
    if (!move) break;
    fens.push(boardState.fen());
  }
  return fens;
}

function jumpPlayoutReview(index) {
  if (!state.playout.historyFens.length) return;
  const clamped = Math.max(0, Math.min(index, state.playout.historyFens.length - 1));
  state.playout.reviewIndex = clamped;
  state.chess = new Chess(state.playout.historyFens[clamped]);
  renderPlayoutTimeline();
  syncBoardInteractivity();
}

function stepPlayoutReview(delta) {
  if (state.view !== "playout" || !state.playout.historyFens.length) return;
  jumpPlayoutReview(state.playout.reviewIndex + delta);
}

function renderPlayoutTimeline() {
  if (!playoutMoveCounter || !playoutMovesList || !playoutPrevButton || !playoutNextButton) {
    return;
  }
  const total = state.playout.historyFens.length > 0 ? state.playout.historyFens.length - 1 : 0;
  playoutMoveCounter.textContent = `${state.playout.reviewIndex} / ${total}`;
  playoutPrevButton.disabled = state.playout.reviewIndex <= 0;
  playoutNextButton.disabled = state.playout.reviewIndex >= total;

  const boardForMoves = state.playout.initialFen ? new Chess(state.playout.initialFen) : new Chess();
  const items = [];
  for (let ply = 0; ply < state.playout.moveList.length; ply += 2) {
    const whiteMoveUci = state.playout.moveList[ply];
    const blackMoveUci = state.playout.moveList[ply + 1];
    const whiteSan = sanFromUci(boardForMoves, whiteMoveUci);
    const blackSan = blackMoveUci ? sanFromUci(boardForMoves, blackMoveUci) : "";
    const moveNumber = Math.floor(ply / 2) + 1;
    const whitePlyIndex = ply + 1;
    const blackPlyIndex = ply + 2;
    const classes = [];
    if (state.playout.reviewIndex === whitePlyIndex || state.playout.reviewIndex === blackPlyIndex) {
      classes.push("current");
    }
    items.push(`<li class="${classes.join(" ")}">${moveNumber}. ${escapeHtml(whiteSan || whiteMoveUci)} ${escapeHtml(blackSan || "")}</li>`);
  }
  playoutMovesList.innerHTML = items.join("");
}

function sanFromUci(boardState, uci) {
  if (!uci) return "";
  const move = tryApplyMove(boardState, {
    from: uci.slice(0, 2),
    to: uci.slice(2, 4),
    promotion: uci.length > 4 ? uci.slice(4) : undefined,
  });
  return move?.san || uci;
}

function isAtLatestPlayoutPosition() {
  const latest = Math.max(0, state.playout.historyFens.length - 1);
  return state.playout.reviewIndex === latest;
}

function onPlayoutKeyDown(event) {
  if (state.view !== "playout") return;
  const targetTag = event.target?.tagName?.toLowerCase();
  if (targetTag === "input" || targetTag === "textarea" || targetTag === "select") return;
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    stepPlayoutReview(-1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    stepPlayoutReview(1);
  }
}

function syncBoardInteractivity() {
  if (MODE_VIEWS.includes(state.view)) {
    renderModeBoard(state.view);
    return;
  }
  const color = state.chess.turn() === "w" ? "white" : "black";
  const interactiveTrain = state.view === "train" && !state.locked;
  const interactivePlayout = state.view === "playout"
    && state.playout.active
    && !state.locked
    && isAtLatestPlayoutPosition()
    && state.chess.turn() === state.userColor;
  const interactive = interactiveTrain || interactivePlayout;

  board.set({
    fen: state.chess.fen(),
    turnColor: color,
    orientation: state.userColor === "b" ? "black" : "white",
    movable: {
      color,
      dests: interactive ? legalDests() : new Map(),
      events: { after: onMove },
    },
  });
}

function renderModeBoard(view) {
  let chess = null;
  let interactive = false;
  let movableColor = "white";
  let orientation = "white";

  if (view === "evalhold" || view === "defense") {
    const hold = state.hold[view];
    chess = hold.chess;
    orientation = hold.userColor === "b" ? "black" : "white";
    movableColor = hold.userColor === "w" ? "white" : "black";
    interactive = Boolean(
      hold.active && !hold.busy && chess && chess.turn() === hold.userColor,
    );
  } else if (view === "forced") {
    const forced = state.forced;
    chess = forced.chess;
    orientation = forced.sideToMove === "b" ? "black" : "white";
    movableColor = "both";
    interactive = Boolean(
      chess && !forced.submitted && !forced.busy && forced.line.length < forced.plyCount,
    );
  } else if (view === "guess") {
    chess = state.guess.chess;
    orientation = chess && chess.turn() === "b" ? "black" : "white";
  } else if (view === "mistakes") {
    const m = state.mistakes;
    chess = m.chess;
    orientation = m.sideToMove === "b" ? "black" : "white";
    movableColor = m.sideToMove === "w" ? "white" : "black";
    interactive = Boolean(chess && m.loaded && !m.solved && !m.busy);
  }

  if (!chess) {
    board.set({ movable: { color: undefined, dests: new Map() } });
    return;
  }

  const turnColor = chess.turn() === "w" ? "white" : "black";
  board.set({
    fen: chess.fen(),
    turnColor,
    orientation,
    lastMove: undefined,
    movable: {
      free: false,
      color: interactive ? movableColor : undefined,
      dests: interactive ? legalDests(chess) : new Map(),
      events: { after: onMove },
    },
  });
}

function resetToPuzzleStart() {
  if (!state.puzzle) return;
  state.locked = false;
  state.chess = new Chess(state.puzzle.fen);
  board.set({
    fen: state.puzzle.fen,
    turnColor: state.puzzle.side_to_move === "w" ? "white" : "black",
    movable: {
      color: state.puzzle.side_to_move === "w" ? "white" : "black",
      dests: legalDests(),
      events: { after: onMove },
    },
  });
}

function playOpponentMove(uci) {
  const from = uci.slice(0, 2);
  const to = uci.slice(2, 4);
  const promotion = uci.length > 4 ? uci.slice(4) : undefined;
  const opponentMove = tryApplyMove(state.chess, { from, to, promotion });
  if (!opponentMove) return;
  playMoveSound(opponentMove);
  board.move(from, to);
  board.set({ fen: state.chess.fen() });
}

function penultimatePlayoutFen() {
  const fens = state.playout.historyFens;
  return fens.length >= 2 ? fens[fens.length - 2] : state.playout.initialFen;
}

function playMoveSoundFromUci(uci, fromFen) {
  const boardCopy = new Chess(fromFen || state.chess.fen());
  const move = tryApplyMove(boardCopy, {
    from: uci.slice(0, 2),
    to: uci.slice(2, 4),
    promotion: uci.length > 4 ? uci.slice(4) : undefined,
  });
  if (!move) return;
  if (boardCopy.inCheck()) playSound("check");
  else if (move.flags.includes("c") || move.flags.includes("e")) playSound("capture");
  else playSound("move");
}

function tryApplyMove(chessState, moveLike) {
  try {
    return chessState.move(moveLike);
  } catch {
    return null;
  }
}

function playMoveSound(move, chessInstance = state.chess) {
  if (chessInstance.inCheck()) {
    playSound("check");
    return;
  }
  if (move.flags.includes("c") || move.flags.includes("e")) {
    playSound("capture");
    return;
  }
  playSound("move");
}

function playSound(name) {
  const audio = sounds[name];
  if (!audio) return;
  audio.currentTime = 0;
  audio.play().catch(() => {});
}

function updatePlayoutStatus(status, result) {
  if (status === "active") {
    playoutStatus.textContent = "Your move.";
    statusText.textContent = "Play-out in progress.";
    return;
  }
  const resultText = result ? ` (${result})` : "";
  playoutStatus.textContent = `Game over: ${status}${resultText}.`;
  statusText.textContent = "Play-out finished.";
  playSound("end");
}

async function onOpeningsChange() {
  const selected = Array.from(
    openingsFieldset.querySelectorAll("input[type=checkbox]:checked"),
    (el) => el.value,
  );
  try {
    await request("/api/openings", {
      method: "PUT",
      body: JSON.stringify({ openings: selected }),
    });
  } catch (error) {
    statusText.textContent = error.message;
  }
}

function renderOpenings(selected) {
  const set = new Set(selected);
  for (const input of openingsFieldset.querySelectorAll("input[type=checkbox]")) {
    input.checked = set.has(input.value);
  }
}

function renderRating() {
  ratingEl.textContent = state.rating;
}

function renderFeedback(result) {
  const isTactical = result.position_classification === "tactical";
  const passed = result.solved;
  statusText.textContent = passed ? "Correct." : "Missed.";
  feedback.className = `feedback ${passed ? "pass" : "fail"}`;
  const summary = isTactical
    ? `<p><strong>${passed ? "Correct" : "Wrong"}</strong> — tactical puzzle</p>`
    : `<p><strong>${result.grade}</strong> (${Math.round(result.eval_loss)} cp loss) — quiet position</p>`;
  const ratingLine = isTactical && typeof result.position_rating === "number"
    ? `<p>Puzzle rating: <strong>${result.position_rating}</strong> <span class="hint">(Lichess Elo)</span></p>`
    : "";
  const openingLine = result.opening_tag
    ? `<p>Opening: <strong>${escapeHtml(openingLabel(result.opening_tag))}</strong></p>`
    : "";
  const topLines = !isTactical && Array.isArray(result.top_lines) && result.top_lines.length
    ? `<p><strong>Top lines:</strong></p><ul class="top-lines">${result.top_lines
      .map(
        (line) =>
          `<li><code>${line.move_san}</code> <span class="eval">${formatEval(line.eval_cp)}</span> <span class="pv">${escapeHtml(line.pv_san || "")}</span></li>`,
      )
      .join("")}</ul>`
    : "";
  const bestMoveLine = result.best_move ? `<p>Best move: <code>${result.best_move}</code></p>` : "";
  const solutionLine = result.solution_line ? `<p>Line: <code>${result.solution_line}</code></p>` : "";
  const refutationLine = result.refutation
    ? `<p>Engine response: <code>${result.refutation}</code> <span class="hint">(red arrow on board)</span></p>`
    : "";
  feedback.innerHTML = `${summary}${ratingLine}${openingLine}${bestMoveLine}${solutionLine}${refutationLine}${topLines}`;
  if (result.refutation_uci) drawRefutationArrow(result.refutation_uci);
  playoutStart.classList.toggle("hidden", !state.canPlayOut);
  nextButton.disabled = false;
}

function drawRefutationArrow(uci) {
  board.setShapes([{ orig: uci.slice(0, 2), dest: uci.slice(2, 4), brush: "red" }]);
}

function legalDests(chessInstance = state.chess) {
  const dests = new Map();
  for (const move of chessInstance.moves({ verbose: true })) {
    const targets = dests.get(move.from) || [];
    targets.push(move.to);
    dests.set(move.from, targets);
  }
  return dests;
}

const OPENING_LABELS = { london: "London System", "caro-kann": "Caro-Kann" };

function openingLabel(tag) {
  return OPENING_LABELS[tag] || tag;
}

function nearestRating(rating) {
  return MAIA_RATINGS.reduce(
    (best, candidate) => (Math.abs(candidate - rating) < Math.abs(best - rating) ? candidate : best),
    MAIA_RATINGS[0],
  );
}

function moveToUci(move) {
  return `${move.from}${move.to}${move.promotion || ""}`;
}

function formatEval(cp) {
  if (typeof cp !== "number") return "";
  const pawns = cp / 100;
  const sign = pawns >= 0 ? "+" : "";
  return `${sign}${pawns.toFixed(2)}`;
}

function formatPct(value, attempts) {
  return `${value}% (${attempts})`;
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  })[c]);
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Request failed");
  return payload;
}

/* ════════════════════════ Training modes ════════════════════════════════
   Eval Hold + Defense Gym share the same "hold session" flow; Forced Lines
   buffers a full line before submitting; Guess hides the bars and scores
   your reads. All four talk to /api/<mode>/… endpoints. */

const HOLD_LABELS = {
  evalhold: {
    statusActive: "Your move — keep the drop under the ceiling.",
    pass: "Held it. Streak +1.",
    fail: "Run over.",
  },
  defense: {
    statusActive: "Hold the line — don't let it collapse.",
    pass: "Survived. The position held.",
    fail: "It collapsed.",
  },
};

function holdEls(mode) {
  const q = (suffix) => document.querySelector(`#${mode}-${suffix}`);
  return {
    setup: q("setup"),
    live: q("live"),
    start: q("start"),
    giveup: q("giveup"),
    moves: q("moves"),
    threshold: q("threshold"),
    opponent: q("opponent"),
    progress: q("progress"),
    counter: q("counter"),
    baseline: q("baseline"),
    lastDelta: q("last-delta"),
    current: q("current"),
    status: q("status"),
    feedback: q("feedback"),
    total: q("total"),
    passed: q("passed"),
    streak: q("streak"),
    bestStreak: q("best-streak"),
    streakBadge: q("streak-badge"),
  };
}

const holdUi = {
  evalhold: holdEls("evalhold"),
  defense: holdEls("defense"),
};

for (const mode of ["evalhold", "defense"]) {
  holdUi[mode].start.addEventListener("click", () => startHold(mode));
  holdUi[mode].giveup.addEventListener("click", () => giveUpHold(mode));
}

async function startHold(mode) {
  const ui = holdUi[mode];
  const hold = state.hold[mode];
  ui.start.disabled = true;
  ui.status.textContent = mode === "defense"
    ? "Scanning the bank for a worse position…"
    : "Picking a position…";
  ui.feedback.className = "feedback hidden";
  ui.feedback.textContent = "";
  ui.live.classList.remove("hidden");
  try {
    const response = await request(`/api/${mode}/start`, {
      method: "POST",
      body: JSON.stringify({
        target_moves: Number(ui.moves.value),
        threshold_cp: Number(ui.threshold.value),
        maia_rating: Number(ui.opponent.value),
      }),
    });
    hold.sessionId = response.session_id;
    hold.active = true;
    hold.busy = false;
    hold.chess = new Chess(response.fen);
    hold.userColor = response.user_color;
    hold.target = response.target_moves;
    hold.threshold = response.threshold_cp;
    hold.baseline = response.baseline_eval_cp;
    hold.currentEval = response.baseline_eval_cp;
    hold.survived = 0;
    renderHoldLive(mode);
    ui.setup.classList.add("hidden");
    ui.status.textContent = HOLD_LABELS[mode].statusActive;
    ui.lastDelta && (ui.lastDelta.textContent = "—");
    syncBoardInteractivity();
  } catch (error) {
    ui.status.textContent = error.message;
    ui.live.classList.add("hidden");
  } finally {
    ui.start.disabled = false;
  }
}

async function submitHoldMove(mode, source, target) {
  const hold = state.hold[mode];
  const ui = holdUi[mode];
  if (!hold.active || hold.busy || !hold.chess) return;
  const move = tryApplyMove(hold.chess, { from: source, to: target, promotion: "q" });
  if (!move) {
    renderModeBoard(mode);
    return;
  }
  playMoveSound(move, hold.chess);
  hold.busy = true;
  renderModeBoard(mode);
  ui.status.textContent = "Engine is judging the move…";
  try {
    const response = await request(`/api/${mode}/${hold.sessionId}/move`, {
      method: "POST",
      body: JSON.stringify({ move: moveToUci(move) }),
    });
    hold.survived = response.moves_survived;
    hold.currentEval = response.played_eval_cp;
    renderHoldLive(mode);
    renderHoldDelta(mode, response);

    if (response.reply_uci) {
      await delay(OPPONENT_REPLY_DELAY_MS);
      playMoveSoundFromUci(response.reply_uci, hold.chess.fen());
    }
    hold.chess = new Chess(response.fen);

    if (response.status === "active") {
      hold.busy = false;
      ui.status.textContent = HOLD_LABELS[mode].statusActive;
      renderModeBoard(mode);
      return;
    }

    hold.active = false;
    hold.busy = false;
    renderModeBoard(mode);
    renderHoldResult(mode, response);
    await loadHoldSummary(mode);
  } catch (error) {
    hold.busy = false;
    ui.status.textContent = error.message;
    hold.chess.undo();
    renderModeBoard(mode);
  }
}

async function giveUpHold(mode) {
  const hold = state.hold[mode];
  const ui = holdUi[mode];
  if (!hold.active || !hold.sessionId) return;
  try {
    await request(`/api/${mode}/${hold.sessionId}/end`, { method: "POST" });
  } catch {
    /* session may already be gone */
  }
  hold.active = false;
  hold.sessionId = null;
  ui.live.classList.add("hidden");
  ui.setup.classList.remove("hidden");
  ui.status.textContent = "";
  ui.feedback.className = "feedback fail";
  ui.feedback.innerHTML = "<p><strong>Abandoned.</strong> Streak reset.</p>";
  renderModeBoard(mode);
  await loadHoldSummary(mode);
}

function renderHoldLive(mode) {
  const hold = state.hold[mode];
  const ui = holdUi[mode];
  ui.counter.textContent = `${hold.survived} / ${hold.target}`;
  ui.progress.style.width = `${Math.min(100, (hold.survived / hold.target) * 100)}%`;
  ui.baseline.textContent = formatEval(hold.baseline);
  if (ui.current) ui.current.textContent = formatEval(hold.currentEval);
}

function renderHoldDelta(mode, response) {
  const ui = holdUi[mode];
  if (!ui.lastDelta) return;
  const drop = Math.round(response.drop_cp);
  if (drop <= 0) {
    ui.lastDelta.innerHTML = `<span class="chip good">best (+${Math.abs(drop)} cp)</span>`;
  } else if (drop <= response.threshold_cp) {
    ui.lastDelta.innerHTML = `<span class="chip ok">−${drop} cp</span>`;
  } else {
    ui.lastDelta.innerHTML = `<span class="chip bad">−${drop} cp</span>`;
  }
}

function renderHoldResult(mode, response) {
  const ui = holdUi[mode];
  const passed = response.status === "passed";
  ui.status.textContent = passed ? HOLD_LABELS[mode].pass : HOLD_LABELS[mode].fail;
  ui.feedback.className = `feedback ${passed ? "pass" : "fail"}`;
  const bestLine = !passed && response.best_move_san
    ? `<p>Engine preferred <code>${escapeHtml(response.best_move_san)}</code> (${formatEval(response.best_eval_cp)}).</p>`
    : "";
  const survived = `<p>Survived <strong>${response.moves_survived}</strong> of ${response.target_moves} moves.</p>`;
  ui.feedback.innerHTML = `
    <p><strong>${passed ? "Passed" : "Failed"}</strong> — ${escapeHtml(response.detail || "")}</p>
    ${survived}${bestLine}
    <p>Streak: <strong>${response.streak}</strong></p>`;
  ui.live.classList.add("hidden");
  ui.setup.classList.remove("hidden");
  playSound("end");
}

async function loadHoldSummary(mode) {
  const ui = holdUi[mode];
  try {
    const summary = await request(`/api/${mode}/summary`);
    ui.total.textContent = summary.total;
    ui.passed.textContent = summary.passed;
    ui.streak.textContent = summary.streak;
    ui.bestStreak.textContent = summary.best_streak;
    ui.streakBadge.textContent = `streak ${summary.streak}`;
    ui.streakBadge.classList.toggle("hot", summary.streak >= 3);
  } catch {
    /* summary is cosmetic */
  }
}

/* ── Forced Lines ──────────────────────────────────────────────────────── */

const forcedUi = {
  plyCount: document.querySelector("#forced-ply-count"),
  entered: document.querySelector("#forced-entered"),
  line: document.querySelector("#forced-line"),
  undo: document.querySelector("#forced-undo"),
  reset: document.querySelector("#forced-reset"),
  submit: document.querySelector("#forced-submit"),
  next: document.querySelector("#forced-next"),
  feedback: document.querySelector("#forced-feedback"),
  total: document.querySelector("#forced-total"),
  passed: document.querySelector("#forced-passed"),
  streak: document.querySelector("#forced-streak"),
  streakBadge: document.querySelector("#forced-streak-badge"),
};

forcedUi.undo.addEventListener("click", forcedUndo);
forcedUi.reset.addEventListener("click", forcedReset);
forcedUi.submit.addEventListener("click", submitForcedLine);
forcedUi.next.addEventListener("click", () => loadForcedPuzzle(true));

async function enterForcedView() {
  if (!state.forced.loaded) await loadForcedPuzzle(false);
  await loadForcedSummary();
  syncBoardInteractivity();
}

async function loadForcedPuzzle(reset) {
  const forced = state.forced;
  if (forced.busy) return;
  forced.busy = true;
  forcedUi.next.disabled = true;
  try {
    const puzzle = await request("/api/forced/next");
    forced.positionId = puzzle.position_id;
    forced.startFen = puzzle.fen;
    forced.sideToMove = puzzle.side_to_move;
    forced.plyCount = puzzle.ply_count;
    forced.chess = new Chess(puzzle.fen);
    forced.line = [];
    forced.submitted = false;
    forced.loaded = true;
    forcedUi.plyCount.textContent = `${puzzle.ply_count} plies`;
    forcedUi.feedback.className = "feedback hidden";
    forcedUi.feedback.textContent = "";
    renderForcedLine();
  } catch (error) {
    forcedUi.feedback.className = "feedback fail";
    forcedUi.feedback.textContent = error.message;
  } finally {
    forced.busy = false;
    forcedUi.next.disabled = false;
    syncBoardInteractivity();
  }
}

function forcedApplyMove(source, target) {
  const forced = state.forced;
  if (!forced.chess || forced.submitted || forced.line.length >= forced.plyCount) {
    renderModeBoard("forced");
    return;
  }
  const move = tryApplyMove(forced.chess, { from: source, to: target, promotion: "q" });
  if (!move) {
    renderModeBoard("forced");
    return;
  }
  playMoveSound(move, forced.chess);
  forced.line.push(moveToUci(move));
  renderForcedLine();
  renderModeBoard("forced");
}

function forcedUndo() {
  const forced = state.forced;
  if (!forced.chess || forced.submitted || forced.line.length === 0) return;
  forced.chess.undo();
  forced.line.pop();
  renderForcedLine();
  renderModeBoard("forced");
}

function forcedReset() {
  const forced = state.forced;
  if (!forced.startFen || forced.submitted) return;
  forced.chess = new Chess(forced.startFen);
  forced.line = [];
  renderForcedLine();
  renderModeBoard("forced");
}

function renderForcedLine() {
  const forced = state.forced;
  forcedUi.entered.textContent = `${forced.line.length} / ${forced.plyCount}`;
  forcedUi.undo.disabled = forced.submitted || forced.line.length === 0;
  forcedUi.reset.disabled = forced.submitted || forced.line.length === 0;
  forcedUi.submit.disabled = forced.submitted || forced.line.length !== forced.plyCount;

  const replay = new Chess(forced.startFen || undefined);
  const items = [];
  for (let i = 0; i < forced.line.length; i += 1) {
    const san = sanFromUci(replay, forced.line[i]);
    const mover = i % 2 === 0 ? "you" : "opp";
    items.push(`<li class="forced-ply ${mover}"><span class="ply-num">${i + 1}.</span> ${escapeHtml(san)}</li>`);
  }
  for (let i = forced.line.length; i < forced.plyCount; i += 1) {
    items.push(`<li class="forced-ply pending"><span class="ply-num">${i + 1}.</span> …</li>`);
  }
  forcedUi.line.innerHTML = items.join("");
}

async function submitForcedLine() {
  const forced = state.forced;
  if (forced.submitted || forced.line.length !== forced.plyCount) return;
  forced.busy = true;
  forcedUi.submit.disabled = true;
  forcedUi.submit.textContent = "Validating…";
  try {
    const result = await request(`/api/forced/${forced.positionId}/submit`, {
      method: "POST",
      body: JSON.stringify({ line: forced.line }),
    });
    forced.submitted = true;
    renderForcedResult(result);
    renderForcedSummary(result.summary);
    playSound("end");
    await animateForcedSolution(result.solution_uci || []);
  } catch (error) {
    forcedUi.feedback.className = "feedback fail";
    forcedUi.feedback.textContent = error.message;
  } finally {
    forced.busy = false;
    forcedUi.submit.textContent = "Submit line";
    renderForcedLine();
    syncBoardInteractivity();
  }
}

function renderForcedResult(result) {
  const verdictIcon = { match: "✓", acceptable: "≈", wrong: "✗", missing: "✗", not_reached: "·" };
  const rows = result.verdicts
    .map((v) => {
      const icon = verdictIcon[v.verdict] || "·";
      const yourMove = v.user_san || v.user_uci || "—";
      const expected = v.verdict === "wrong" && v.expected_san
        ? ` <span class="expected">(best: ${escapeHtml(v.expected_san)})</span>`
        : "";
      const note = v.note ? ` <span class="note">${escapeHtml(v.note)}</span>` : "";
      return `<li class="verdict ${v.verdict}"><span class="verdict-icon">${icon}</span> ${v.ply + 1}. ${escapeHtml(yourMove)}${expected}${note}</li>`;
    })
    .join("");
  forcedUi.feedback.className = `feedback ${result.passed ? "pass" : "fail"}`;
  forcedUi.feedback.innerHTML = `
    <p><strong>${result.passed ? "Line verified" : "Not the forced line"}</strong>
       — ${result.matched_plies}/${result.total_plies} plies correct.</p>
    <ul class="verdict-list">${rows}</ul>
    <p>Main line: <code>${escapeHtml(result.solution_san)}</code></p>`;
}

async function animateForcedSolution(solutionUci) {
  const forced = state.forced;
  if (!forced.startFen || solutionUci.length === 0) return;
  forced.chess = new Chess(forced.startFen);
  renderModeBoard("forced");
  for (const uci of solutionUci) {
    await delay(650);
    if (state.view !== "forced") return;
    playMoveSoundFromUci(uci, forced.chess.fen());
    tryApplyMove(forced.chess, {
      from: uci.slice(0, 2),
      to: uci.slice(2, 4),
      promotion: uci.length > 4 ? uci.slice(4) : undefined,
    });
    board.set({ fen: forced.chess.fen(), lastMove: [uci.slice(0, 2), uci.slice(2, 4)] });
  }
}

function renderForcedSummary(summary) {
  if (!summary) return;
  forcedUi.total.textContent = summary.total;
  forcedUi.passed.textContent = summary.passed;
  forcedUi.streak.textContent = summary.streak;
  forcedUi.streakBadge.textContent = `streak ${summary.streak}`;
  forcedUi.streakBadge.classList.toggle("hot", summary.streak >= 3);
}

async function loadForcedSummary() {
  try {
    renderForcedSummary(await request("/api/forced/summary"));
  } catch {
    /* cosmetic */
  }
}

/* ── Eval + Sharpness Guess ────────────────────────────────────────────── */

const guessUi = {
  evalSlider: document.querySelector("#guess-eval"),
  evalValue: document.querySelector("#guess-eval-value"),
  sharpSlider: document.querySelector("#guess-sharp"),
  sharpValue: document.querySelector("#guess-sharp-value"),
  submit: document.querySelector("#guess-submit"),
  next: document.querySelector("#guess-next"),
  reveal: document.querySelector("#guess-reveal"),
  evalChart: document.querySelector("#guess-eval-chart"),
  sharpChart: document.querySelector("#guess-sharp-chart"),
};

guessUi.evalSlider.addEventListener("input", () => {
  const pawns = Number(guessUi.evalSlider.value) / 100;
  guessUi.evalValue.textContent = `${pawns >= 0 ? "+" : ""}${pawns.toFixed(2)}`;
});
guessUi.sharpSlider.addEventListener("input", () => {
  guessUi.sharpValue.textContent = guessUi.sharpSlider.value;
});
guessUi.submit.addEventListener("click", submitGuess);
guessUi.next.addEventListener("click", () => loadGuessPosition());

// ── Your Mistakes ─────────────────────────────────────────────────────────
const mistakesUi = {
  username: document.querySelector("#mistakes-username"),
  generate: document.querySelector("#mistakes-generate-btn"),
  genStatus: document.querySelector("#mistakes-gen-status"),
  prompt: document.querySelector("#mistakes-prompt"),
  feedback: document.querySelector("#mistakes-feedback"),
  next: document.querySelector("#mistakes-next"),
  bucketBadge: document.querySelector("#mistakes-bucket-badge"),
};

mistakesUi.generate.addEventListener("click", generateMistakes);
mistakesUi.next.addEventListener("click", () => loadMistakePuzzle());

function setMistakeStatus(text) {
  mistakesUi.genStatus.textContent = text;
}

async function enterMistakesView() {
  const m = state.mistakes;
  try {
    const u = await request("/api/mistakes/username");
    if (u.chesscom_username && !mistakesUi.username.value) {
      mistakesUi.username.value = u.chesscom_username;
    }
  } catch (error) {
    /* username lookup is non-fatal */
  }
  try {
    const r = await request("/api/mistakes/run");
    if (r.run) {
      setMistakeStatus(
        `Last sync: ${r.run.puzzles_created} puzzles from ${r.run.games_eligible} games (${r.run.status}).`,
      );
    }
  } catch (error) {
    /* run lookup is non-fatal */
  }
  if (!m.loaded) await loadMistakePuzzle();
  syncBoardInteractivity();
}

async function loadMistakePuzzle() {
  const m = state.mistakes;
  if (m.busy) return;
  m.busy = true;
  mistakesUi.next.disabled = true;
  board.setShapes([]);
  try {
    const puzzle = await request("/api/mistakes/next");
    m.positionId = puzzle.position_id;
    m.fen = puzzle.fen;
    m.sideToMove = puzzle.side_to_move;
    m.bucket = puzzle.bucket;
    m.plyCount = puzzle.ply_count;
    m.chess = new Chess(puzzle.fen);
    m.solved = false;
    m.loaded = true;
    mistakesUi.feedback.className = "feedback hidden";
    mistakesUi.feedback.innerHTML = "";
    const toMove = m.sideToMove === "w" ? "White" : "Black";
    const label =
      m.bucket === "missed_win"
        ? "you missed a win here. Find it."
        : "you blundered here. Find the move you should have played.";
    mistakesUi.prompt.textContent = `${toMove} to move — ${label}`;
    mistakesUi.bucketBadge.textContent = m.bucket === "missed_win" ? "missed win" : "blunder";
  } catch (error) {
    m.loaded = false;
    m.chess = null;
    mistakesUi.bucketBadge.textContent = "—";
    mistakesUi.prompt.textContent = error.message;
  } finally {
    m.busy = false;
    board.setShapes([]);
    syncBoardInteractivity();
  }
}

async function submitMistakeMove(source, target) {
  const m = state.mistakes;
  if (!m.chess || m.solved || m.busy) {
    renderModeBoard("mistakes");
    return;
  }
  const move = tryApplyMove(m.chess, { from: source, to: target, promotion: "q" });
  if (!move) {
    renderModeBoard("mistakes");
    return;
  }
  playMoveSound(move, m.chess);
  m.busy = true;
  board.set({ movable: { dests: new Map() } });
  let result;
  try {
    result = await request(`/api/mistakes/${m.positionId}/attempt`, {
      method: "POST",
      body: JSON.stringify({ move: moveToUci(move), step: 0 }),
    });
  } catch (error) {
    m.busy = false;
    m.chess = new Chess(m.fen);
    mistakesUi.prompt.textContent = error.message;
    syncBoardInteractivity();
    return;
  }
  m.solved = Boolean(result.solved);
  m.busy = false;
  // Snap back to the position the user actually faced, then overlay both moves.
  m.chess = new Chess(m.fen);
  renderMistakeFeedback(result);
  playSound("end");
  syncBoardInteractivity();
  drawMistakeArrows(result.best_move_uci, result.user_actual_uci);
  mistakesUi.next.disabled = false;
}

function drawMistakeArrows(bestUci, actualUci) {
  const shapes = [];
  if (bestUci) {
    shapes.push({ orig: bestUci.slice(0, 2), dest: bestUci.slice(2, 4), brush: "green" });
  }
  if (actualUci && actualUci !== bestUci) {
    shapes.push({ orig: actualUci.slice(0, 2), dest: actualUci.slice(2, 4), brush: "red" });
  }
  board.setShapes(shapes);
}

function renderMistakeFeedback(result) {
  const passed = Boolean(result.solved);
  mistakesUi.feedback.className = `feedback ${passed ? "pass" : "fail"}`;
  const head = passed ? "Found it." : "Not the move.";
  const bucketLabel = result.bucket === "missed_win" ? "Missed win" : "Blunder";
  const played = `<code>${escapeHtml(result.user_actual_san || "")}</code> <span class="hint">(red — what you played)</span>`;
  const best = `<code>${escapeHtml(result.best_move_san || "")}</code> <span class="hint">(green — the answer)</span>`;
  const evalLine = `<p class="hint">After your move: ${formatEval(result.eval_played_cp)} · best: ${formatEval(result.eval_best_cp)}</p>`;
  const link = result.game_url
    ? `<p><a href="${escapeHtml(result.game_url)}" target="_blank" rel="noopener">View this game ↗</a></p>`
    : "";
  mistakesUi.feedback.innerHTML =
    `<p><strong>${head}</strong> — ${bucketLabel}</p>` +
    `<p>You played ${played}.</p>` +
    `<p>Best was ${best}.</p>` +
    `<p>${escapeHtml(result.caption || "")}</p>` +
    evalLine +
    link;
}

async function generateMistakes() {
  const m = state.mistakes;
  if (m.generating) return;
  const username = mistakesUi.username.value.trim();
  if (!username) {
    setMistakeStatus("Enter your Chess.com username first.");
    return;
  }
  m.generating = true;
  mistakesUi.generate.disabled = true;
  setMistakeStatus("Starting…");
  try {
    const resp = await fetch("/api/mistakes/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chesscom_username: username }),
    });
    if (!resp.ok || !resp.body) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "Generation failed");
    }
    await consumeEventStream(resp.body, (event, data) => {
      if (event === "start") {
        setMistakeStatus("Scanning your games…");
      } else if (event === "progress") {
        setMistakeStatus(`Scanned ${data.games_scanned} games · ${data.puzzles_created} found…`);
      } else if (event === "done") {
        setMistakeStatus(`Done — ${data.puzzles_created} puzzles from ${data.games_eligible} games.`);
      } else if (event === "error") {
        setMistakeStatus(`Error: ${data.message || "generation failed"}`);
      }
    });
    state.mistakes.loaded = false;
    await loadMistakePuzzle();
  } catch (error) {
    setMistakeStatus(error.message);
  } finally {
    m.generating = false;
    mistakesUi.generate.disabled = false;
  }
}

// Parse a `fetch` ReadableStream of Server-Sent Events (same wire format the
// volatility PGN analyzer consumes), invoking onEvent(name, data) per event.
async function consumeEventStream(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  // sse_starlette terminates events with CRLF (`\r\n\r\n`); also tolerate `\n\n`.
  const splitRe = /\r\n\r\n|\n\n/;
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let match;
    while ((match = splitRe.exec(buffer))) {
      const chunk = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      let event = "message";
      const dataLines = [];
      for (const line of chunk.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) {
        let data = {};
        try {
          data = JSON.parse(dataLines.join("\n"));
        } catch (error) {
          /* ignore malformed event */
        }
        onEvent(event, data);
      }
    }
  }
}

async function enterGuessView() {
  if (!state.guess.loaded) {
    await loadGuessPosition();
    await loadGuessHistory();
  }
  syncBoardInteractivity();
}

async function loadGuessPosition() {
  const guess = state.guess;
  if (guess.busy) return;
  guess.busy = true;
  guessUi.next.disabled = true;
  try {
    const position = await request("/api/guess/next");
    guess.positionId = position.position_id;
    guess.chess = new Chess(position.fen);
    guess.revealed = false;
    guess.loaded = true;
    guessUi.reveal.classList.add("hidden");
    guessUi.reveal.innerHTML = "";
    guessUi.submit.disabled = false;
    guessUi.evalSlider.value = "0";
    guessUi.sharpSlider.value = "50";
    guessUi.evalValue.textContent = "+0.00";
    guessUi.sharpValue.textContent = "50";
  } catch (error) {
    guessUi.reveal.classList.remove("hidden");
    guessUi.reveal.textContent = error.message;
  } finally {
    guess.busy = false;
    guessUi.next.disabled = false;
    syncBoardInteractivity();
  }
}

async function submitGuess() {
  const guess = state.guess;
  if (!guess.chess || guess.revealed || guess.busy) return;
  guess.busy = true;
  guessUi.submit.disabled = true;
  guessUi.submit.textContent = "Analyzing…";
  try {
    const result = await request("/api/guess/submit", {
      method: "POST",
      body: JSON.stringify({
        position_id: guess.positionId,
        fen: guess.chess.fen(),
        guessed_eval_cp: Number(guessUi.evalSlider.value),
        guessed_sharpness: Number(guessUi.sharpSlider.value),
      }),
    });
    guess.revealed = true;
    renderGuessReveal(result);
    playSound("end");
    await loadGuessHistory();
  } catch (error) {
    guessUi.reveal.classList.remove("hidden");
    guessUi.reveal.textContent = error.message;
  } finally {
    guess.busy = false;
    guessUi.submit.textContent = "Lock in guess";
  }
}

function renderGuessReveal(result) {
  const evalErrPawns = (result.eval_error_cp / 100).toFixed(2);
  const evalGrade = result.eval_error_cp <= 50 ? "good" : result.eval_error_cp <= 150 ? "ok" : "bad";
  const sharpGrade = result.sharpness_error <= 10 ? "good" : result.sharpness_error <= 25 ? "ok" : "bad";
  const decidedNote = result.decided
    ? "<p class=\"hint\">Position is already decided — sharpness reads low by design.</p>"
    : "";
  guessUi.reveal.classList.remove("hidden");
  guessUi.reveal.innerHTML = `
    <h3 class="card-title">Reveal</h3>
    <div class="guess-result-grid">
      <div>
        <span class="label">Eval</span>
        <p>You: <strong>${formatEval(result.guessed_eval_cp)}</strong></p>
        <p>Engine: <strong>${formatEval(clampCp(result.actual_eval_cp))}</strong></p>
        <span class="chip ${evalGrade}">off by ${evalErrPawns}</span>
      </div>
      <div>
        <span class="label">Sharpness</span>
        <p>You: <strong>${Math.round(result.guessed_sharpness)}</strong></p>
        <p>Actual: <strong>${Math.round(result.actual_sharpness)}</strong></p>
        <span class="chip ${sharpGrade}">off by ${Math.round(result.sharpness_error)}</span>
      </div>
    </div>
    ${decidedNote}
    <button id="guess-another" type="button" class="btn-primary">Next position</button>`;
  guessUi.reveal.querySelector("#guess-another").addEventListener("click", () => loadGuessPosition());
}

function clampCp(cp) {
  return Math.max(-800, Math.min(800, cp));
}

async function loadGuessHistory() {
  let attempts = [];
  try {
    attempts = (await request("/api/guess/history")).attempts || [];
  } catch {
    return;
  }
  drawCalibrationChart("eval", guessUi.evalChart, attempts.map((a) => ({
    x: clampCp(a.actual_eval_cp) / 100,
    y: clampCp(a.guessed_eval_cp) / 100,
  })), { min: -8, max: 8 });
  drawCalibrationChart("sharp", guessUi.sharpChart, attempts.map((a) => ({
    x: a.actual_sharpness,
    y: a.guessed_sharpness,
  })), { min: 0, max: 100 });
}

function drawCalibrationChart(key, canvas, points, range) {
  if (!canvas || typeof window.Chart === "undefined") return;
  const existing = state.guess.charts[key];
  if (existing) existing.destroy();
  state.guess.charts[key] = new window.Chart(canvas.getContext("2d"), {
    type: "scatter",
    data: {
      datasets: [
        {
          label: "perfect",
          type: "line",
          data: [{ x: range.min, y: range.min }, { x: range.max, y: range.max }],
          borderColor: "rgba(231, 237, 245, 0.22)",
          borderWidth: 1.5,
          borderDash: [6, 5],
          pointRadius: 0,
          fill: false,
        },
        {
          label: "your guesses",
          data: points,
          backgroundColor: "rgba(138, 180, 248, 0.85)",
          pointRadius: 4,
          pointHoverRadius: 6,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      plugins: { legend: { display: false } },
      scales: {
        x: {
          min: range.min,
          max: range.max,
          title: { display: true, text: "actual", color: "#9aa6b2", font: { size: 10 } },
          grid: { color: "rgba(255,255,255,0.06)" },
          ticks: { color: "#9aa6b2", font: { size: 10 } },
        },
        y: {
          min: range.min,
          max: range.max,
          title: { display: true, text: "guessed", color: "#9aa6b2", font: { size: 10 } },
          grid: { color: "rgba(255,255,255,0.06)" },
          ticks: { color: "#9aa6b2", font: { size: 10 } },
        },
      },
    },
  });
}

window.ChessTrainer = { state, board };
