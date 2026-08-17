/* Lichess "standard" board sounds — simple wooden move + capture clicks.
 * Matches lichess.org default: no check stinger, no victory fanfare on moves.
 */
(function () {
  "use strict";

  const SOUND_DIR = "/static/sounds/";
  const FILES = { move: "Move", capture: "Capture" };
  const STORAGE_ENABLED = "cvb.audio.enabled";
  const STORAGE_VOLUME = "cvb.audio.volume";
  const THROTTLE_MS = 80;

  function readBool(key, fallback) {
    try {
      const v = window.localStorage.getItem(key);
      if (v === null) return fallback;
      return v === "1" || v === "true";
    } catch (_) { return fallback; }
  }
  function readNum(key, fallback) {
    try {
      const v = window.localStorage.getItem(key);
      if (v === null) return fallback;
      const n = Number(v);
      return Number.isFinite(n) ? n : fallback;
    } catch (_) { return fallback; }
  }
  function writeKV(key, value) {
    try { window.localStorage.setItem(key, String(value)); } catch (_) { /* ignore */ }
  }

  let enabled = readBool(STORAGE_ENABLED, true);
  let volume = Math.min(1, Math.max(0, readNum(STORAGE_VOLUME, 0.7)));

  let ctx = null;
  const buffers = new Map();
  let resumeBound = false;
  const lastPlayed = new Map();

  function makeContext() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return null;
      return new Ctx({ latencyHint: "interactive" });
    } catch (_) {
      return null;
    }
  }

  async function resumeContext() {
    if (!ctx) ctx = makeContext();
    if (!ctx) return false;
    if (ctx.state === "suspended") {
      try { await ctx.resume(); } catch (_) { /* ignore */ }
    }
    return ctx.state === "running";
  }

  function bindResumeOnGesture() {
    if (resumeBound) return;
    resumeBound = true;
    const primer = () => { void resumeContext(); };
    for (const ev of ["pointerdown", "mousedown", "keydown", "touchend"]) {
      window.addEventListener(ev, primer, { capture: true, once: true });
    }
  }

  async function loadBuffer(baseName) {
    for (const ext of ["mp3", "ogg"]) {
      try {
        const res = await fetch(`${SOUND_DIR}${baseName}.${ext}`);
        if (!res.ok) continue;
        const audioCtx = ctx || makeContext();
        if (!audioCtx) return null;
        ctx = audioCtx;
        const data = await res.arrayBuffer();
        if (data.byteLength < 256) continue;
        const buffer = await new Promise((resolve, reject) => {
          if (audioCtx.decodeAudioData.length === 1) {
            audioCtx.decodeAudioData(data).then(resolve).catch(reject);
          } else {
            audioCtx.decodeAudioData(data, resolve, reject);
          }
        });
        return buffer;
      } catch (_) { /* try next ext */ }
    }
    return null;
  }

  async function preload() {
    bindResumeOnGesture();
    ctx = ctx || makeContext();
    if (!ctx) return;
    await Promise.all(
      Object.values(FILES).map(async (base) => {
        const buffer = await loadBuffer(base);
        if (buffer) buffers.set(base, buffer);
      }),
    );
  }

  function throttled(name, vol = 1) {
    const now = Date.now();
    const last = lastPlayed.get(name) || 0;
    if (now - last < THROTTLE_MS) return;
    lastPlayed.set(name, now);
    void play(name, vol);
  }

  async function play(name, vol = 1) {
    if (!enabled || volume === 0) return;
    const base = FILES[name];
    if (!base) return;
    if (!(await resumeContext())) return;
    if (!buffers.has(base)) {
      const buffer = await loadBuffer(base);
      if (!buffer) return;
      buffers.set(base, buffer);
    }
    const buffer = buffers.get(base);
    if (!buffer || !ctx) return;
    const source = ctx.createBufferSource();
    const gain = ctx.createGain();
    source.buffer = buffer;
    gain.gain.value = Math.min(1, Math.max(0, volume * vol));
    source.connect(gain);
    gain.connect(ctx.destination);
    source.start(0);
  }

  /** Move or capture only — same rule as lichess.org default (standard set). */
  function playMove(opts = {}) {
    if (!enabled) return;
    const san = opts.san || "";
    if (san.includes("x")) throttled("capture");
    else throttled("move");
  }

  function setEnabled(on) {
    enabled = !!on;
    writeKV(STORAGE_ENABLED, enabled ? "1" : "0");
  }
  function setVolume(v) {
    const clamped = Math.min(1, Math.max(0, Number(v)));
    if (!Number.isFinite(clamped)) return;
    volume = clamped;
    writeKV(STORAGE_VOLUME, String(volume));
  }
  function isEnabled() { return enabled; }
  function getVolume() { return volume; }

  window.LichessAudio = {
    play: (name) => { if (name === "move" || name === "capture") throttled(name); },
    playMove,
    playGameResult() { /* standard set: no game-end stinger on the board */ },
    setEnabled,
    setVolume,
    isEnabled,
    getVolume,
  };

  void preload();
})();
