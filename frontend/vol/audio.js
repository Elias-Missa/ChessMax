/* Lichess audio module — preloaded HTMLAudioElements for the four standard
 * board sounds. State (enabled, volume) persists to localStorage.
 *
 * Exposes: window.LichessAudio = {
 *   play(name): void,        // 'move' | 'capture' | 'check' | 'notify'
 *   setEnabled(on): void,
 *   setVolume(v): void,      // 0–1
 *   isEnabled(): boolean,
 *   getVolume(): number,
 * }
 */
(function () {
  "use strict";

  const SOUND_DIR = "/static/vendor/sound/";
  const FILES = {
    move: "Move",
    capture: "Capture",
    check: "Check",
    notify: "GenericNotify",
  };
  const STORAGE_ENABLED = "cvb.audio.enabled";
  const STORAGE_VOLUME = "cvb.audio.volume";

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

  // Pick an extension the browser claims it can play. Falls back to mp3.
  function pickExt() {
    try {
      const a = document.createElement("audio");
      if (a.canPlayType("audio/ogg") !== "") return "ogg";
    } catch (_) { /* ignore */ }
    return "mp3";
  }

  let enabled = readBool(STORAGE_ENABLED, true);
  let volume = Math.min(1, Math.max(0, readNum(STORAGE_VOLUME, 0.6)));

  // Single HTMLAudioElement per sound; cloned per play so overlapping plays
  // don't restart mid-fire (e.g. rapid scrubbing through a game).
  const ext = pickExt();
  const elements = {};
  for (const [key, base] of Object.entries(FILES)) {
    try {
      const el = new Audio(`${SOUND_DIR}${base}.${ext}`);
      el.preload = "auto";
      el.volume = volume;
      elements[key] = el;
    } catch (_) { /* ignore */ }
  }

  function play(name) {
    if (!enabled) return;
    const src = elements[name];
    if (!src) return;
    try {
      const node = src.cloneNode(true);
      node.volume = volume;
      const p = node.play();
      if (p && typeof p.catch === "function") p.catch(() => { /* autoplay gate */ });
    } catch (_) { /* ignore */ }
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
    for (const el of Object.values(elements)) el.volume = volume;
  }
  function isEnabled() { return enabled; }
  function getVolume() { return volume; }

  window.LichessAudio = { play, setEnabled, setVolume, isEnabled, getVolume };
})();
