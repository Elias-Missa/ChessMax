// Hero demo data — Paul Morphy vs Duke Karl / Count Isouard, Paris Opera, 1858.
//
// GENERATED — do not hand-edit. Run `python -m scripts.generate_home_demo`.
//
// These are not marketing numbers. Every row was produced by the same code
// paths the product runs: `core.volatility.compute_volatility` (Stockfish
// depth 20, MultiPV 6) for `vol`, `core.evaluation.win_prob_cp` for
// `win`, and `core.findability.score_position` against the Maia3Policy policy head
// for `find` / `band` / `best`. All three describe the position *before* the
// move on that row — i.e. the decision the player was actually facing.

window.HM_OPERA = {
  white: "Paul Morphy",
  black: "Duke Karl / Count Isouard",
  event: "Paris Opera",
  year: 1858,
  plies: [
    { uci: "e2e4", san: "e4", vol: 1.7, win: 53.1, find: 88, band: "Obvious", best: "e4" },
    { uci: "e7e5", san: "e5", vol: 4.3, win: 54.3, find: 67, band: "Natural", best: "e5" },
    { uci: "g1f3", san: "Nf3", vol: 12.6, win: 53.2, find: 74, band: "Natural", best: "Nf3" },
    { uci: "d7d6", san: "d6", vol: 19.0, win: 54.2, find: 78, band: "Natural", best: "Nc6" },
    { uci: "d2d4", san: "d4", vol: 23.3, win: 56.7, find: 73, band: "Natural", best: "d4" },
    { uci: "c8g4", san: "Bg4", vol: 8.9, win: 56.2, find: 67, band: "Natural", best: "Nf6" },
    { uci: "d4e5", san: "dxe5", vol: 7.0, win: 59.9, find: 62, band: "Natural", best: "Be3" },
    { uci: "g4f3", san: "Bxf3", vol: 11.0, win: 58.3, find: 34, band: "Hard", best: "Nc6" },
    { uci: "d1f3", san: "Qxf3", vol: 41.8, win: 64.3, find: 70, band: "Natural", best: "Qxf3" },
    { uci: "d6e5", san: "dxe5", vol: 29.8, win: 63.6, find: 94, band: "Obvious", best: "dxe5" },
    { uci: "f1c4", san: "Bc4", vol: 20.7, win: 63.6, find: 43, band: "Needs thought", best: "Qb3" },
    { uci: "g8f6", san: "Nf6", vol: 41.4, win: 61.8, find: 27, band: "Hard", best: "Qd7" },
    { uci: "f3b3", san: "Qb3", vol: 55.0, win: 69.9, find: 19, band: "Hard", best: "Qb3" },
    { uci: "d8e7", san: "Qe7", vol: 23.7, win: 70.0, find: 50, band: "Needs thought", best: "Bc5" },
    { uci: "b1c3", san: "Nc3", vol: 37.5, win: 69.7, find: 84, band: "Obvious", best: "Qxb7" },
    { uci: "c7c6", san: "c6", vol: 39.1, win: 67.6, find: 30, band: "Hard", best: "c6" },
    { uci: "c1g5", san: "Bg5", vol: 27.0, win: 67.7, find: 69, band: "Natural", best: "Bg5" },
    { uci: "b7b5", san: "b5", vol: 26.9, win: 67.9, find: 3, band: "Engine-only", best: "Kd8" },
    { uci: "c3b5", san: "Nxb5", vol: 79.2, win: 76.9, find: 49, band: "Needs thought", best: "Nxb5" },
    { uci: "c6b5", san: "cxb5", vol: 63.3, win: 74.6, find: 13, band: "Engine-only", best: "Qb4+" },
    { uci: "c4b5", san: "Bxb5+", vol: 72.6, win: 86.8, find: 56, band: "Needs thought", best: "Bxb5+" },
    { uci: "b8d7", san: "Nbd7", vol: 21.7, win: 86.7, find: 93, band: "Obvious", best: "Nbd7" },
    { uci: "e1c1", san: "O-O-O", vol: 83.7, win: 86.4, find: 44, band: "Needs thought", best: "O-O-O" },
    { uci: "a8d8", san: "Rd8", vol: 22.1, win: 87.2, find: 52, band: "Needs thought", best: "Rb8" },
    { uci: "d1d7", san: "Rxd7", vol: 43.6, win: 87.0, find: 18, band: "Hard", best: "Rxd7" },
    { uci: "d8d7", san: "Rxd7", vol: 25.7, win: 86.3, find: 80, band: "Obvious", best: "Nxd7" },
    { uci: "h1d1", san: "Rd1", vol: 90.5, win: 91.8, find: 48, band: "Needs thought", best: "Rd1" },
    { uci: "e7e6", san: "Qe6", vol: 4.1, win: 91.9, find: 61, band: "Natural", best: "Kd8" },
    { uci: "b5d7", san: "Bxd7+", vol: 52.9, win: 96.1, find: 36, band: "Hard", best: "Bxd7+" },
    { uci: "f6d7", san: "Nxd7", vol: 33.8, win: 96.0, find: 73, band: "Natural", best: "Nxd7" },
    { uci: "b3b8", san: "Qb8+", vol: 100.0, win: 99.9, find: 48, band: "Needs thought", best: "Qb8+" },
    { uci: "d7b8", san: "Nxb8", vol: null, win: 99.9, find: null, band: null, best: null },
    { uci: "d1d8", san: "Rd8#", vol: 98.4, win: 99.9, find: 99, band: "Obvious", best: "Rd8#" },
  ],
};
