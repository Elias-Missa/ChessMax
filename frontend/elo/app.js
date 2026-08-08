// Guess the Elo Duels — matchmaking, game replay, 2-minute guess, closest wins.
/* eslint-disable no-undef */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const api = (path, opts) =>
    fetch(path, Object.assign({ headers: { "Content-Type": "application/json" }, credentials: "same-origin" }, opts || {}));

  let active = false;
  let state = "idle"; // idle | searching | dueling | locked | result
  let duel = null;
  let frames = [];
  let frameIdx = 0;
  let cg = null;
  let searchTimer = null;
  let pollTimer = null;
  let tickTimer = null;
  let autoplayTimer = null;

  // ── Board replay ──────────────────────────────────────────────────────── //
  function buildFrames(moves) {
    const out = [{ fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", from: null, to: null, label: "Start" }];
    if (typeof Chess === "undefined") return out;
    const game = new Chess();
    (moves || []).forEach((san, i) => {
      const mv = game.move(san, { sloppy: true });
      if (!mv) return;
      const n = Math.floor(i / 2) + 1;
      const label = i % 2 === 0 ? `${n}. ${san}` : `${n}… ${san}`;
      out.push({ fen: game.fen(), from: mv.from, to: mv.to, label });
    });
    return out;
  }

  function ensureBoard() {
    if (cg || typeof Chessground === "undefined") return;
    cg = Chessground($("eloBoard"), {
      viewOnly: true,
      coordinates: false,
      fen: frames.length ? frames[0].fen : undefined,
      animation: { enabled: true, duration: 180 },
    });
  }

  function renderFrame(idx) {
    frameIdx = Math.max(0, Math.min(frames.length - 1, idx));
    const f = frames[frameIdx];
    if (cg && f) cg.set({ fen: f.fen, lastMove: f.from ? [f.from, f.to] : undefined });
    const label = $("eloMoveLabel");
    if (label && f) label.textContent = frameIdx === 0 ? "Start" : `${f.label}  (${frameIdx}/${frames.length - 1})`;
  }

  function stopAutoplay() {
    if (autoplayTimer) clearInterval(autoplayTimer);
    autoplayTimer = null;
    const btn = $("eloPlay");
    if (btn) btn.innerHTML = "&#9654;";
  }

  function startAutoplay() {
    stopAutoplay();
    const btn = $("eloPlay");
    if (btn) btn.innerHTML = "&#10073;&#10073;";
    autoplayTimer = setInterval(() => {
      if (frameIdx >= frames.length - 1) {
        stopAutoplay();
        return;
      }
      renderFrame(frameIdx + 1);
    }, 850);
  }

  function toggleAutoplay() {
    if (autoplayTimer) stopAutoplay();
    else {
      if (frameIdx >= frames.length - 1) renderFrame(0);
      startAutoplay();
    }
  }

  // ── Timer ─────────────────────────────────────────────────────────────── //
  function stopTimer() {
    if (tickTimer) clearInterval(tickTimer);
    tickTimer = null;
  }

  function startTimer(deadlineTs) {
    stopTimer();
    const el = $("eloTimer");
    const update = () => {
      const rem = Math.max(0, Math.ceil((deadlineTs * 1000 - Date.now()) / 1000));
      const m = Math.floor(rem / 60);
      const s = rem % 60;
      if (el) {
        el.textContent = `${m}:${String(s).padStart(2, "0")}`;
        el.dataset.low = rem <= 20 ? "true" : "false";
      }
      if (rem <= 0) {
        stopTimer();
        onDeadline();
      }
    };
    update();
    tickTimer = setInterval(update, 250);
  }

  function onDeadline() {
    if (state === "dueling") {
      lockGuess(true);
    } else if (state === "locked") {
      pollDuel();
    }
  }

  // ── Matchmaking ───────────────────────────────────────────────────────── //
  async function refreshStats() {
    try {
      const r = await api("/api/elo/stats");
      if (!r.ok) return;
      const s = await r.json();
      $("eloWins").textContent = s.wins;
      $("eloLosses").textContent = s.losses;
      $("eloDraws").textContent = s.draws;
    } catch (_) {
      /* ignore */
    }
  }

  function showLobby() {
    state = "idle";
    stopEverything();
    $("eloLobby").classList.remove("hidden");
    $("eloDuel").classList.add("hidden");
    $("eloSearching").classList.add("hidden");
    $("eloFindBtn").classList.remove("hidden");
    $("eloLobbyError").classList.add("hidden");
    refreshStats();
  }

  function showError(msg) {
    state = "idle";
    $("eloSearching").classList.add("hidden");
    $("eloFindBtn").classList.remove("hidden");
    const e = $("eloLobbyError");
    if (e) {
      e.textContent = msg;
      e.classList.remove("hidden");
    }
  }

  function findMatch() {
    state = "searching";
    $("eloFindBtn").classList.add("hidden");
    $("eloSearching").classList.remove("hidden");
    $("eloLobbyError").classList.add("hidden");
    const poll = async () => {
      if (state !== "searching") return;
      try {
        const r = await api("/api/elo/match", { method: "POST" });
        if (r.status === 503) {
          const e = await r.json().catch(() => ({}));
          showError(e.detail || "No duel games available yet.");
          return;
        }
        if (r.status === 401) {
          showError("Please log in to play duels.");
          return;
        }
        if (r.ok) {
          const data = await r.json();
          if (data.status === "matched") {
            startDuel(data.duel);
            return;
          }
        }
      } catch (_) {
        /* keep trying */
      }
      searchTimer = setTimeout(poll, 1500);
    };
    poll();
  }

  function cancelSearch() {
    state = "idle";
    if (searchTimer) clearTimeout(searchTimer);
    api("/api/elo/leave", { method: "POST" }).catch(() => {});
    showLobby();
  }

  // ── Duel ──────────────────────────────────────────────────────────────── //
  function startDuel(d) {
    duel = d;
    $("eloLobby").classList.add("hidden");
    $("eloDuel").classList.remove("hidden");
    $("eloOppName").textContent = d.opponent || "Opponent";
    $("eloOppLabel").textContent = d.opponent || "Opponent";

    frames = buildFrames(d.moves || []);
    ensureBoard();
    renderFrame(0);
    startAutoplay();

    $("eloResult").classList.add("hidden");
    $("eloGuessBox").classList.remove("hidden");
    $("eloWaitOpp").classList.add("hidden");
    $("eloTimer").classList.remove("hidden");
    const slider = $("eloGuessSlider");
    const lockBtn = $("eloLockBtn");
    if (d.status === "done") {
      showResult(d);
      return;
    }
    startTimer(d.deadline_ts);
    if (d.your_guess != null) {
      state = "locked";
      afterLock(d.your_guess);
      pollDuel();
    } else {
      state = "dueling";
      slider.value = 1500;
      slider.disabled = false;
      $("eloGuessVal").textContent = "1500";
      lockBtn.disabled = false;
      lockBtn.classList.remove("hidden");
    }
  }

  function afterLock(value) {
    $("eloLockBtn").classList.add("hidden");
    $("eloGuessSlider").disabled = true;
    const wait = $("eloWaitOpp");
    wait.classList.remove("hidden");
    const lv = $("eloLockedVal");
    if (lv) lv.textContent = String(value);
  }

  async function lockGuess(force) {
    if (state !== "dueling") return;
    const guess = parseInt($("eloGuessSlider").value, 10) || 1500;
    state = "locked";
    afterLock(guess);
    try {
      const r = await api("/api/elo/guess", {
        method: "POST",
        body: JSON.stringify({ duel_id: duel.duel_id, guess }),
      });
      if (r.ok) {
        const updated = await r.json();
        if (updated.status === "done") {
          showResult(updated);
          return;
        }
      }
    } catch (_) {
      /* fall through to polling */
    }
    if (!force) pollDuel();
    else pollDuel();
  }

  function pollDuel() {
    if (pollTimer) clearTimeout(pollTimer);
    const poll = async () => {
      if (!duel) return;
      try {
        const r = await api(`/api/elo/duel/${duel.duel_id}`);
        if (r.ok) {
          const d = await r.json();
          if (d.status === "done") {
            showResult(d);
            return;
          }
        }
      } catch (_) {
        /* keep polling */
      }
      pollTimer = setTimeout(poll, 1500);
    };
    poll();
  }

  function showResult(d) {
    state = "result";
    stopTimer();
    if (pollTimer) clearTimeout(pollTimer);
    duel = d;
    $("eloGuessBox").classList.add("hidden");
    $("eloTimer").classList.add("hidden");
    const res = $("eloResult");
    res.classList.remove("hidden");

    const outcome = $("eloOutcome");
    const label = d.outcome === "win" ? "You win!" : d.outcome === "loss" ? "You lost" : "Draw";
    outcome.textContent = label;
    outcome.dataset.outcome = d.outcome || "draw";

    $("eloTrue").textContent = d.true_elo != null ? d.true_elo : "—";
    $("eloYourGuess").textContent = d.your_guess != null ? d.your_guess : "—";
    $("eloYourPts").textContent = d.your_points != null ? `${d.your_points} pts` : "—";
    $("eloOppGuess").textContent = d.opponent_guess != null ? d.opponent_guess : "—";
    $("eloOppPts").textContent = d.opponent_points != null ? `${d.opponent_points} pts` : "—";
    $("eloOppLabel").textContent = d.opponent || "Opponent";
    refreshStats();
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────── //
  function stopEverything() {
    if (searchTimer) clearTimeout(searchTimer);
    if (pollTimer) clearTimeout(pollTimer);
    stopTimer();
    stopAutoplay();
  }

  function wire() {
    const on = (id, ev, fn) => {
      const el = $(id);
      if (el) el.addEventListener(ev, fn);
    };
    on("eloFindBtn", "click", findMatch);
    on("eloCancelBtn", "click", cancelSearch);
    on("eloAgainBtn", "click", showLobby);
    on("eloLockBtn", "click", () => lockGuess(false));
    on("eloFirst", "click", () => { stopAutoplay(); renderFrame(0); });
    on("eloPrev", "click", () => { stopAutoplay(); renderFrame(frameIdx - 1); });
    on("eloNext", "click", () => { stopAutoplay(); renderFrame(frameIdx + 1); });
    on("eloLast", "click", () => { stopAutoplay(); renderFrame(frames.length - 1); });
    on("eloPlay", "click", toggleAutoplay);
    const slider = $("eloGuessSlider");
    if (slider) slider.addEventListener("input", () => { $("eloGuessVal").textContent = slider.value; });
  }

  window.__eloSetActive = function (isActive) {
    active = isActive;
    if (isActive) {
      if (state === "idle") showLobby();
      // The board may have been created while hidden (zero size); redraw.
      if (cg) requestAnimationFrame(() => renderFrame(frameIdx));
    } else if (state === "searching") {
      cancelSearch();
    } else {
      stopEverything();
    }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
