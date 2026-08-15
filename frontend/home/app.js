// ChessMax home screen: ambient board, scroll reveals, pointer-tracked card
// spotlight/tilt, and count-up stats. Purely presentational — every navigation
// goes back through `window.__shellNavigate`, so the shell stays the single
// owner of routing.

const root = document.getElementById("home-root");

if (root) {
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ── Ambient 8×8 board ────────────────────────────────────────────────
  // Lighting every square at once reads as a flicker; keying the delay off the
  // anti-diagonal index makes the pulse sweep across the board instead.
  const boardEl = document.getElementById("hmBoard");
  if (boardEl && !boardEl.childElementCount) {
    const frag = document.createDocumentFragment();
    for (let i = 0; i < 64; i += 1) {
      const rank = Math.floor(i / 8);
      const file = i % 8;
      const tile = document.createElement("div");
      tile.className = (rank + file) % 2 ? "hm-tile dark" : "hm-tile";
      if (!reduceMotion && (rank * 3 + file * 5) % 7 === 0) tile.classList.add("lit");
      tile.style.setProperty("--d", String(rank + file));
      frag.appendChild(tile);
    }
    boardEl.appendChild(frag);
  }

  // ── Scroll reveal ────────────────────────────────────────────────────
  // `#home-root` starts hidden, so elements measure as out-of-viewport until the
  // route activates. `revealAll` is therefore also called from setActive.
  const revealTargets = () => Array.from(root.querySelectorAll(".hm-reveal"));
  let observer = null;

  function revealAll() {
    revealTargets().forEach((el) => el.classList.add("is-in"));
  }

  if (typeof IntersectionObserver === "function" && !reduceMotion) {
    observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-in");
          observer.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.08 },
    );
    revealTargets().forEach((el) => observer.observe(el));
  } else {
    revealAll();
  }

  // ── Cursor spotlight + subtle tilt ───────────────────────────────────
  // Pointer position is written as CSS variables (percent for the spotlight,
  // degrees for the tilt) so the animation itself stays in the compositor.
  const cards = Array.from(root.querySelectorAll(".hm-card"));
  const MAX_TILT = 4.5;

  cards.forEach((card) => {
    if (reduceMotion) return;
    card.addEventListener("pointermove", (event) => {
      const rect = card.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width;
      const y = (event.clientY - rect.top) / rect.height;
      card.style.setProperty("--mx", `${(x * 100).toFixed(1)}%`);
      card.style.setProperty("--my", `${(y * 100).toFixed(1)}%`);
      card.style.setProperty("--hm-tilt-y", `${((x - 0.5) * 2 * MAX_TILT).toFixed(2)}deg`);
      card.style.setProperty("--hm-tilt-x", `${((0.5 - y) * 2 * MAX_TILT).toFixed(2)}deg`);
    });
    card.addEventListener("pointerleave", () => {
      card.style.setProperty("--hm-tilt-x", "0deg");
      card.style.setProperty("--hm-tilt-y", "0deg");
    });
  });

  // ── Navigation ───────────────────────────────────────────────────────
  root.querySelectorAll("[data-home-go]").forEach((el) => {
    el.addEventListener("click", (event) => {
      event.preventDefault();
      const path = el.dataset.homeGo;
      if (path && window.__shellNavigate) window.__shellNavigate(path, { push: true });
    });
  });

  // ── Count-up stats ───────────────────────────────────────────────────
  // Runs once, the first time the strip is actually on screen.
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

  // ── Shell hook ───────────────────────────────────────────────────────
  window.__homeSetActive = (active) => {
    root.classList.toggle("hidden", !active);
    if (!active) return;
    // Now that the root has a layout box, let the observer (or the fallback)
    // decide what is on screen and start the counters.
    requestAnimationFrame(() => {
      if (observer) {
        revealTargets().forEach((el) => {
          if (!el.classList.contains("is-in")) observer.observe(el);
        });
        // Anything already above the fold should not wait for a scroll.
        revealTargets().forEach((el) => {
          const rect = el.getBoundingClientRect();
          if (rect.top < window.innerHeight) el.classList.add("is-in");
        });
      } else {
        revealAll();
      }
      maybeCount();
    });
  };
}
