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

  window.ChessReviewUI = {
    CLASSIFICATION_STYLES,
    arrowColorForClassification,
    clearBoardOverlay,
    getClassificationStyle,
    renderBadgeIcon,
    renderBoardOverlay,
    renderClassificationPills,
    squareToOverlayPosition,
  };
})();
