"""Phase 10 — structure, endgame types and unsupervised blunder clustering.

Three classifiers, each of which turns into a card:

* **10.1 Pawn structure families.** The phase split is time-based; *structure*
  is what decides which skills a position demands, and it is how coaches
  actually diagnose. A blind spot in every existing tool.
* **10.4 Endgame types.** "Endgame" is far too coarse — rook endings are roughly
  half of all endgames and nearly everyone is weak in them. Split by material
  signature, and where Syzygy tablebases are configured use them as ground
  truth: at ≤7 pieces that is literal perfect play, not an engine approximation.
* **10.5 Unsupervised clustering.** The motif taxonomy is hand-labelled, so it
  can only find what someone thought to name. Clustering blunders in raw feature
  space surfaces recurring personal signatures no named motif covers, paired
  with a montage of visually similar boards.

k-means is hand-rolled rather than imported: it is twenty lines, it keeps a
heavy dependency out of server start-up, and seeding it keeps immutable runs
reproducible.
"""

from __future__ import annotations

import math
import os
from collections import Counter, defaultdict
from typing import Any, Sequence

import chess

PIECE_VALUES = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}

#: Where to look for Syzygy tablebases. Absent by default; the card says so.
SYZYGY_ENV = "CHESS_TRAINER_SYZYGY"

STRUCTURE_LABELS = {
    "iqp": "Isolated queen's pawn",
    "hanging": "Hanging pawns",
    "carlsbad": "Carlsbad / minority attack",
    "french_chain": "French chain",
    "benoni_chain": "Benoni chain",
    "kid_chain": "King's Indian chain",
    "closed_centre": "Closed centre",
    "open_centre": "Open centre",
    "symmetrical": "Symmetrical",
    "opposite_castling": "Opposite-side castling",
    "other": "Other / unclassified",
}


# ── 10.1 Pawn structure families ──────────────────────────────────────────────


def _pawn_files(board: chess.Board, color: chess.Color) -> Counter:
    return Counter(
        chess.square_file(sq) for sq in board.pieces(chess.PAWN, color)
    )


def classify_structure(fen: str | None) -> str | None:
    """Name the pawn skeleton, or ``None`` when the FEN cannot be read.

    Tested in the order a coach would: the sharp, diagnostic structures first,
    the generic descriptions last.
    """

    if not fen:
        return None
    try:
        board = chess.Board(fen)
    except ValueError:
        return None

    white = _pawn_files(board, chess.WHITE)
    black = _pawn_files(board, chess.BLACK)
    if not white and not black:
        return "open_centre"

    def has(color: chess.Color, square: str) -> bool:
        piece = board.piece_at(chess.parse_square(square))
        return piece is not None and piece.piece_type == chess.PAWN and piece.color == color

    # Kings on opposite wings is a race, whatever the pawns are doing.
    wk, bk = board.king(chess.WHITE), board.king(chess.BLACK)
    if wk is not None and bk is not None:
        wf, bf = chess.square_file(wk), chess.square_file(bk)
        if (wf <= 2 and bf >= 5) or (wf >= 5 and bf <= 2):
            return "opposite_castling"

    # Named chains.
    if has(chess.WHITE, "e5") and has(chess.WHITE, "d4") and has(chess.BLACK, "e6") and has(chess.BLACK, "d5"):
        return "french_chain"
    if has(chess.WHITE, "d5") and has(chess.BLACK, "c5") and has(chess.BLACK, "e6"):
        return "benoni_chain"
    if has(chess.WHITE, "d5") and has(chess.WHITE, "e4") and has(chess.BLACK, "e5") and has(chess.BLACK, "d6"):
        return "kid_chain"

    def isolated(files: Counter, file_index: int) -> bool:
        return files.get(file_index, 0) > 0 and not (
            files.get(file_index - 1, 0) or files.get(file_index + 1, 0)
        )

    d_file, c_file, e_file = 3, 2, 4
    if isolated(white, d_file) or isolated(black, d_file):
        return "iqp"

    def hanging(files: Counter) -> bool:
        return (
            files.get(c_file, 0) > 0 and files.get(d_file, 0) > 0
            and not files.get(1, 0) and not files.get(e_file, 0)
        )

    if hanging(white) or hanging(black):
        return "hanging"

    # Carlsbad: the queen's-side majority structure of the exchange QGD.
    if (
        white.get(d_file, 0) and white.get(c_file, 0) and not white.get(e_file, 0)
        and black.get(d_file, 0) and black.get(c_file, 0) and not black.get(e_file, 0)
    ):
        return "carlsbad"

    centre_pawns = sum(white.get(f, 0) + black.get(f, 0) for f in (d_file, e_file))
    if centre_pawns == 0:
        return "open_centre"
    if centre_pawns >= 4:
        return "closed_centre"
    if white == black:
        return "symmetrical"
    return "other"


def compute_structures(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Loss per move by pawn structure family, over middlegame positions."""

    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"moves": 0, "loss": 0.0, "blunders": 0, "games": set()}
    )
    for row in rows:
        if not row.get("is_user_move") or row.get("is_book"):
            continue
        if str(row.get("phase") or "") != "middlegame":
            continue
        family = classify_structure(row.get("fen_before"))
        if not family:
            continue
        bucket = buckets[family]
        bucket["moves"] += 1
        bucket["loss"] += float(row.get("delta_w") or 0.0)
        if float(row.get("delta_w") or 0.0) >= 25:
            bucket["blunders"] += 1
        bucket["games"].add(str(row.get("game_id")))

    out = [
        {
            "family": key,
            "label": STRUCTURE_LABELS.get(key, key),
            "moves": b["moves"],
            "games": len(b["games"]),
            "delta_w_per_move": b["loss"] / b["moves"],
            "blunder_rate": b["blunders"] / b["moves"],
            # Structures are move-level and noisy, so the floor is higher than
            # elsewhere and also demands several games — 30 moves from one game
            # describes that game, not the structure.
            "below_floor": b["moves"] < 50 or len(b["games"]) < 3,
        }
        for key, b in buckets.items()
        if b["moves"]
    ]
    out.sort(key=lambda r: -r["delta_w_per_move"])
    scored = [r for r in out if not r["below_floor"] and r["family"] != "other"]
    worst = scored[0] if scored else None
    return {
        "rows": out,
        "worst": worst,
        "note": (
            f"You leak most in the {worst['label'].lower()} — "
            f"{worst['delta_w_per_move']:.2f} win% per move across {worst['games']} games."
            if worst else None
        ),
    }


# ── 10.4 Endgame types ────────────────────────────────────────────────────────


ENDGAME_LABELS = {
    "rook": "Rook endings",
    "rook_minor": "Rook + minor",
    "opposite_bishops": "Opposite-coloured bishops",
    "same_bishops": "Same-coloured bishops",
    "knight": "Knight endings",
    "queen": "Queen endings",
    "pawn": "Pawn endings",
    "other": "Other material",
}


def classify_endgame(fen: str | None) -> str | None:
    """Material signature of an endgame — 'endgame' alone is far too coarse."""

    if not fen:
        return None
    try:
        board = chess.Board(fen)
    except ValueError:
        return None

    def count(piece_type: int) -> int:
        return len(board.pieces(piece_type, chess.WHITE)) + len(board.pieces(piece_type, chess.BLACK))

    queens, rooks = count(chess.QUEEN), count(chess.ROOK)
    knights, bishops = count(chess.KNIGHT), count(chess.BISHOP)
    minors = knights + bishops

    if queens:
        return "queen"
    if rooks and minors:
        return "rook_minor"
    if rooks:
        return "rook"
    if bishops == 2 and knights == 0:
        squares = [
            sq for color in (chess.WHITE, chess.BLACK)
            for sq in board.pieces(chess.BISHOP, color)
        ]
        if len(squares) == 2:
            same_colour = (chess.square_rank(squares[0]) + chess.square_file(squares[0])) % 2 == (
                chess.square_rank(squares[1]) + chess.square_file(squares[1])
            ) % 2
            return "same_bishops" if same_colour else "opposite_bishops"
    if minors == 0:
        return "pawn"
    if knights and not bishops:
        return "knight"
    return "other"


def _open_tablebase():
    """Syzygy tablebases if a path is configured, else ``None``."""

    path = os.environ.get(SYZYGY_ENV)
    if not path or not os.path.isdir(path):
        return None
    try:
        import chess.syzygy

        return chess.syzygy.open_tablebase(path)
    except Exception:  # noqa: BLE001 — a missing or partial set is not an error
        return None


def compute_endgame_types(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Loss per move by endgame material, with tablebase ground truth if present.

    At ≤7 pieces a tablebase gives *optimal* play rather than an engine's
    approximation, which makes the resulting technique score uniquely
    trustworthy. Without tablebase files the card still reports the material
    split and says the DTZ column is unavailable.
    """

    tablebase = _open_tablebase()
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"moves": 0, "loss": 0.0, "games": set(), "dtz_checked": 0, "dtz_optimal": 0}
    )

    try:
        for row in rows:
            if not row.get("is_user_move") or str(row.get("phase") or "") != "endgame":
                continue
            fen = row.get("fen_before")
            family = classify_endgame(fen)
            if not family:
                continue
            bucket = buckets[family]
            bucket["moves"] += 1
            bucket["loss"] += float(row.get("delta_w") or 0.0)
            bucket["games"].add(str(row.get("game_id")))

            if tablebase is None or not row.get("move_uci"):
                continue
            try:
                board = chess.Board(fen)
                if chess.popcount(board.occupied) > 7:
                    continue
                played = chess.Move.from_uci(str(row["move_uci"]))
                if played not in board.legal_moves:
                    continue
                best = _dtz_best_moves(board, tablebase)
                if best:
                    bucket["dtz_checked"] += 1
                    if played in best:
                        bucket["dtz_optimal"] += 1
            except Exception:  # noqa: BLE001 — a probe miss is not a failure
                continue
    finally:
        if tablebase is not None:
            tablebase.close()

    rows_out = []
    for key, b in buckets.items():
        if not b["moves"]:
            continue
        rows_out.append({
            "family": key,
            "label": ENDGAME_LABELS.get(key, key),
            "moves": b["moves"],
            "games": len(b["games"]),
            "delta_w_per_move": b["loss"] / b["moves"],
            "dtz_checked": b["dtz_checked"],
            "dtz_optimal_rate": (
                b["dtz_optimal"] / b["dtz_checked"] if b["dtz_checked"] else None
            ),
            "below_floor": b["moves"] < 20,
        })
    rows_out.sort(key=lambda r: -r["moves"])

    scored = [r for r in rows_out if not r["below_floor"]]
    worst = max(scored, key=lambda r: r["delta_w_per_move"]) if scored else None
    return {
        "rows": rows_out,
        "worst": worst,
        "tablebase": tablebase is not None,
        "tablebase_note": (
            None if tablebase is not None
            else f"Set {SYZYGY_ENV} to a Syzygy directory for perfect-play "
                 f"comparison at 7 pieces or fewer."
        ),
        "note": (
            f"{worst['label']} are your weakest endgame type — "
            f"{worst['delta_w_per_move']:.2f} win% per move over {worst['moves']} moves."
            if worst else None
        ),
    }


def _dtz_best_moves(board: chess.Board, tablebase: Any) -> set[chess.Move]:
    """Every move preserving the best achievable DTZ outcome."""

    scored: list[tuple[chess.Move, tuple[int, int]]] = []
    for move in board.legal_moves:
        board.push(move)
        try:
            wdl = tablebase.probe_wdl(board)
            dtz = tablebase.probe_dtz(board)
        except Exception:  # noqa: BLE001
            board.pop()
            return set()
        board.pop()
        # Values are from the opponent's point of view after the move, so the
        # best move minimizes their WDL, then delays their progress.
        scored.append((move, (wdl, -abs(dtz))))
    if not scored:
        return set()
    best_key = min(key for _, key in scored)
    return {move for move, key in scored if key == best_key}


# ── 10.5 Unsupervised blunder clustering ──────────────────────────────────────


def _blunder_features(row: dict[str, Any]) -> list[float] | None:
    """Raw geometric/material description of a blunder, for clustering."""

    fen = row.get("fen_before")
    if not fen:
        return None
    try:
        board = chess.Board(fen)
    except ValueError:
        return None

    mover = board.turn
    material = sum(
        PIECE_VALUES[p.piece_type] * (1 if p.color == mover else -1)
        for p in board.piece_map().values()
    )
    king = board.king(not mover)
    own_king = board.king(mover)

    best = row.get("best_uci") or ""
    played = row.get("move_uci") or ""

    def dest_distance(uci: str, target: int | None) -> float:
        if not uci or len(uci) < 4 or target is None:
            return 4.0
        try:
            return float(chess.square_distance(chess.parse_square(uci[2:4]), target))
        except ValueError:
            return 4.0

    attackers = 0
    if own_king is not None:
        ring = chess.SquareSet(chess.BB_KING_ATTACKS[own_king])
        attackers = sum(1 for sq in ring if board.attackers(not mover, sq))

    return [
        float(row.get("delta_w") or 0.0) / 25.0,
        float(row.get("volatility") or 40.0) / 50.0,
        float(material) / 5.0,
        float(chess.popcount(board.occupied)) / 16.0,
        dest_distance(best, king) / 4.0,
        dest_distance(played, king) / 4.0,
        float(attackers) / 4.0,
        1.0 if "x" in str(row.get("san") or "") else 0.0,
    ]


def _kmeans(
    points: Sequence[Sequence[float]], k: int, *, seed: int = 7, iterations: int = 40
) -> list[int]:
    """Deterministic k-means++ seeding, so a recomputed run clusters identically."""

    import random

    rng = random.Random(seed)
    n = len(points)
    if n <= k:
        return list(range(n))

    centroids = [list(points[rng.randrange(n)])]
    while len(centroids) < k:
        distances = [
            min(sum((a - b) ** 2 for a, b in zip(p, c)) for c in centroids) for p in points
        ]
        total = sum(distances) or 1.0
        threshold = rng.random() * total
        cumulative = 0.0
        for idx, d in enumerate(distances):
            cumulative += d
            if cumulative >= threshold:
                centroids.append(list(points[idx]))
                break

    labels = [0] * n
    for _ in range(iterations):
        changed = False
        for i, point in enumerate(points):
            best = min(
                range(len(centroids)),
                key=lambda c: sum((a - b) ** 2 for a, b in zip(point, centroids[c])),
            )
            if labels[i] != best:
                labels[i] = best
                changed = True
        for c in range(len(centroids)):
            members = [points[i] for i in range(n) if labels[i] == c]
            if members:
                centroids[c] = [sum(v) / len(members) for v in zip(*members)]
        if not changed:
            break
    return labels


def _describe_cluster(members: Sequence[dict[str, Any]]) -> str:
    """Say what a cluster has in common, from what its members actually share."""

    phases = Counter(str(m.get("phase") or "?") for m in members)
    captures = sum(1 for m in members if "x" in str(m.get("san") or ""))
    pieces = Counter(
        str(m.get("san") or "?")[0] if str(m.get("san") or "?")[:1].isupper() else "pawn"
        for m in members
    )
    parts = []
    phase, phase_n = phases.most_common(1)[0]
    if phase_n / len(members) >= 0.6:
        parts.append(f"mostly in the {phase}")
    if captures / len(members) >= 0.6:
        parts.append("usually on a capture")
    elif captures / len(members) <= 0.15:
        parts.append("rarely on a capture")
    piece, piece_n = pieces.most_common(1)[0]
    if piece_n / len(members) >= 0.45:
        name = {"N": "knight", "B": "bishop", "R": "rook", "Q": "queen", "K": "king"}.get(piece, "pawn")
        parts.append(f"typically a {name} move")
    return ", ".join(parts) if parts else "no single shared trait"


def compute_blunder_clusters(
    rows: Sequence[dict[str, Any]], *, k: int = 4, min_blunders: int = 20
) -> dict[str, Any]:
    """Recurring personal signatures the hand-labelled motif taxonomy misses."""

    candidates = []
    features = []
    for row in rows:
        if not row.get("is_user_move") or float(row.get("delta_w") or 0.0) < 25:
            continue
        vector = _blunder_features(row)
        if vector is None:
            continue
        candidates.append(row)
        features.append(vector)

    if len(candidates) < min_blunders:
        return {
            "available": False,
            "n": len(candidates),
            "min_blunders": min_blunders,
            "reason": f"Needs at least {min_blunders} blunders with positions to cluster.",
        }

    labels = _kmeans(features, k)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row, label in zip(candidates, labels):
        grouped[label].append(row)

    clusters = []
    for label, members in grouped.items():
        clusters.append({
            "cluster": label,
            "n": len(members),
            "share": len(members) / len(candidates),
            "mean_delta_w": sum(float(m["delta_w"] or 0) for m in members) / len(members),
            "description": _describe_cluster(members),
            # The montage: visually similar boards side by side make the pattern
            # obvious before a word is read.
            "montage": [
                {
                    "game_id": m.get("game_id"),
                    "ply": m.get("ply"),
                    "fen": m.get("fen_before"),
                    "san": m.get("san"),
                    "best_uci": m.get("best_uci"),
                    "move_uci": m.get("move_uci"),
                    "delta_w": m.get("delta_w"),
                }
                for m in sorted(members, key=lambda x: -float(x["delta_w"] or 0))[:6]
            ],
        })
    clusters.sort(key=lambda c: -c["n"])

    top = clusters[0] if clusters else None
    return {
        "available": True,
        "n": len(candidates),
        "k": k,
        "clusters": clusters,
        "note": (
            f"{top['share'] * 100:.0f}% of your blunders share one signature: {top['description']}."
            if top and top["share"] >= 0.3
            else None
        ),
    }


def compute_structure_analysis(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Everything in Phase 10, over one pass of enriched move rows."""

    return {
        "pawn_structures": compute_structures(rows),
        "endgame_types": compute_endgame_types(rows),
        "blunder_clusters": compute_blunder_clusters(rows),
    }
