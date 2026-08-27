// Dev tab — manual calibration labelling over every stored review position.
//
// An internal harness, not a product surface. It pages through `review_moves`
// (every ply of every completed review this account owns), draws the position,
// shows the volatility and findability we stored, and records a verdict on each
// so `chess_vol/config.py` and `core/constants/findability.json` can be refit
// against human judgement.
//
// Two invariants worth keeping in mind while editing:
//   * Findability scores the BEST move, never the move played. The panel says
//     so and the purple arrow points at that move — mislabel this and every
//     verdict collected here is about the wrong move.
//   * Ordering is server-side and stable (review created_at, review_id, ply).
//     A labelling pass that reshuffles between pages is unusable, so the client
//     addresses positions by absolute index and caches pages, never by
//     re-sorting what it has.
/* eslint-disable no-undef */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const api = (path, opts) =>
    fetch(
      path,
      Object.assign(
        { headers: { "Content-Type": "application/json" }, credentials: "same-origin" },
        opts || {},
      ),
    );

  const PAGE = 50;
  // Same purple the review board uses for the best-move arrow, so "the engine's
  // move" reads identically in both places.
  const BEST_COLOR = "purple";
  const PLAYED_COLOR = "blue";

  const VOL_KEYS = { 1: "way_lower", 2: "lower", 3: "about_right", 4: "higher", 5: "way_higher" };
  const FIND_KEYS = { q: "way_harder", w: "harder", e: "about_right", r: "easier", t: "way_easier" };

  let active = false;
  let cg = null;
  let index = 0;
  let total = 0;
  let scope = "all";
  let require = "any";
  const pages = new Map(); // page number → positions[]
  let current = null;
  let saveTimer = null;
  let booted = false;

  // ── Data ──────────────────────────────────────────────────────────────── //

  function resetPages() {
    pages.clear();
    current = null;
  }

  async function fetchPage(page) {
    if (pages.has(page)) return pages.get(page);
    const url =
      `/api/dev/positions?scope=${scope}&require=${require}` +
      `&limit=${PAGE}&offset=${page * PAGE}`;
    const res = await api(url);
    if (!res.ok) throw new Error(`positions ${res.status}`);
    const data = await res.json();
    total = data.total || 0;
    pages.set(page, data.positions || []);
    return pages.get(page);
  }

  async function positionAt(i) {
    if (i < 0) return null;
    const page = Math.floor(i / PAGE);
    const items = await fetchPage(page);
    return items[i - page * PAGE] || null;
  }

  // ── Formatting ────────────────────────────────────────────────────────── //

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function moveNumber(ply) {
    const n = Math.ceil(ply / 2);
    return ply % 2 === 1 ? `${n}.` : `${n}…`;
  }

  function formatCp(cp) {
    if (cp == null) return "—";
    const sign = cp > 0 ? "+" : cp < 0 ? "−" : "";
    return `${sign}${(Math.abs(cp) / 100).toFixed(2)}`;
  }

  function turnFromFen(fen) {
    if (!fen) return "white";
    return fen.split(" ")[1] === "b" ? "black" : "white";
  }

  function uciToShape(uci, brush) {
    if (!uci || uci.length < 4) return null;
    return { orig: uci.slice(0, 2), dest: uci.slice(2, 4), brush };
  }

  // ── Board ─────────────────────────────────────────────────────────────── //

  function drawBoard(pos) {
    const el = $("dvBoard");
    if (!el || typeof Chessground === "undefined") return;
    const fen = pos && pos.fen ? pos.fen : "start";
    const orientation = turnFromFen(pos && pos.fen);
    // The best-move arrow is drawn last so it sits on top when both moves share
    // a square, which is the common case on a recapture.
    const shapes = [
      uciToShape(pos && pos.move_uci, PLAYED_COLOR),
      uciToShape(pos && pos.best_uci, BEST_COLOR),
    ].filter(Boolean);

    if (cg) {
      cg.set({ fen, orientation, drawable: { autoShapes: shapes } });
    } else {
      cg = Chessground(el, {
        viewOnly: true,
        coordinates: true,
        fen,
        orientation,
        animation: { enabled: false },
        drawable: { enabled: false, visible: true, autoShapes: shapes },
      });
    }
  }

  // ── Render ────────────────────────────────────────────────────────────── //

  function setChoice(groupId, label) {
    const group = $(groupId);
    if (!group) return;
    group.querySelectorAll("button[data-label]").forEach((btn) => {
      const on = btn.dataset.label === label;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function renderMeter(id, value, max) {
    const el = $(id);
    if (!el) return;
    const pct = value == null ? 0 : Math.max(0, Math.min(100, (value / max) * 100));
    el.style.width = `${pct}%`;
    el.dataset.empty = value == null ? "true" : "false";
  }

  function renderEmpty(show, text) {
    const empty = $("dvEmpty");
    const content = $("dvContent");
    if (empty) {
      empty.classList.toggle("hidden", !show);
      if (show && text) $("dvEmptyText").textContent = text;
    }
    if (content) content.classList.toggle("hidden", show);
  }

  function render(pos) {
    current = pos;
    updateCounter();
    if (!pos) {
      renderEmpty(
        true,
        total === 0
          ? "Run a game review first — this tab walks the positions those reviews stored."
          : "No positions match this filter.",
      );
      drawBoard(null);
      return;
    }
    renderEmpty(false);
    drawBoard(pos);

    const white = escapeHtml(pos.white_name || "White");
    const black = escapeHtml(pos.black_name || "Black");
    const played = pos.played_at ? ` · ${escapeHtml(pos.played_at)}` : "";
    $("dvGame").innerHTML =
      `${white} <span class="dv-vs">vs</span> ${black}` +
      `<span class="dv-dim">${played}${pos.opening_name ? ` · ${escapeHtml(pos.opening_name)}` : ""}</span>`;

    $("dvMoveNum").textContent = moveNumber(pos.ply);
    $("dvSan").textContent = pos.san || "";
    $("dvTurn").textContent = `${turnFromFen(pos.fen) === "white" ? "White" : "Black"} to move`;

    const cls = $("dvClass");
    cls.textContent = pos.classification || "";
    cls.classList.toggle("hidden", !pos.classification);
    cls.dataset.kind = pos.classification || "";
    $("dvPhase").textContent = pos.phase || "";
    $("dvTier").textContent = pos.depth_tier || "";

    const bits = [];
    if (pos.eval_cp != null) bits.push(`eval ${formatCp(pos.eval_cp)}`);
    if (pos.win_prob != null) bits.push(`win% ${Math.round(pos.win_prob * 100)}`);
    if (pos.delta_w != null) bits.push(`Δw ${pos.delta_w.toFixed(1)}`);
    bits.push(pos.is_user_move ? "your move" : "opponent");
    $("dvSubstats").textContent = bits.join(" · ");

    // Volatility (0–100 by construction — see core/volatility.py).
    const vol = pos.volatility;
    $("dvVolScore").textContent = vol == null ? "—" : vol.toFixed(1);
    renderMeter("dvVolMeter", vol, 100);
    $("dvVolSection").classList.toggle("dv-metric--absent", vol == null);
    setChoice("dvVolChoices", pos.volatility_label);

    const find = pos.findability;
    const band = pos.findability_band ? ` · ${pos.findability_band}` : "";
    $("dvFindScore").textContent = find == null ? "—" : `${find}${band}`;
    renderMeter("dvFindMeter", find, 100);
    $("dvBestMove").textContent = pos.best_san || pos.best_uci || "best move";
    $("dvNoFind").classList.toggle("hidden", find != null);
    $("dvFindSection").classList.toggle("dv-metric--absent", find == null);
    setChoice("dvFindChoices", pos.findability_label);

    const lines = $("dvLines");
    lines.innerHTML = (pos.top_lines || [])
      .map((line, i) => {
        const isBest = i === 0;
        const isPlayed = line.uci && line.uci === pos.move_uci;
        const tags = [isBest ? "best" : "", isPlayed ? "played" : ""]
          .filter(Boolean)
          .map((t) => `<span class="dv-linetag dv-linetag--${t}">${t}</span>`)
          .join("");
        return (
          `<li${isBest ? ' class="dv-line--best"' : ""}>` +
          `<span class="dv-linesan">${escapeHtml(line.san || line.uci || "?")}</span>` +
          `<span class="dv-linecp">${formatCp(line.eval_cp)}</span>${tags}</li>`
        );
      })
      .join("");

    $("dvNote").value = pos.note || "";
    $("dvSaved").textContent = pos.labeled_at ? "Saved" : "";
  }

  function updateCounter() {
    $("dvCounter").textContent = total ? `${index + 1} / ${total}` : "0 / 0";
    const jump = $("dvJump");
    if (jump && document.activeElement !== jump) {
      jump.value = total ? index + 1 : "";
      jump.max = String(total);
    }
  }

  async function show(i) {
    if (total > 0) index = Math.max(0, Math.min(total - 1, i));
    else index = 0;
    const pos = await positionAt(index).catch(() => null);
    render(pos);
    // Warm the next page so ← / → held down never stalls on a fetch.
    const nextPage = Math.floor((index + 5) / PAGE);
    if (!pages.has(nextPage)) fetchPage(nextPage).catch(() => {});
  }

  // ── Saving ────────────────────────────────────────────────────────────── //

  async function save({ silent = false } = {}) {
    if (!current) return;
    const body = {
      review_id: current.review_id,
      ply: current.ply,
      volatility_label: current.volatility_label || null,
      findability_label: current.findability_label || null,
      note: $("dvNote").value || null,
    };
    const saved = $("dvSaved");
    try {
      const res = await api("/api/dev/label", { method: "POST", body: JSON.stringify(body) });
      if (!res.ok) throw new Error(`label ${res.status}`);
      const data = await res.json();
      current.note = data.note;
      current.labeled_at = data.cleared ? null : new Date().toISOString();
      if (!silent && saved) {
        saved.textContent = data.cleared ? "Cleared" : "Saved";
        saved.classList.add("dv-saved--flash");
        setTimeout(() => saved.classList.remove("dv-saved--flash"), 400);
      }
      refreshProgress();
    } catch (err) {
      if (saved) saved.textContent = "Save failed";
    }
  }

  // Clicking the active verdict again takes it back off, so a mis-key is one
  // keystroke to undo rather than a wrong row in the export.
  function pick(kind, label) {
    if (!current) return;
    const field = kind === "vol" ? "volatility_label" : "findability_label";
    current[field] = current[field] === label ? null : label;
    setChoice(kind === "vol" ? "dvVolChoices" : "dvFindChoices", current[field]);
    save();
  }

  async function refreshProgress() {
    try {
      const res = await api("/api/dev/stats");
      if (!res.ok) return;
      const s = await res.json();
      $("dvProgress").textContent =
        `${s.labeled_positions} labelled · ${s.volatility_total} vol · ${s.findability_total} find`;
    } catch (err) {
      /* progress is decoration; never block labelling on it */
    }
  }

  async function exportLabels() {
    try {
      const res = await api("/api/dev/export");
      if (!res.ok) throw new Error(`export ${res.status}`);
      const data = await res.json();
      const blob = new Blob([JSON.stringify(data.labels, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `chessmax-calibration-labels-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      $("dvProgress").textContent = "Export failed";
    }
  }


  // ── Piece values lab ──────────────────────────────────────────────────── //
  //
  // Runs ~30 engine searches per position, so it is a bench you press a button
  // on, never something that fires as you scrub. Two things about the display
  // are load-bearing:
  //
  //   * The number on a square is the piece's contextual value; the COLOUR is
  //     its premium over the static table. The premium is the insight — "your
  //     bishop is worth 0.8, not 3" — and the value alone does not carry it.
  //   * `anchored` explains (the board sums to the evaluation) and `shrunk`
  //     predicts (it is damped toward the static value because the measurement
  //     is noisy). Showing only one of them would misrepresent the other.

  let mode = "label";
  let valuesData = null;

  const PIECE_NAMES = { 1: "Pawn", 2: "Knight", 3: "Bishop", 4: "Rook", 5: "Queen" };

  function pawns(cp) {
    return (cp / 100).toFixed(2);
  }

  function signedPawns(cp) {
    const v = cp / 100;
    return `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}`;
  }

  /** Square -> {top,left} in 12.5% steps. Percentages track the board on
   *  resize, so this needs no pixel measuring and no resize listener. */
  function squareToPercent(square, orientation) {
    const file = square.charCodeAt(0) - 97;
    const rank = parseInt(square[1], 10) - 1;
    const flip = orientation === "black";
    const col = flip ? 7 - file : file;
    const row = flip ? rank : 7 - rank;
    return { left: `${col * 12.5}%`, top: `${row * 12.5}%` };
  }

  /** Premium -> colour. Green = worth more than the book says, red = less. */
  function premiumColor(premiumCp) {
    const t = Math.max(-1, Math.min(1, premiumCp / 300));
    if (t >= 0) return `rgba(80, 220, 90, ${0.25 + 0.6 * t})`;
    return `rgba(235, 90, 90, ${0.25 + 0.6 * -t})`;
  }

  function clearValueOverlay() {
    const el = $("dvValueOverlay");
    if (!el) return;
    el.innerHTML = "";
    el.classList.add("hidden");
  }

  function renderValueOverlay(data) {
    const el = $("dvValueOverlay");
    if (!el || !data) return;
    const orientation = turnFromFen(data.fen);
    el.innerHTML = data.pieces
      .map((p) => {
        const pos = squareToPercent(p.square, orientation);
        return (
          `<div class="dv-vcell" style="top:${pos.top};left:${pos.left}">` +
          `<span class="dv-vchip" style="background:${premiumColor(p.premium_cp)}">` +
          `${pawns(p.anchored_cp)}</span></div>`
        );
      })
      .join("");
    el.classList.remove("hidden");
  }

  function renderValues(data) {
    valuesData = data;
    $("dvValuesResult").classList.remove("hidden");

    $("dvRecon").innerHTML =
      `<div><b>${signedPawns(data.material_cp)}</b><span>Material</span></div>` +
      `<div><b>${signedPawns(data.gap_cp)}</b><span>Gap</span></div>` +
      `<div><b>${signedPawns(data.eval_material_cp)}</b><span>Evaluation</span></div>`;

    $("dvSaturated").classList.toggle("hidden", !data.saturated);

    // Sorted by |premium| so the piece that explains the gap is row one.
    const rows = data.pieces
      .slice()
      .sort((a, b) => Math.abs(b.premium_cp) - Math.abs(a.premium_cp));

    $("dvValuesRows").innerHTML = rows
      .map((p) => {
        const why = (p.tags || []).join(", ");
        const reloc = p.relocation
          ? `<span class="dv-reloc">&rarr; ${escapeHtml(p.relocation.to_square)} ` +
            `(${signedPawns(p.relocation.gain_cp)})</span>`
          : "";
        const sign = p.premium_cp >= 0 ? "up" : "down";
        return (
          `<tr><td><span class="dv-vpiece" data-color="${p.color}">` +
          `${escapeHtml(p.symbol)}${escapeHtml(p.square)}</span> ` +
          `<span class="dv-vwhy">${escapeHtml(PIECE_NAMES[p.piece_type] || "")}</span></td>` +
          `<td>${pawns(p.static_cp)}</td>` +
          `<td><b>${pawns(p.anchored_cp)}</b></td>` +
          `<td class="dv-prem" data-sign="${sign}">${signedPawns(p.premium_cp)}</td>` +
          `<td>${pawns(p.shrunk_cp)}</td>` +
          `<td class="dv-vwhy">${escapeHtml(why)} ${reloc}</td></tr>`
        );
      })
      .join("");

    $("dvValuesFoot").innerHTML =
      `${data.analyses} engine searches at depth ${data.depth} in ` +
      `${(data.elapsed_ms / 1000).toFixed(1)}s. ` +
      `<b>Value</b> reconciles to the evaluation and is what explains the gap; ` +
      `<b>Predict</b> is the same number shrunk ${Math.round(data.shrinkage * 100)}% ` +
      `toward the static table, which is what beat 1/3/3/5/9 at predicting real trades. ` +
      `Residual before anchoring: ${signedPawns(data.residual_cp)}.`;

    // Draw the position the values were measured on, then the overlay.
    drawBoard({ fen: data.fen });
    renderValueOverlay(data);
  }

  /** FastAPI reports 422 as a list of error objects and everything else as a
   *  string, so a bare `body.detail` renders "[object Object]" on the one path
   *  a user hits most: a malformed FEN. */
  function detailText(body, statusCode) {
    const detail = body && body.detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
      return detail
        .map((d) => (d && d.msg ? d.msg : JSON.stringify(d)))
        .join("; ");
    }
    return `Failed (${statusCode})`;
  }

  async function runValues() {
    const fen = ($("dvFen").value || "").trim();
    const status = $("dvValuesStatus");
    if (!fen) {
      status.textContent = "Paste a FEN, or press “From board”.";
      status.dataset.error = "true";
      return;
    }
    const btn = $("dvRunValues");
    btn.disabled = true;
    status.dataset.error = "false";
    status.textContent = "Measuring — one engine search per piece…";
    try {
      const res = await api("/api/dev/piece-values", {
        method: "POST",
        body: JSON.stringify({
          fen,
          depth: parseInt($("dvDepth").value, 10),
          relocate_top_k: $("dvRelocate").checked ? 2 : 0,
        }),
      });
      const body = await res.json();
      if (!res.ok) {
        status.dataset.error = "true";
        status.textContent = detailText(body, res.status);
        $("dvValuesResult").classList.add("hidden");
        clearValueOverlay();
        return;
      }
      status.textContent = "";
      renderValues(body);
    } catch (err) {
      status.dataset.error = "true";
      status.textContent = "Request failed.";
    } finally {
      btn.disabled = false;
    }
  }

  function setMode(next) {
    mode = next;
    const values = mode === "values";
    $("dvValuesPanel").classList.toggle("hidden", !values);
    document.querySelector("#dev-root .dv-panel:not(.dv-values)")
      .classList.toggle("hidden", values);
    $("dvCounter").classList.toggle("hidden", values);
    document.querySelector("#dev-root .dv-bar-mid").classList.toggle("hidden", values);
    document.querySelector("#dev-root .dv-nav").classList.toggle("hidden", values);
    document.querySelector("#dev-root .dv-legend").classList.toggle("hidden", values);
    $("dvValueLegend").classList.toggle("hidden", !values);
    // The labelling progress counter and export belong to the other mode.
    document.querySelector("#dev-root .dv-bar-right").classList.toggle("dv-quiet", values);
    document.querySelectorAll("#dev-root .dv-modes button").forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (values) {
      if (valuesData) renderValueOverlay(valuesData);
      // Seed the FEN box from whatever position the labeller was showing.
      if (!$("dvFen").value && current && current.fen) $("dvFen").value = current.fen;
    } else {
      clearValueOverlay();
      render(current);
    }
  }

  // ── Wiring ────────────────────────────────────────────────────────────── //

  function onKey(event) {
    if (!active || mode === "values") return;
    const tag = (event.target && event.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    const key = event.key.toLowerCase();
    if (event.key === "ArrowRight") { event.preventDefault(); show(index + 1); return; }
    if (event.key === "ArrowLeft") { event.preventDefault(); show(index - 1); return; }
    if (VOL_KEYS[key]) { event.preventDefault(); pick("vol", VOL_KEYS[key]); return; }
    if (FIND_KEYS[key]) { event.preventDefault(); pick("find", FIND_KEYS[key]); }
  }

  function boot() {
    if (booted) return;
    booted = true;

    $("dvVolChoices").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-label]");
      if (btn) pick("vol", btn.dataset.label);
    });
    $("dvFindChoices").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-label]");
      if (btn) pick("find", btn.dataset.label);
    });
    $("dvPrev").addEventListener("click", () => show(index - 1));
    $("dvNext").addEventListener("click", () => show(index + 1));
    $("dvJump").addEventListener("change", (e) => {
      const n = parseInt(e.target.value, 10);
      if (!Number.isNaN(n)) show(n - 1);
    });
    $("dvClear").addEventListener("click", () => {
      if (!current) return;
      current.volatility_label = null;
      current.findability_label = null;
      $("dvNote").value = "";
      setChoice("dvVolChoices", null);
      setChoice("dvFindChoices", null);
      save();
    });
    $("dvNote").addEventListener("input", () => {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => save({ silent: true }), 600);
    });
    $("dvExportBtn").addEventListener("click", exportLabels);

    document.querySelectorAll("#dev-root .dv-modes button").forEach((b) => {
      b.addEventListener("click", () => setMode(b.dataset.mode));
    });
    $("dvRunValues").addEventListener("click", runValues);
    $("dvFromBoard").addEventListener("click", () => {
      if (current && current.fen) $("dvFen").value = current.fen;
    });

    // A filter change re-indexes everything, so drop the page cache and restart
    // at the top rather than keeping a now-meaningless offset.
    const onFilter = () => {
      scope = $("dvScope").value;
      require = $("dvRequire").value;
      resetPages();
      show(0);
    };
    $("dvScope").addEventListener("change", onFilter);
    $("dvRequire").addEventListener("change", onFilter);

    document.addEventListener("keydown", onKey);
  }

  window.__devSetActive = function (isActive) {
    active = !!isActive;
    if (!active) return;
    boot();
    // Reviews may have been added since the last visit; the cache is only good
    // for one sitting.
    resetPages();
    refreshProgress();
    show(index).then(() => {
      // Chessground measures on insert; the root was `hidden` until now.
      window.dispatchEvent(new Event("resize"));
    });
  };
})();
