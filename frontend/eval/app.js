// Guess the Eval Duels — matchmaking, position board, 1-minute timer, closest eval guess wins.
/* eslint-disable no-undef */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const api = (path, opts) =>
    fetch(path, Object.assign({ headers: { "Content-Type": "application/json" }, credentials: "same-origin" }, opts || {}));

  let active = false;
  let state = "idle"; // idle | searching | dueling | locked | result
  let duel = null;
  let cg = null;
  let searchTimer = null;
  let pollTimer = null;
  let tickTimer = null;

  // ── Formatters ────────────────────────────────────────────────────────── //
  function formatCp(cp) {
    if (cp == null) return "—";
    if (cp === 0) return "0.00";
    const sign = cp > 0 ? "+" : "−";
    return sign + (Math.abs(cp) / 100).toFixed(2);
  }

  function sliderToCp(val) {
    return Math.round(parseFloat(val) * 100);
  }

  function cpToSlider(cp) {
    return (cp / 100).toFixed(2);
  }

  // ── Board ─────────────────────────────────────────────────────────────── //
  function ensureBoard(fen) {
    const el = $("evalBoard");
    if (!el) return;
    if (cg) {
      cg.set({ fen: fen || "start" });
    } else if (typeof Chessground !== "undefined") {
      cg = Chessground(el, {
        viewOnly: true,
        coordinates: false,
        fen: fen || "start",
        animation: { enabled: true, duration: 180 },
      });
    }
  }

  // ── Timer (1-Minute / 60-Second Deadline) ─────────────────────────────── //
  function stopTimer() {
    if (tickTimer) clearInterval(tickTimer);
    tickTimer = null;
  }

  function startTimer(deadlineTs) {
    stopTimer();
    const el = $("evalTimer");
    const update = () => {
      const rem = Math.max(0, Math.ceil((deadlineTs * 1000 - Date.now()) / 1000));
      const m = Math.floor(rem / 60);
      const s = rem % 60;
      if (el) {
        el.textContent = `${m}:${String(s).padStart(2, "0")}`;
        el.dataset.low = rem <= 15 ? "true" : "false";
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
      const r = await api("/api/eval/stats");
      if (!r.ok) return;
      const s = await r.json();
      $("evalWins").textContent = s.wins;
      $("evalLosses").textContent = s.losses;
      $("evalDraws").textContent = s.draws;
    } catch (_) {
      /* ignore */
    }
  }

  function showLobby() {
    state = "idle";
    stopEverything();
    $("evalLobby").classList.remove("hidden");
    $("evalDuel").classList.add("hidden");
    $("evalSearching").classList.add("hidden");
    $("evalFindBtn").classList.remove("hidden");
    $("evalLobbyError").classList.add("hidden");
    refreshStats();
  }

  function showError(msg) {
    state = "idle";
    $("evalSearching").classList.add("hidden");
    $("evalFindBtn").classList.remove("hidden");
    const e = $("evalLobbyError");
    if (e) {
      e.textContent = msg;
      e.classList.remove("hidden");
    }
  }

  function findMatch() {
    state = "searching";
    $("evalFindBtn").classList.add("hidden");
    $("evalSearching").classList.remove("hidden");
    $("evalLobbyError").classList.add("hidden");
    const poll = async () => {
      if (state !== "searching") return;
      try {
        const r = await api("/api/eval/match", { method: "POST" });
        if (r.status === 503) {
          const e = await r.json().catch(() => ({}));
          showError(e.detail || "No eval positions available.");
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
    api("/api/eval/leave", { method: "POST" }).catch(() => {});
    showLobby();
  }

  // ── Duel ──────────────────────────────────────────────────────────────── //
  function startDuel(d) {
    duel = d;
    $("evalLobby").classList.add("hidden");
    $("evalDuel").classList.remove("hidden");
    $("evalOppName").textContent = d.opponent || "Opponent";
    $("evalOppLabel").textContent = d.opponent || "Opponent";

    ensureBoard(d.fen);

    $("evalResult").classList.add("hidden");
    $("evalGuessBox").classList.remove("hidden");
    $("evalWaitOpp").classList.add("hidden");
    $("evalTimer").classList.remove("hidden");
    const slider = $("evalGuessSlider");
    const lockBtn = $("evalLockBtn");
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
      slider.value = "0.00";
      slider.disabled = false;
      $("evalGuessVal").textContent = "0.00";
      lockBtn.disabled = false;
      lockBtn.classList.remove("hidden");
    }
  }

  function afterLock(valueCp) {
    $("evalLockBtn").classList.add("hidden");
    $("evalGuessSlider").disabled = true;
    const wait = $("evalWaitOpp");
    wait.classList.remove("hidden");
    const lv = $("evalLockedVal");
    if (lv) lv.textContent = formatCp(valueCp);
  }

  async function lockGuess(force) {
    if (state !== "dueling") return;
    const guessCp = sliderToCp($("evalGuessSlider").value);
    state = "locked";
    afterLock(guessCp);
    try {
      const r = await api("/api/eval/guess", {
        method: "POST",
        body: JSON.stringify({ duel_id: duel.duel_id, guess: guessCp }),
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
    pollDuel();
  }

  function pollDuel() {
    if (pollTimer) clearTimeout(pollTimer);
    const poll = async () => {
      if (!duel) return;
      try {
        const r = await api(`/api/eval/duel/${duel.duel_id}`);
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
    $("evalGuessBox").classList.add("hidden");
    $("evalTimer").classList.add("hidden");
    const res = $("evalResult");
    res.classList.remove("hidden");

    const outcome = $("evalOutcome");
    const label = d.outcome === "win" ? "You win!" : d.outcome === "loss" ? "You lost" : "Draw";
    outcome.textContent = label;
    outcome.dataset.outcome = d.outcome || "draw";

    $("evalTrue").textContent = formatCp(d.true_eval_cp);
    $("evalYourGuess").textContent = formatCp(d.your_guess);
    $("evalYourPts").textContent = d.your_points != null ? `${d.your_points} pts` : "—";
    $("evalOppGuess").textContent = formatCp(d.opponent_guess);
    $("evalOppPts").textContent = d.opponent_points != null ? `${d.opponent_points} pts` : "—";
    $("evalOppLabel").textContent = d.opponent || "Opponent";
    refreshStats();
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────── //
  function stopEverything() {
    if (searchTimer) clearTimeout(searchTimer);
    if (pollTimer) clearTimeout(pollTimer);
    stopTimer();
  }

  function wire() {
    const on = (id, ev, fn) => {
      const el = $(id);
      if (el) el.addEventListener(ev, fn);
    };
    on("evalFindBtn", "click", findMatch);
    on("evalCancelBtn", "click", cancelSearch);
    on("evalAgainBtn", "click", showLobby);
    on("evalLockBtn", "click", () => lockGuess(false));
    const slider = $("evalGuessSlider");
    if (slider) {
      slider.addEventListener("input", () => {
        const cp = sliderToCp(slider.value);
        $("evalGuessVal").textContent = formatCp(cp);
      });
    }
  }

  window.__evalSetActive = function (isActive) {
    active = isActive;
    if (isActive) {
      if (state === "idle") showLobby();
      if (cg && duel && duel.fen) requestAnimationFrame(() => cg.set({ fen: duel.fen }));
    } else if (state === "searching") {
      cancelSearch();
    } else {
      stopEverything();
    }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
