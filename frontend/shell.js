// ChessMax shell: History-API routes across Puzzles, Training, Game Review,
// Insights, and Duels. Training, Game Review and Duels keep nested sub-navs.
//
// Duels is one top-level tab over two independent app roots (`elo` + `eval`).
// `/guess-the-elo` and `/guess-the-eval` predate the merge and are kept as
// aliases so old links and bookmarks still resolve — they carry the same
// `top: "duels"`, so the single tab highlights either way.

const ROUTES = [
  { path: "/", app: "home", tab: "home", top: "home" },
  { path: "/puzzles", app: "puzzles", tab: "train", top: "puzzles" },
  { path: "/training", app: "puzzles", tab: "evalhold", top: "training" },
  { path: "/training/eval-hold", app: "puzzles", tab: "evalhold", top: "training" },
  { path: "/training/defense", app: "puzzles", tab: "defense", top: "training" },
  { path: "/training/forced", app: "puzzles", tab: "forced", top: "training" },
  { path: "/training/guess-eval", app: "puzzles", tab: "guess", top: "training" },
  { path: "/training/mistakes", app: "puzzles", tab: "mistakes", top: "training" },
  { path: "/training/playout", app: "puzzles", tab: "playout", top: "training" },
  { path: "/training/stats", app: "puzzles", tab: "stats", top: "training" },
  { path: "/game-review", app: "vol", tab: "game", top: "game-review" },
  { path: "/game-review/editor", app: "vol", tab: "editor", top: "game-review" },
  { path: "/game-review/library", app: "vol", tab: "library", top: "game-review" },
  { path: "/game-review/why", app: "vol", tab: "about", top: "game-review" },
  { path: "/insights", app: "insights", tab: "insights", top: "insights" },
  { path: "/duels", app: "elo", tab: "eloduels", top: "duels" },
  { path: "/duels/elo", app: "elo", tab: "eloduels", top: "duels" },
  { path: "/duels/eval", app: "eval", tab: "evalduels", top: "duels" },
  { path: "/guess-the-elo", app: "elo", tab: "eloduels", top: "duels" },
  { path: "/guess-the-eval", app: "eval", tab: "evalduels", top: "duels" },
];

const TAB_TO_PATH = Object.fromEntries(
  ROUTES.filter((r) => r.path !== "/training").map((r) => [r.tab, r.path]),
);
TAB_TO_PATH.home = "/";
TAB_TO_PATH.train = "/puzzles";
TAB_TO_PATH.game = "/game-review";
TAB_TO_PATH.editor = "/game-review/editor";
TAB_TO_PATH.library = "/game-review/library";
TAB_TO_PATH.about = "/game-review/why";
TAB_TO_PATH.eloduels = "/duels/elo";
TAB_TO_PATH.evalduels = "/duels/eval";
TAB_TO_PATH.insights = "/insights";

const TRAINING_TABS = new Set([
  "evalhold", "defense", "forced", "guess", "mistakes", "playout", "stats",
]);
const VOL_TABS = new Set(["game", "editor", "library", "about"]);
const DUEL_APPS = new Set(["elo", "eval"]);

const shellTabs = Array.from(document.querySelectorAll(".shell-tab[data-top]"));
const trainingNav = document.getElementById("training-subnav");
const gameReviewNav = document.getElementById("game-review-subnav");
const duelsNav = document.getElementById("duels-subnav");
const homeRoot = document.getElementById("home-root");
const puzzlesRoot = document.getElementById("puzzles-root");
const volRoot = document.getElementById("vol-root");
const eloRoot = document.getElementById("elo-root");
const evalRoot = document.getElementById("eval-root");
const insightsRoot = document.getElementById("insights-root");

let navigating = false;
let currentRoute = null;

function matchRoute(pathname) {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/" || path === "") {
    return ROUTES.find((r) => r.path === "/");
  }
  const exact = ROUTES.find((r) => r.path === path);
  if (exact) return exact;
  if (path.startsWith("/insights")) {
    const base = ROUTES.find((r) => r.path === "/insights");
    return { ...base, path };
  }
  // Prefix match for unknown training/game-review children → hub defaults
  if (path.startsWith("/training")) {
    return ROUTES.find((r) => r.path === "/training");
  }
  if (path.startsWith("/game-review")) {
    return ROUTES.find((r) => r.path === "/game-review");
  }
  if (path.startsWith("/duels")) {
    return ROUTES.find((r) => r.path === "/duels");
  }
  return ROUTES.find((r) => r.path === "/");
}

function highlightTop(topId) {
  shellTabs.forEach((btn) => {
    const active = btn.dataset.top === topId;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
}

function highlightSubnav(nav, tabId) {
  if (!nav) return;
  nav.querySelectorAll("[data-tab]").forEach((btn) => {
    const active = btn.dataset.tab === tabId;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
}

function showRoots(app, tab) {
  if (homeRoot) homeRoot.classList.toggle("hidden", app !== "home");
  puzzlesRoot.classList.toggle("hidden", app !== "puzzles");
  volRoot.classList.toggle("hidden", app !== "vol");
  if (eloRoot) eloRoot.classList.toggle("hidden", app !== "elo");
  if (evalRoot) evalRoot.classList.toggle("hidden", app !== "eval");
  if (insightsRoot) insightsRoot.classList.toggle("hidden", app !== "insights");
  if (trainingNav) {
    trainingNav.classList.toggle("hidden", app !== "puzzles" || !TRAINING_TABS.has(tab));
  }
  if (gameReviewNav) gameReviewNav.classList.toggle("hidden", app !== "vol");
  if (duelsNav) duelsNav.classList.toggle("hidden", !DUEL_APPS.has(app));
}

function applyRoute(route, { push = false } = {}) {
  if (!route) return;
  currentRoute = route;
  if (push) {
    navigating = true;
    const url = route.path;
    if (window.location.pathname !== url) {
      history.pushState({ chessmax: true, path: url }, "", url);
    }
    navigating = false;
  }

  highlightTop(route.top);
  showRoots(route.app, route.tab);

  // Every app gets told whether it is the active one, exactly once, so adding a
  // sixth app is one line here rather than a fresh `false` in five branches.
  const app = route.app;
  if (window.__homeSetActive) window.__homeSetActive(app === "home");
  if (window.__eloSetActive) window.__eloSetActive(app === "elo");
  if (window.__evalSetActive) window.__evalSetActive(app === "eval");
  if (window.__insightsSetActive) {
    if (app === "insights") window.__insightsSetActive(true, route.path);
    else window.__insightsSetActive(false);
  }
  if (app === "puzzles") {
    if (trainingNav) {
      trainingNav.classList.toggle("hidden", !TRAINING_TABS.has(route.tab));
      highlightSubnav(trainingNav, route.tab);
    }
    if (window.__puzzlesSwitchTab) window.__puzzlesSwitchTab(route.tab);
  } else if (window.__puzzlesDeactivate) {
    window.__puzzlesDeactivate();
  }
  if (app === "vol") {
    if (gameReviewNav) highlightSubnav(gameReviewNav, route.tab);
    if (window.__volSetTab) window.__volSetTab(route.tab);
  }
  if (DUEL_APPS.has(app) && duelsNav) highlightSubnav(duelsNav, route.tab);

  requestAnimationFrame(() => {
    window.dispatchEvent(new Event("resize"));
    document.body.dispatchEvent(new Event("chessground.resize"));
  });
}

function navigateToPath(path, { push = true } = {}) {
  applyRoute(matchRoute(path), { push });
}

function navigateToTab(tabId, { push = true } = {}) {
  const path = TAB_TO_PATH[tabId];
  if (!path) return;
  navigateToPath(path, { push });
}

function switchShellTab(tabId) {
  navigateToTab(tabId, { push: true });
}

shellTabs.forEach((btn) => {
  btn.addEventListener("click", () => {
    const path = btn.dataset.route || TAB_TO_PATH[btn.dataset.tab];
    if (path) navigateToPath(path, { push: true });
  });
});

// The wordmark is a home link, not a tab — it carries no `data-top`, so it is
// not in `shellTabs` and never picks up the active highlight.
const homeLink = document.querySelector(".shell-home");
if (homeLink) {
  homeLink.addEventListener("click", () => navigateToPath("/", { push: true }));
}

if (trainingNav) {
  trainingNav.querySelectorAll("[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => navigateToTab(btn.dataset.tab, { push: true }));
  });
}
if (gameReviewNav) {
  gameReviewNav.querySelectorAll("[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => navigateToTab(btn.dataset.tab, { push: true }));
  });
}
if (duelsNav) {
  duelsNav.querySelectorAll("[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => navigateToTab(btn.dataset.tab, { push: true }));
  });
}

window.addEventListener("popstate", () => {
  if (navigating) return;
  navigateToPath(window.location.pathname, { push: false });
});

// Keep URL in sync when an app switches tabs internally (Play Out, Library → game).
window.__onPuzzlesTabChange = (tabId) => {
  if (puzzlesRoot.classList.contains("hidden")) return;
  const path = TAB_TO_PATH[tabId];
  if (!path) return;
  const route = matchRoute(path);
  currentRoute = route;
  highlightTop(route.top);
  if (trainingNav) {
    trainingNav.classList.toggle("hidden", !TRAINING_TABS.has(tabId));
    highlightSubnav(trainingNav, tabId);
  }
  if (window.location.pathname !== path) {
    navigating = true;
    history.pushState({ chessmax: true, path }, "", path);
    navigating = false;
  }
};

window.__onVolTabChange = (tabId) => {
  if (volRoot.classList.contains("hidden")) return;
  if (!VOL_TABS.has(tabId)) return;
  const path = TAB_TO_PATH[tabId];
  if (!path) return;
  const route = matchRoute(path);
  currentRoute = route;
  highlightTop(route.top);
  if (gameReviewNav) highlightSubnav(gameReviewNav, tabId);
  if (window.location.pathname !== path) {
    navigating = true;
    history.pushState({ chessmax: true, path }, "", path);
    navigating = false;
  }
};

// The sub-nav and the full-screen overlays all sit directly under the header.
// Hard-coding that offset in CSS left a sliver of scrolling content showing
// through whenever the header measured taller than the guess, so publish the
// real height and let everything key off it.
function publishHeaderHeight() {
  const header = document.querySelector(".shell-header");
  if (!header) return;
  const h = Math.round(header.getBoundingClientRect().height);
  if (h > 0) document.documentElement.style.setProperty("--shell-header-h", `${h}px`);
}

publishHeaderHeight();
window.addEventListener("resize", publishHeaderHeight);
if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(publishHeaderHeight).catch(() => {});
}

window.__shellNavigate = navigateToPath;
window.__shellSwitchTab = switchShellTab;

// Boot from the current URL — `/` is the home screen, everything else is a
// deep link into one of the apps.
navigateToPath(window.location.pathname, { push: false });
