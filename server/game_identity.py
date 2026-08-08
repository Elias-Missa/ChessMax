"""Stable game identity for review/insights persistence (Insights.md B.2)."""

from __future__ import annotations

import hashlib
import io
import re
from typing import Any

import chess
import chess.pgn

_CHESSCOM_ID_RE = re.compile(
    r"chess\.com/(?:game|live/game)/(?:live/)?(\d+)",
    re.IGNORECASE,
)
_LICHESS_ID_RE = re.compile(
    r"lichess\.org/([a-zA-Z0-9]{8,12})",
    re.IGNORECASE,
)


def chesscom_game_id(meta_or_url: str | dict[str, Any]) -> str:
    """Return ``chesscom:<uuid-or-numeric-id>``."""

    if isinstance(meta_or_url, dict):
        raw = meta_or_url.get("game_id") or meta_or_url.get("url") or ""
    else:
        raw = meta_or_url
    raw = str(raw)
    match = _CHESSCOM_ID_RE.search(raw)
    if match:
        return f"chesscom:{match.group(1)}"
    # Chess.com PubAPI uuid field
    if re.fullmatch(r"[0-9a-fA-F-]{16,}", raw):
        return f"chesscom:{raw}"
    return f"chesscom:{raw}"


def lichess_game_id(url_or_id: str) -> str:
    match = _LICHESS_ID_RE.search(url_or_id)
    if match:
        return f"lichess:{match.group(1)}"
    return f"lichess:{url_or_id.strip()}"


def pgn_san_hash(pgn: str) -> str:
    """SHA-256 of the normalized SAN move list (strip comments/clocks/anns)."""

    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        digest = hashlib.sha256(pgn.strip().encode("utf-8")).hexdigest()
        return f"pgn:{digest}"
    board = game.board()
    sans: list[str] = []
    for move in game.mainline_moves():
        sans.append(board.san(move))
        board.push(move)
    payload = " ".join(sans)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"pgn:{digest}"


def resolve_game_id(
    *,
    source: str,
    pgn: str,
    meta: dict[str, Any] | None = None,
    url: str | None = None,
) -> str:
    """Pick a stable game_id from source + available identifiers."""

    meta = meta or {}
    if source == "chesscom":
        return chesscom_game_id(meta if meta else (url or ""))
    if source == "lichess":
        return lichess_game_id(url or meta.get("url") or meta.get("game_id") or "")
    return pgn_san_hash(pgn)


def result_from_chesscom(meta: dict[str, Any]) -> str | None:
    """Map Chess.com per-side results to ``1-0`` / ``0-1`` / ``1/2-1/2``."""

    white = str(meta.get("white_result") or "")
    black = str(meta.get("black_result") or "")
    wins = {"win"}
    losses = {"checkmated", "timeout", "resigned", "abandoned", "lose"}
    draws = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}
    if white in wins or black in losses:
        return "1-0"
    if black in wins or white in losses:
        return "0-1"
    if white in draws or black in draws:
        return "1/2-1/2"
    return None
