// ChessMax shell: one flat tab bar that delegates to the two embedded apps.
// Puzzle tabs call the trainer's switchTab(); vol tabs call the vol app's
// setTab(). Both functions are exposed on window by the respective app.js.

const TAB_APP = {
  train: "puzzles",
  evalhold: "puzzles",
  defense: "puzzles",
  forced: "puzzles",
  guess: "puzzles",
  mistakes: "puzzles",
  playout: "puzzles",
  stats: "puzzles",
  game: "vol",
  editor: "vol",
  library: "vol",
  about: "vol",
};

const shellTabs = Array.from(document.querySelectorAll(".shell-tab"));
const puzzlesRoot = document.getElementById("puzzles-root");
const volRoot = document.getElementById("vol-root");

function highlight(tabId) {
  shellTabs.forEach((btn) => {
    const active = btn.dataset.tab === tabId;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
}

function switchShellTab(tabId) {
  const app = TAB_APP[tabId];
  if (!app) return;
  highlight(tabId);
  puzzlesRoot.classList.toggle("hidden", app !== "puzzles");
  volRoot.classList.toggle("hidden", app !== "vol");
  if (app === "puzzles") {
    if (window.__puzzlesSwitchTab) window.__puzzlesSwitchTab(tabId);
  } else {
    if (window.__volSetTab) window.__volSetTab(tabId);
  }
  // Boards/charts initialised while hidden have zero size; both apps listen
  // for window resize, so nudge them after the panes become visible.
  requestAnimationFrame(() => {
    window.dispatchEvent(new Event("resize"));
    document.body.dispatchEvent(new Event("chessground.resize"));
  });
}

shellTabs.forEach((btn) => {
  btn.addEventListener("click", () => switchShellTab(btn.dataset.tab));
});

// Keep the shell highlight in sync when an app switches tabs internally
// (e.g. "Play it out" jumps to the playout view, the library opens a game
// in the Game Analyzer).
window.__onPuzzlesTabChange = (tabId) => {
  if (puzzlesRoot.classList.contains("hidden")) return;
  highlight(tabId);
};
window.__onVolTabChange = (tabId) => {
  if (volRoot.classList.contains("hidden")) return;
  highlight(tabId);
};

switchShellTab("train");
