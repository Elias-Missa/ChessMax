/* Game Review visual helpers ported from ChessReviewEngine assets. */
/* eslint-disable no-undef */
(function () {
  "use strict";

  const BADGE_BASE = "/static/vol/assets/review";

  const CLASSIFICATION_STYLES = {
    brilliant: { imageUrl: `${BADGE_BASE}/brilliant.png`, backgroundColor: "#1BACA6", label: "Brilliant" },
    great: { imageUrl: `${BADGE_BASE}/great.png`, backgroundColor: "#5C8BB0", label: "Great" },
    best: { imageUrl: `${BADGE_BASE}/best.png`, backgroundColor: "#7DB249", label: "Best" },
    excellent: { imageUrl: `${BADGE_BASE}/excellent.png`, backgroundColor: "#96BC4B", label: "Excellent" },
    good: { imageUrl: `${BADGE_BASE}/good.png`, backgroundColor: "#A4BA65", label: "Good" },
    book: { imageUrl: `${BADGE_BASE}/book.png`, backgroundColor: "#A88865", label: "Book" },
    inaccuracy: { imageUrl: `${BADGE_BASE}/inaccuracy.png`, backgroundColor: "#E3AF35", label: "Inaccuracy" },
    mistake: { imageUrl: `${BADGE_BASE}/mistake.png`, backgroundColor: "#CA6830", label: "Mistake" },
    miss: { imageUrl: `${BADGE_BASE}/miss.png`, backgroundColor: "#FF7769", label: "Miss" },
    blunder: { imageUrl: `${BADGE_BASE}/blunder.png`, backgroundColor: "#B33430", label: "Blunder" },
  };

  function getClassificationStyle(kind) {
    return CLASSIFICATION_STYLES[kind] || null;
  }

  /** Arrow tint for the engine suggestion while reviewing a classified move. */
  function arrowColorForClassification(kind) {
    switch (kind) {
      case "brilliant":
      case "best":
      case "excellent":
        return "rgba(129, 182, 76, 0.85)";
      case "great":
      case "good":
      case "book":
        return "rgba(92, 139, 176, 0.85)";
      case "inaccuracy":
      case "mistake":
      case "miss":
      case "blunder":
        return "rgba(202, 52, 49, 0.85)";
      default:
        return null;
    }
  }

  function squareToOverlayPosition(square, orientation) {
    if (!square || square.length < 2) return null;
    const file = square.charCodeAt(0) - 97;
    const rank = Number(square[1]) - 1;
    if (file < 0 || file > 7 || rank < 0 || rank > 7) return null;
    const left = orientation === "black" ? (7 - file) * 12.5 : file * 12.5;
    const top = orientation === "black" ? rank * 12.5 : (7 - rank) * 12.5;
    return { top: `${top}%`, left: `${left}%` };
  }

  function renderBadgeIcon(kind, className) {
    const style = getClassificationStyle(kind);
    if (!style) return null;
    const img = document.createElement("img");
    img.className = className || "review-badge-icon";
    img.src = style.imageUrl;
    img.alt = style.label;
    img.title = style.label;
    img.draggable = false;
    return img;
  }

  function renderBoardOverlay(overlayEl, classification, targetSquare, orientation) {
    if (!overlayEl) return;
    overlayEl.innerHTML = "";
    const style = getClassificationStyle(classification);
    const position = squareToOverlayPosition(targetSquare, orientation || "white");
    if (!style || !position) {
      overlayEl.classList.add("hidden");
      return;
    }
    const cell = document.createElement("div");
    cell.className = "review-board-overlay-cell";
    cell.style.top = position.top;
    cell.style.left = position.left;
    const img = document.createElement("img");
    img.src = style.imageUrl;
    img.alt = style.label;
    img.draggable = false;
    cell.appendChild(img);
    overlayEl.appendChild(cell);
    overlayEl.classList.remove("hidden");
  }

  function clearBoardOverlay(overlayEl) {
    if (!overlayEl) return;
    overlayEl.innerHTML = "";
    overlayEl.classList.add("hidden");
  }

  const PILL_ORDER = [
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
  ];

  function renderClassificationPills(container, counts) {
    if (!container) return;
    container.innerHTML = "";
    const data = counts || {};
    let any = false;
    for (const kind of PILL_ORDER) {
      const n = data[kind] || 0;
      if (!n) continue;
      any = true;
      const style = getClassificationStyle(kind);
      const pill = document.createElement("span");
      pill.className = "review-class-pill";
      pill.dataset.kind = kind;
      if (style) pill.style.setProperty("--pill-color", style.backgroundColor);
      const img = renderBadgeIcon(kind, "review-badge-icon review-badge-icon--pill");
      if (img) pill.appendChild(img);
      const count = document.createElement("strong");
      count.textContent = String(n);
      pill.appendChild(count);
      const label = document.createElement("span");
      label.textContent = style ? style.label : kind;
      pill.appendChild(label);
      container.appendChild(pill);
    }
    if (!any) {
      const empty = document.createElement("span");
      empty.className = "review-class-empty";
      empty.textContent = "—";
      container.appendChild(empty);
    }
  }

  const FIND_BAND_COLORS = {
    Obvious: "#7DB249",
    Natural: "#96BC4B",
    "Needs thought": "#E3AF35",
    Hard: "#CA6830",
    "Engine-only": "#B33430",
    Forced: "#5C8BB0",
  };

  const SVG_NS = "http://www.w3.org/2000/svg";

  /** Inline SVG sparkline of C_A(rating) — how the chance of finding a good
   *  move rises with player strength, with a 50% reference line. */
  function buildFindSparkline(curve, color) {
    const wrap = document.createElement("div");
    wrap.className = "review-find-spark";
    const W = 240;
    const H = 46;
    const pad = 5;
    const ratings = curve.map((p) => p[0]);
    const rMin = Math.min.apply(null, ratings);
    const rMax = Math.max.apply(null, ratings);
    const px = (r) => (rMax === rMin ? pad : pad + ((r - rMin) / (rMax - rMin)) * (W - 2 * pad));
    const py = (v) => H - pad - Math.max(0, Math.min(1, v)) * (H - 2 * pad);
    const line = curve.map((p) => `${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join(" ");
    const area = `${pad},${H - pad} ${line} ${(W - pad).toFixed(1)},${H - pad}`;

    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("class", "review-find-spark-svg");
    svg.setAttribute("preserveAspectRatio", "none");

    const guide = document.createElementNS(SVG_NS, "line");
    guide.setAttribute("x1", String(pad));
    guide.setAttribute("x2", String(W - pad));
    guide.setAttribute("y1", py(0.5).toFixed(1));
    guide.setAttribute("y2", py(0.5).toFixed(1));
    guide.setAttribute("class", "review-find-spark-guide");
    svg.appendChild(guide);

    const fill = document.createElementNS(SVG_NS, "polygon");
    fill.setAttribute("points", area);
    fill.setAttribute("fill", color);
    fill.setAttribute("fill-opacity", "0.16");
    svg.appendChild(fill);

    const stroke = document.createElementNS(SVG_NS, "polyline");
    stroke.setAttribute("points", line);
    stroke.setAttribute("fill", "none");
    stroke.setAttribute("stroke", color);
    stroke.setAttribute("stroke-width", "2");
    stroke.setAttribute("stroke-linejoin", "round");
    svg.appendChild(stroke);

    wrap.appendChild(svg);
    const cap = document.createElement("div");
    cap.className = "review-find-spark-cap";
    cap.innerHTML = `<span>${rMin}</span><span>chance of finding it &rarr;</span><span>${rMax}</span>`;
    wrap.appendChild(cap);
    return wrap;
  }

  /**
   * Render the findability panel (Game Review 2.0 §3) into ``panelEl``.
   * Null-safe: hides the panel when ``findability`` is absent (position gated
   * out, or no human model installed). Shows the band, a score meter, a
   * rising-probability sparkline (the ``curve``), the personal likelihood, and
   * the realistic alternative move.
   */
  function renderFindability(panelEl, findability, bestSan) {
    if (!panelEl) return;
    panelEl.innerHTML = "";
    if (!findability) {
      panelEl.classList.add("hidden");
      return;
    }
    panelEl.classList.remove("hidden");
    const band = findability.band || "";
    const bandColor = FIND_BAND_COLORS[band] || "#5C8BB0";
    const score = typeof findability.score === "number" ? findability.score : null;

    // Best-move header — makes explicit that this whole panel describes how hard
    // it is to FIND THE BEST MOVE (not the move that was actually played).
    if (bestSan) {
      const bm = document.createElement("div");
      bm.className = "review-find-bestmove";
      const bmLabel = document.createElement("span");
      bmLabel.className = "review-find-bestmove-label";
      bmLabel.textContent = "Best move to find";
      const bmMove = document.createElement("strong");
      bmMove.className = "review-find-bestmove-san";
      bmMove.textContent = `→ ${bestSan}`;
      bm.appendChild(bmLabel);
      bm.appendChild(bmMove);
      panelEl.appendChild(bm);
    }

    // Header: label + band verdict.
    const head = document.createElement("div");
    head.className = "review-find-head";
    const title = document.createElement("span");
    title.className = "review-find-title";
    title.textContent = "How hard to find";
    head.appendChild(title);
    const bandEl = document.createElement("span");
    bandEl.className = "review-find-band";
    bandEl.style.color = bandColor;
    bandEl.textContent = band;
    head.appendChild(bandEl);
    panelEl.appendChild(head);

    // Score meter (0–100), filled and colored by band.
    if (score != null) {
      const meter = document.createElement("div");
      meter.className = "review-find-meter";
      const fillEl = document.createElement("div");
      fillEl.className = "review-find-meter-fill";
      fillEl.style.width = `${Math.max(0, Math.min(100, score))}%`;
      fillEl.style.background = bandColor;
      meter.appendChild(fillEl);
      const val = document.createElement("span");
      val.className = "review-find-meter-val";
      val.textContent = String(score);
      meter.appendChild(val);
      panelEl.appendChild(meter);
    }

    // Rising-probability sparkline from the C_A curve.
    const curve = Array.isArray(findability.curve) ? findability.curve : [];
    if (curve.length >= 2) {
      panelEl.appendChild(buildFindSparkline(curve, bandColor));
    }

    // Personal likelihood (needs the user's rating) or a strength hint.
    const desc = document.createElement("div");
    desc.className = "review-find-desc";
    if (typeof findability.personal === "number") {
      desc.textContent = `At your rating, you'd find this move ${Math.round(findability.personal * 100)}% of the time.`;
    } else if (findability.r_find != null) {
      desc.textContent =
        findability.r_find <= 850
          ? "Most players would find this move."
          : `Takes about ${findability.r_find}-strength to find reliably.`;
    }
    if (desc.textContent) panelEl.appendChild(desc);

    // The strongest move a human at this level would more plausibly have played.
    if (findability.alternate && findability.alternate.san) {
      const alt = document.createElement("div");
      alt.className = "review-find-alt";
      const label = document.createElement("span");
      label.textContent = "You'd more likely find:";
      const move = document.createElement("strong");
      move.textContent = findability.alternate.san;
      alt.appendChild(label);
      alt.appendChild(move);
      panelEl.appendChild(alt);
    }
  }

  window.ChessReviewUI = {
    CLASSIFICATION_STYLES,
    FIND_BAND_COLORS,
    arrowColorForClassification,
    clearBoardOverlay,
    getClassificationStyle,
    renderBadgeIcon,
    renderBoardOverlay,
    renderClassificationPills,
    renderFindability,
    squareToOverlayPosition,
  };
})();
