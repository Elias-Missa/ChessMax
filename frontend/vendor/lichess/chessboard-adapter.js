/* chessboard.js v1 → Chessground adapter shim.
 *
 * Exposes window.Chessboard(idOrEl, options) returning an object with the
 * subset of the chessboard.js API the app uses: position(), fen(), flip(),
 * resize(), clear(), orientation(), destroy(). Internally backed by
 * Chessground, so the board looks/feels like lichess.org without rewriting
 * the 2600-line app.js controller.
 *
 * Spare pieces (sparePieces:true) are rendered as two HTML rows above and
 * below the board; HTML5 drag-and-drop translates a drop coordinate into a
 * board square and inserts the piece via cg.set({ fen }).
 */
(function () {
  "use strict";

  if (typeof window === "undefined") return;
  if (!window.Chessground) {
    console.error("[chessboard-adapter] Chessground bundle not loaded");
    return;
  }

  const STARTING_PLACEMENT = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR";
  const EMPTY_PLACEMENT = "8/8/8/8/8/8/8/8";
  const FILES = ["a", "b", "c", "d", "e", "f", "g", "h"];
  const PIECE_LETTERS = ["k", "q", "r", "b", "n", "p"];

  // ── FEN ↔ position-object helpers ─────────────────────────────────────── //
  // chessboard.js represents positions as { e2: "wP", g1: "wN", ... }.
  // Empty squares are simply absent from the object.

  function fenToPos(placement) {
    const out = {};
    if (!placement) return out;
    const rows = placement.split("/");
    if (rows.length !== 8) return out;
    for (let r = 0; r < 8; r++) {
      const rank = 8 - r;
      let f = 0;
      for (const ch of rows[r]) {
        if (ch >= "1" && ch <= "8") {
          f += ch.charCodeAt(0) - 48;
        } else {
          if (f >= 8) break;
          const color = ch === ch.toUpperCase() ? "w" : "b";
          out[FILES[f] + rank] = color + ch.toUpperCase();
          f++;
        }
      }
    }
    return out;
  }

  function posToFen(pos) {
    const rows = [];
    for (let r = 8; r >= 1; r--) {
      let row = "";
      let blanks = 0;
      for (let f = 0; f < 8; f++) {
        const sq = FILES[f] + r;
        const piece = pos[sq];
        if (!piece) {
          blanks++;
          continue;
        }
        if (blanks) {
          row += String(blanks);
          blanks = 0;
        }
        const letter = piece[1];
        row += piece[0] === "w" ? letter.toUpperCase() : letter.toLowerCase();
      }
      if (blanks) row += String(blanks);
      rows.push(row);
    }
    return rows.join("/");
  }

  // ── Spare-piece tray ──────────────────────────────────────────────────── //

  // Cburnett SVG URLs are defined inside the bundled chessground.css under
  // selectors like `.cg-wrap piece.pawn.white`. We pull them out of the loaded
  // stylesheet once and reuse them for the spare tray (which lives outside
  // any `.cg-wrap`, so the original selectors don't apply).
  const _pieceUrlCache = {};
  function getPieceImageUrl(color, role) {
    const key = `${color}.${role}`;
    if (key in _pieceUrlCache) return _pieceUrlCache[key];
    const wantSelector = `.cg-wrap piece.${role}.${color}`;
    let found = null;
    for (const sheet of document.styleSheets) {
      let rules;
      try { rules = sheet.cssRules; } catch (_) { continue; }
      if (!rules) continue;
      for (const rule of rules) {
        if (rule.type !== 1) continue; // CSSStyleRule
        if (rule.selectorText === wantSelector) {
          const bg = rule.style && rule.style.backgroundImage;
          if (bg) {
            const m = bg.match(/url\(\s*(['"]?)([^'")]+)\1\s*\)/);
            if (m) { found = m[2]; break; }
          }
        }
      }
      if (found) break;
    }
    _pieceUrlCache[key] = found;
    return found;
  }

  function makeSpareRow(color) {
    // color: "w" or "b"
    const row = document.createElement("div");
    row.className = "cb-spare-row";
    row.dataset.color = color;
    for (const letter of PIECE_LETTERS) {
      const cell = document.createElement("div");
      cell.className = "cb-spare-piece";
      cell.draggable = true;
      const role = roleFromLetter(letter);
      const colorName = color === "w" ? "white" : "black";
      const piece = document.createElement("piece");
      piece.className = `${colorName} ${role}`;
      const url = getPieceImageUrl(colorName, role);
      if (url) piece.style.backgroundImage = `url("${url}")`;
      cell.appendChild(piece);
      cell.dataset.piece = (color === "w" ? "w" : "b") + letter.toUpperCase();
      row.appendChild(cell);
    }
    return row;
  }

  function roleFromLetter(letter) {
    switch (letter) {
      case "k": return "king";
      case "q": return "queen";
      case "r": return "rook";
      case "b": return "bishop";
      case "n": return "knight";
      case "p": return "pawn";
      default: return "pawn";
    }
  }

  // Compute the chessground square key under (clientX, clientY), or null.
  function squareUnderCursor(cgState, boardEl, clientX, clientY) {
    const rect = boardEl.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) return null;
    const file = Math.min(7, Math.max(0, Math.floor((x / rect.width) * 8)));
    const rankFromTop = Math.min(7, Math.max(0, Math.floor((y / rect.height) * 8)));
    const orientation = (cgState && cgState.orientation) || "white";
    const realFile = orientation === "white" ? file : 7 - file;
    const realRank = orientation === "white" ? 8 - rankFromTop : rankFromTop + 1;
    return FILES[realFile] + realRank;
  }

  // ── Adapter constructor ───────────────────────────────────────────────── //

  function Chessboard(elOrId, opts) {
    const host = typeof elOrId === "string" ? document.getElementById(elOrId) : elOrId;
    if (!host) {
      console.error("[chessboard-adapter] target element not found:", elOrId);
      return null;
    }
    opts = opts || {};
    host.innerHTML = "";
    host.classList.add("cb-host");
    if (opts.sparePieces) host.classList.add("cb-host-spares");

    let spareTop = null;
    let spareBottom = null;
    if (opts.sparePieces) {
      spareTop = makeSpareRow("b");
      host.appendChild(spareTop);
    }

    const boardWrap = document.createElement("div");
    boardWrap.className = "cb-board-wrap";
    host.appendChild(boardWrap);

    if (opts.sparePieces) {
      spareBottom = makeSpareRow("w");
      host.appendChild(spareBottom);
    }

    // Initial placement
    let initialPlacement = STARTING_PLACEMENT;
    if (typeof opts.position === "string") {
      if (opts.position === "start") initialPlacement = STARTING_PLACEMENT;
      else initialPlacement = opts.position.split(" ")[0];
    } else if (opts.position && typeof opts.position === "object") {
      initialPlacement = posToFen(opts.position);
    }

    let lastFen = initialPlacement;
    let suppressEvents = false;
    let pendingDrop = null; // {orig, dest} captured by movable.events.after
    const draggable = opts.draggable !== false;

    const cgConfig = {
      fen: initialPlacement,
      orientation: opts.orientation === "black" ? "black" : "white",
      coordinates: opts.showNotation !== false,
      animation: { enabled: true, duration: 200 },
      highlight: { lastMove: true, check: true },
      drawable: {
        enabled: true,
        visible: true,
        defaultSnapToValidMove: false,
        eraseOnClick: true,
      },
      draggable: {
        enabled: draggable,
        deleteOnDropOff: opts.dropOffBoard === "trash",
        showGhost: true,
      },
      selectable: { enabled: draggable },
      movable: {
        free: true,
        color: draggable ? "both" : undefined,
        showDests: false,
        events: {
          after: (orig, dest) => {
            // Stash the move; the change event below fires next and pulls it.
            pendingDrop = { orig, dest };
          },
        },
      },
      events: {
        change: () => {
          if (suppressEvents) return;
          const newFen = api.getFen();
          const oldPos = fenToPos(lastFen);
          const newPos = fenToPos(newFen);
          lastFen = newFen;

          // onDrop fires for board-to-board moves. Spare drops are handled
          // separately below via the spare-piece DnD path.
          if (pendingDrop && typeof opts.onDrop === "function") {
            const { orig, dest } = pendingDrop;
            const piece = newPos[dest] || oldPos[orig];
            try {
              opts.onDrop(orig, dest, piece, newPos, oldPos);
            } catch (e) { console.error(e); }
          }
          pendingDrop = null;

          if (typeof opts.onChange === "function") {
            try { opts.onChange(oldPos, newPos); } catch (e) { console.error(e); }
          }
          if (typeof opts.onMoveEnd === "function") {
            // chessboard.js fires onMoveEnd after the slide animation. We fire
            // a touch later to approximate that timing.
            setTimeout(() => {
              try { opts.onMoveEnd(oldPos, newPos); } catch (e) { console.error(e); }
            }, 210);
          }
        },
      },
    };

    const api = window.Chessground(boardWrap, cgConfig);

    // ── Spare-piece drag/drop wiring ────────────────────────────────────── //
    if (opts.sparePieces) {
      const installSpares = (row) => {
        if (!row) return;
        row.addEventListener("dragstart", (ev) => {
          const cell = ev.target.closest(".cb-spare-piece");
          if (!cell) return;
          ev.dataTransfer.setData("text/plain", cell.dataset.piece);
          ev.dataTransfer.effectAllowed = "copy";
          row.classList.add("dragging");
        });
        row.addEventListener("dragend", () => row.classList.remove("dragging"));
      };
      installSpares(spareTop);
      installSpares(spareBottom);

      boardWrap.addEventListener("dragover", (ev) => {
        if (ev.dataTransfer && ev.dataTransfer.types.includes("text/plain")) {
          ev.preventDefault();
          ev.dataTransfer.dropEffect = "copy";
        }
      });
      boardWrap.addEventListener("drop", (ev) => {
        const piece = ev.dataTransfer && ev.dataTransfer.getData("text/plain");
        if (!piece || piece.length !== 2) return;
        ev.preventDefault();
        const sq = squareUnderCursor(api.state, boardWrap, ev.clientX, ev.clientY);
        if (!sq) return;
        const oldFen = api.getFen();
        const oldPos = fenToPos(oldFen);
        const newPos = { ...oldPos, [sq]: piece };
        const newFen = posToFen(newPos);
        suppressEvents = true;
        try { api.set({ fen: newFen }); } finally { suppressEvents = false; }
        lastFen = newFen;

        if (typeof opts.onDrop === "function") {
          try {
            opts.onDrop("spare", sq, piece, newPos, oldPos);
          } catch (e) { console.error(e); }
        }
        if (typeof opts.onChange === "function") {
          try { opts.onChange(oldPos, newPos); } catch (e) { console.error(e); }
        }
      });
    }

    // ── Public API ──────────────────────────────────────────────────────── //
    const adapter = {
      _cg: api,
      _host: host,
      position(arg, _animate) {
        let placement;
        if (typeof arg === "string") {
          placement = arg === "start" ? STARTING_PLACEMENT : arg.split(" ")[0];
        } else if (arg && typeof arg === "object") {
          placement = posToFen(arg);
        } else if (arg === false) {
          // chessboard.js: position() with no args returns current pos
          return fenToPos(api.getFen());
        } else {
          return fenToPos(api.getFen());
        }
        suppressEvents = true;
        try {
          api.set({ fen: placement, lastMove: undefined });
        } finally { suppressEvents = false; }
        lastFen = placement;
      },
      // Custom helper — paint a lichess-style last-move highlight.
      setLastMove(orig, dest) {
        if (!orig || !dest) {
          api.set({ lastMove: undefined });
        } else {
          api.set({ lastMove: [orig, dest] });
        }
      },
      fen() {
        return api.getFen();
      },
      flip() {
        api.toggleOrientation();
      },
      orientation(side) {
        if (side === "white" || side === "black") {
          api.set({ orientation: side });
        } else if (side === "flip") {
          api.toggleOrientation();
        }
        return api.state.orientation;
      },
      resize() {
        api.redrawAll();
      },
      clear(_animate) {
        suppressEvents = true;
        try { api.set({ fen: EMPTY_PLACEMENT, lastMove: undefined }); }
        finally { suppressEvents = false; }
        lastFen = EMPTY_PLACEMENT;
      },
      destroy() {
        try { api.destroy(); } catch (_) { /* ignore */ }
        host.innerHTML = "";
      },
    };
    return adapter;
  }

  Chessboard.fenToObj = fenToPos;
  Chessboard.objToFen = posToFen;
  window.Chessboard = Chessboard;
})();
