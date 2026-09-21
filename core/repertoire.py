"""Opening repertoire built from the games the player actually played.

There is no opening book in this repo and no intention of shipping one: a book
tells you what masters play, which is not what you need at 1400. What you need
is the position you reach every fifth game and lose win% in without noticing.
So the repertoire here is **mined, not authored** — every line is a path the
player has walked at least ``min_node_games`` times, and every recommendation is
backed either by their own results or by an engine move a stored review already
found.

Three verdicts come out of it, and they are the whole product:

* ``fix``  — you play this, and the reviews say it costs you. Play *that*.
* ``gap``  — you have not decided what you play here. Pick one, drill it.
* ``keep`` — your move, and nothing argues with it. Reinforce it so it is fast.

Everything in this module is pure: no engine, no database, no network. Review
evidence arrives as a plain ``fen -> PositionEvidence`` mapping that the server
layer assembles; without it every node is ``keep`` or ``gap``, which is the
correct degradation rather than a guess.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import chess

_CONSTANTS_PATH = Path(__file__).resolve().parent / "constants" / "repertoire.json"

FIX = "fix"
GAP = "gap"
KEEP = "keep"
VERDICTS: tuple[str, ...] = (FIX, GAP, KEEP)

WHITE = "white"
BLACK = "black"


@dataclass(frozen=True)
class RepertoireConstants:
    max_plies: int = 16
    min_node_games: int = 3
    min_branch_share: float = 0.25
    max_branches: int = 2
    consistent_share: float = 0.6
    fix_delta_w: float = 4.0
    score_prior_games: float = 2.0
    drill_recent_days: int = 3
    verdict_weight: dict[str, float] = field(
        default_factory=lambda: {FIX: 3.0, GAP: 2.0, KEEP: 1.0}
    )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RepertoireConstants":
        base = cls()
        weights = dict(base.verdict_weight)
        for key, value in (data.get("verdict_weight") or {}).items():
            if key in VERDICTS:
                weights[key] = float(value)
        return cls(
            max_plies=int(data.get("max_plies", base.max_plies)),
            min_node_games=int(data.get("min_node_games", base.min_node_games)),
            min_branch_share=float(data.get("min_branch_share", base.min_branch_share)),
            max_branches=int(data.get("max_branches", base.max_branches)),
            consistent_share=float(data.get("consistent_share", base.consistent_share)),
            fix_delta_w=float(data.get("fix_delta_w", base.fix_delta_w)),
            score_prior_games=float(
                data.get("score_prior_games", base.score_prior_games)
            ),
            drill_recent_days=int(data.get("drill_recent_days", base.drill_recent_days)),
            verdict_weight=weights,
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "RepertoireConstants":
        target = Path(path) if path is not None else _CONSTANTS_PATH
        with open(target, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


# --------------------------------------------------------------------------- #
# Inputs                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GameLine:
    """One of the player's games, reduced to what a repertoire needs.

    ``points`` is from the *player's* side (1 / 0.5 / 0), ``None`` for a game
    with no recorded result — those still shape the tree's move counts but are
    excluded from every score.
    """

    game_id: str
    user_color: str
    moves: Sequence[str]
    points: float | None = None
    opening: str | None = None
    eco: str | None = None


@dataclass(frozen=True)
class PositionEvidence:
    """What the stored reviews know about one position.

    ``loss_by_uci`` is the mean win% the player gave up on the plies where they
    played that move; ``best_uci`` is the engine's top line from the review's
    MultiPV. Both are optional — a position nobody has reviewed contributes an
    empty record, not a zero.
    """

    best_uci: str | None = None
    best_san: str | None = None
    loss_by_uci: Mapping[str, float] = field(default_factory=dict)
    n_by_uci: Mapping[str, int] = field(default_factory=dict)


def fen_key(fen: str) -> str:
    """Position identity: board, side, castling, en-passant — no clocks.

    The same opening position reached by transposition carries different move
    counters, and matching on the full FEN would file it as a different node.
    """

    return " ".join(str(fen).split(" ")[:4])


def canonical_fen(fen: str) -> str:
    """Re-emit a FEN through python-chess so its en-passant square is canonical.

    chess.js writes the ep square after *every* double pawn push; python-chess
    writes it only when an en-passant capture is actually legal (its ``fen()``
    defaults to ``en_passant="legal"``). So the browser calls the position after
    1.e4 ``... b KQkq e3`` and the server calls it ``... b KQkq -`` — the same
    position under two different keys.

    That mismatch is invisible until it is not: it forks the explorer cache, it
    makes a node created by the client a different row from the same node
    created by the server pushing a move, and it silently defeats transposition
    merging. Normalizing preserves a genuinely capturable ep square (those
    positions really are different — the spec is explicit at §6.4), and drops
    only the unusable one.
    """

    return chess.Board(str(fen)).fen()


def position_key(fen: str) -> str:
    """``fen_key`` over a canonicalized FEN — the identity the builder uses."""

    return fen_key(canonical_fen(fen))


# --------------------------------------------------------------------------- #
# The tree                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class TreeNode:
    """A position reached in the player's games, plus what happened from it."""

    key: str
    fen: str
    ply: int
    user_to_move: bool
    move_uci: str | None = None
    move_san: str | None = None
    games: int = 0
    points: float = 0.0
    decided: int = 0
    openings: Counter = field(default_factory=Counter)
    ecos: Counter = field(default_factory=Counter)
    game_ids: list[str] = field(default_factory=list)
    children: dict[str, "TreeNode"] = field(default_factory=dict)

    @property
    def score_pct(self) -> float | None:
        return self.points / self.decided if self.decided else None

    def shrunk_score(self, prior: float) -> float:
        """Score pulled toward 50% by ``prior`` phantom half-points.

        Used only to rank a node's own alternatives against each other, where a
        raw rate over a handful of games is mostly noise. It softens the
        ordering rather than reversing it — the hard protection against a fluke
        is that a move must clear ``min_node_games`` to be recommended at all.
        """

        return (self.points + prior * 0.5) / (self.decided + prior)


def build_tree(
    lines: Iterable[GameLine],
    *,
    color: str,
    constants: RepertoireConstants | None = None,
) -> TreeNode:
    """Replay every game of one colour into a move tree.

    Illegal or unparseable moves truncate that game rather than dropping it —
    a PGN that goes wrong on move 20 still tells us what was played on move 3.
    """

    constants = constants or RepertoireConstants.load()
    root = TreeNode(
        key="",
        fen=chess.Board().fen(),
        ply=0,
        user_to_move=(color == WHITE),
    )

    for line in lines:
        if line.user_color != color:
            continue
        board = chess.Board()
        node = root
        _absorb(node, line)
        path: list[str] = []
        for uci in list(line.moves)[: constants.max_plies]:
            try:
                move = chess.Move.from_uci(str(uci))
            except ValueError:
                break
            if move not in board.legal_moves:
                break
            san = board.san(move)
            board.push(move)
            path.append(move.uci())
            child = node.children.get(move.uci())
            if child is None:
                child = TreeNode(
                    key=" ".join(path),
                    fen=board.fen(),
                    ply=len(path),
                    user_to_move=(board.turn == chess.WHITE) == (color == WHITE),
                    move_uci=move.uci(),
                    move_san=san,
                )
                node.children[move.uci()] = child
            _absorb(child, line)
            node = child

    return root


def _absorb(node: TreeNode, line: GameLine) -> None:
    node.games += 1
    node.game_ids.append(line.game_id)
    if line.points is not None:
        node.points += float(line.points)
        node.decided += 1
    if line.opening:
        node.openings[line.opening] += 1
    if line.eco:
        node.ecos[line.eco] += 1


# --------------------------------------------------------------------------- #
# Extraction                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class Alternative:
    uci: str
    san: str
    games: int
    share: float
    score_pct: float | None
    mean_loss: float | None


@dataclass
class RepMove:
    """One ply of an extracted line.

    ``user_to_move`` says whose move ``uci`` is — when it is true, ``fen`` is a
    position the player has to find a move in, and the verdict applies.
    """

    ply: int
    fen: str
    user_to_move: bool
    uci: str
    san: str
    games: int
    share: float
    score_pct: float | None
    verdict: str | None = None
    recommended_uci: str | None = None
    recommended_san: str | None = None
    mean_loss: float | None = None
    reason: str | None = None
    alternatives: list[Alternative] = field(default_factory=list)


@dataclass
class RepLine:
    color: str
    opening: str | None
    eco: str | None
    games: int
    score_pct: float | None
    moves: list[RepMove]
    roi_score: float = 0.0

    @property
    def key(self) -> str:
        return " ".join(m.uci for m in self.moves)


def extract_lines(
    root: TreeNode,
    *,
    color: str,
    evidence: Mapping[str, PositionEvidence] | None = None,
    constants: RepertoireConstants | None = None,
) -> list[RepLine]:
    """Walk the tree down every branch that is actually part of the repertoire.

    A child is followed when it clears both bars — ``min_node_games`` in
    absolute terms and ``min_branch_share`` of its parent — and only the top
    ``max_branches`` are followed, so a player who meets six different second
    moves gets the two they actually face rather than a fan of singletons.

    **A measured ``fix`` is never dropped for being a side line.** Those bars
    exist to keep the tree small, but a habit the reviews have already measured
    as costing win% every time is precisely the thing worth drilling — on a real
    252-game account the caps hid every one of the eight such habits while the
    evidence for them sat in the database. So :func:`paths_to_fixes` marks every
    node on a route to one and those are followed as well. Marking the whole
    route matters: following the costly move itself achieves nothing if the
    branch three plies above it was already pruned.
    """

    constants = constants or RepertoireConstants.load()
    evidence = evidence or {}
    lines: list[RepLine] = []
    # Widening has to be path-aware: following the fix itself is useless if its
    # ancestors were already pruned, so mark every node on a route to one first.
    on_route = paths_to_fixes(root, evidence, constants)

    def eligible(node: TreeNode) -> list[TreeNode]:
        if node.ply >= constants.max_plies:
            return []
        kids = [
            child
            for child in node.children.values()
            if child.games >= constants.min_node_games
            and node.games
            and child.games / node.games >= constants.min_branch_share
        ]
        kids.sort(key=lambda c: (-c.games, c.move_uci or ""))
        chosen = kids[: constants.max_branches]
        taken = {c.move_uci for c in chosen}
        for child in node.children.values():
            if child.move_uci in taken:
                continue
            if child.games < constants.min_node_games:
                continue
            if child.key in on_route:
                chosen.append(child)
                taken.add(child.move_uci)
        return chosen

    def walk(node: TreeNode, prefix: list[RepMove]) -> None:
        kids = eligible(node)
        if not kids:
            if prefix:
                lines.append(_finish_line(color, node, prefix))
            return
        for child in kids:
            move = _describe(node, child, evidence, constants)
            walk(child, prefix + [move])

    walk(root, [])
    lines.sort(key=lambda line: (-line.games, line.key))
    return lines


def _finish_line(color: str, leaf: TreeNode, moves: list[RepMove]) -> RepLine:
    opening = leaf.openings.most_common(1)[0][0] if leaf.openings else None
    eco = leaf.ecos.most_common(1)[0][0] if leaf.ecos else None
    return RepLine(
        color=color,
        opening=opening,
        eco=eco,
        games=leaf.games,
        score_pct=leaf.score_pct,
        moves=moves,
    )


def _describe(
    parent: TreeNode,
    child: TreeNode,
    evidence: Mapping[str, PositionEvidence],
    constants: RepertoireConstants,
) -> RepMove:
    share = child.games / parent.games if parent.games else 0.0
    record = evidence.get(fen_key(parent.fen))
    alternatives = _alternatives(parent, record, constants)
    move = RepMove(
        ply=child.ply,
        fen=parent.fen,
        user_to_move=parent.user_to_move,
        uci=child.move_uci or "",
        san=child.move_san or "",
        games=child.games,
        share=round(share, 4),
        score_pct=child.score_pct,
        alternatives=alternatives,
    )
    if not parent.user_to_move:
        # The opponent's move. It shapes the line but there is nothing to drill.
        return move
    verdict, recommended, reason, mean_loss = classify_move(
        parent, child, record, constants
    )
    move.verdict = verdict
    move.reason = reason
    move.mean_loss = mean_loss
    move.recommended_uci = recommended
    move.recommended_san = _san_for(parent.fen, recommended) if recommended else None
    return move


def _alternatives(
    parent: TreeNode,
    record: PositionEvidence | None,
    constants: RepertoireConstants,
) -> list[Alternative]:
    out: list[Alternative] = []
    for uci, node in parent.children.items():
        loss = None
        if record is not None and uci in record.loss_by_uci:
            loss = round(float(record.loss_by_uci[uci]), 2)
        out.append(
            Alternative(
                uci=uci,
                san=node.move_san or "",
                games=node.games,
                share=round(node.games / parent.games, 4) if parent.games else 0.0,
                score_pct=node.score_pct,
                mean_loss=loss,
            )
        )
    out.sort(key=lambda a: (-a.games, a.uci))
    return out


def classify_move(
    parent: TreeNode,
    child: TreeNode,
    record: PositionEvidence | None,
    constants: RepertoireConstants,
) -> tuple[str, str | None, str, float | None]:
    """Decide what the player should do at ``parent``, and say why.

    Order matters. A habit the reviews have measured as costly is a ``fix``
    whatever its share — being consistent about a bad move is worse, not
    better. Only then does inconsistency become the story.
    """

    played = child.move_uci or ""
    mean_loss = None
    if record is not None and played in record.loss_by_uci:
        mean_loss = round(float(record.loss_by_uci[played]), 2)

    if (
        record is not None
        and record.best_uci
        and record.best_uci != played
        and mean_loss is not None
        and mean_loss >= constants.fix_delta_w
    ):
        n = int(record.n_by_uci.get(played, 0))
        games = "game" if n == 1 else "games"
        return (
            FIX,
            record.best_uci,
            f"{child.move_san} has cost you {mean_loss:.0f} win% a game "
            f"across {n} reviewed {games}. The engine plays "
            f"{record.best_san or record.best_uci} here.",
            mean_loss,
        )

    share = child.games / parent.games if parent.games else 0.0
    if (
        parent.games >= constants.min_node_games
        and share < constants.consistent_share
        and len(parent.children) > 1
    ):
        # Only moves that are themselves part of the repertoire may be
        # recommended: telling someone to settle on a move they have played
        # once is not a decision, it is another experiment. Fall back to the
        # whole set only when nothing clears the bar.
        candidates = [
            child
            for child in parent.children.values()
            if child.games >= constants.min_node_games
        ] or list(parent.children.values())
        pick = max(
            candidates,
            key=lambda n: (n.shrunk_score(constants.score_prior_games), n.games),
        )
        return (
            GAP,
            pick.move_uci,
            f"You have played {len(parent.children)} different moves here in "
            f"{parent.games} games. {pick.move_san} is the one that has scored "
            f"best — decide on it.",
            mean_loss,
        )

    return (
        KEEP,
        played,
        f"Your move in {child.games} of {parent.games} games"
        + (f", scoring {child.score_pct * 100:.0f}%." if child.score_pct is not None else "."),
        mean_loss,
    )


def paths_to_fixes(
    root: TreeNode,
    evidence: Mapping[str, PositionEvidence],
    constants: RepertoireConstants,
) -> set[str]:
    """Keys of every node on a route from the root to a measured ``fix``.

    Only nodes that clear ``min_node_games`` are walked, so this widens the
    repertoire toward known leaks without dragging one-off games in with them.
    """

    marked: set[str] = set()

    def visit(node: TreeNode) -> bool:
        if node.ply >= constants.max_plies:
            return False
        found = False
        for child in node.children.values():
            if child.games < constants.min_node_games:
                continue
            hit = measured_fix(node, child, evidence, constants)
            deeper = visit(child)
            if hit or deeper:
                marked.add(child.key)
                found = True
        return found

    visit(root)
    return marked


def measured_fix(
    parent: TreeNode,
    child: TreeNode,
    evidence: Mapping[str, PositionEvidence],
    constants: RepertoireConstants,
) -> bool:
    """Has this exact habit been *measured* as costly, over enough reviews?

    Stricter than the ``fix`` verdict itself, and deliberately: the verdict
    answers "what should the player do at a node already in their repertoire",
    while this answers "is this worth widening the tree for". Widening on a
    single reviewed ply would let one bad game add a branch.
    """

    if not parent.user_to_move:
        return False
    record = evidence.get(fen_key(parent.fen))
    if record is None or not record.best_uci:
        return False
    played = child.move_uci or ""
    loss = record.loss_by_uci.get(played)
    return (
        loss is not None
        and loss >= constants.fix_delta_w
        and record.best_uci != played
        and int(record.n_by_uci.get(played, 0)) >= constants.min_node_games
    )


def _san_for(fen: str, uci: str) -> str | None:
    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None
    if move not in board.legal_moves:
        return None
    return board.san(move)


# --------------------------------------------------------------------------- #
# Drill cards                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class DrillCard:
    """One position to answer, with everything the UI needs to explain itself."""

    card_id: str
    color: str
    fen: str
    ply: int
    expected_uci: str
    expected_san: str
    played_uci: str
    played_san: str
    verdict: str
    reason: str
    opening: str | None
    eco: str | None
    games: int
    score_pct: float | None
    roi_score: float
    line_san: list[str]
    alternatives: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "color": self.color,
            "fen": self.fen,
            "ply": self.ply,
            "expected_uci": self.expected_uci,
            "expected_san": self.expected_san,
            "played_uci": self.played_uci,
            "played_san": self.played_san,
            "verdict": self.verdict,
            "reason": self.reason,
            "opening": self.opening,
            "eco": self.eco,
            "games": self.games,
            "score_pct": self.score_pct,
            "roi_score": self.roi_score,
            "line_san": list(self.line_san),
            "alternatives": list(self.alternatives),
        }


def cards_from_lines(
    lines: Sequence[RepLine],
    *,
    constants: RepertoireConstants | None = None,
) -> list[DrillCard]:
    """One card per decision point, deduplicated by position.

    Two lines that share their first four moves share those decision points;
    emitting the card twice would drill the same position twice in one session
    and make the queue look longer than it is. The first line to reach a
    position owns it, and lines arrive frequency-ordered.
    """

    constants = constants or RepertoireConstants.load()
    seen: set[str] = set()
    cards: list[DrillCard] = []
    for line in lines:
        history: list[str] = []
        for move in line.moves:
            if not move.user_to_move or not move.verdict:
                history.append(move.san)
                continue
            expected = move.recommended_uci or move.uci
            identity = f"{line.color}:{fen_key(move.fen)}"
            if identity in seen:
                history.append(move.san)
                continue
            seen.add(identity)
            cards.append(
                DrillCard(
                    card_id=identity,
                    color=line.color,
                    fen=move.fen,
                    ply=move.ply,
                    expected_uci=expected,
                    expected_san=move.recommended_san or move.san,
                    played_uci=move.uci,
                    played_san=move.san,
                    verdict=move.verdict,
                    reason=move.reason or "",
                    opening=line.opening,
                    eco=line.eco,
                    games=move.games,
                    score_pct=move.score_pct,
                    roi_score=line.roi_score,
                    line_san=list(history),
                    alternatives=[
                        {
                            "uci": alt.uci,
                            "san": alt.san,
                            "games": alt.games,
                            "share": alt.share,
                            "score_pct": alt.score_pct,
                            "mean_loss": alt.mean_loss,
                        }
                        for alt in move.alternatives
                    ],
                )
            )
            history.append(move.san)
    return cards


def order_cards(
    cards: Sequence[DrillCard],
    history: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    constants: RepertoireConstants | None = None,
) -> list[DrillCard]:
    """Rank the queue: what you get wrong, in the lines that cost you most.

    ``history`` maps ``card_id`` to ``{"correct": bool, "days_ago": float}`` for
    the most recent attempt. A card answered correctly inside
    ``drill_recent_days`` sinks to the bottom rather than being removed — a
    short session should not run out of cards just because the good ones are
    fresh.
    """

    constants = constants or RepertoireConstants.load()
    history = history or {}

    def priority(card: DrillCard) -> tuple[float, float, float, str]:
        record = history.get(card.card_id) or {}
        seen_before = bool(record)
        correct = bool(record.get("correct"))
        days = float(record.get("days_ago", 0.0) or 0.0)
        fresh = correct and days < constants.drill_recent_days
        weight = constants.verdict_weight.get(card.verdict, 1.0)
        if not seen_before:
            recall = 1.0
        elif not correct:
            recall = 2.0
        else:
            recall = 0.0 if fresh else 0.5
        return (-(weight + recall), -card.roi_score, -card.games, card.card_id)

    return sorted(cards, key=priority)
