/* Insights post-mortem: Verdict / Why you lose / How to fix it.
 *
 * Server owns the story (`metrics.narrative`). This file only renders.
 * Deep Dive is the existing dashboard — we hand off, we do not rebuild it.
 */
(function () {
  const overlay = document.getElementById("postmortem");
  if (!overlay) return;

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const scrollEl = $("#pm-scroll");
  const spineEl = $("#pm-spine");
  const identityEl = $("#pm-identity");
  const verdictEl = $("#pm-verdict");
  const whyEl = $("#pm-why");
  const howEl = $("#pm-how");
  const secnavEl = $("#pm-secnav");
  const footEl = $("#pm-foot");

  const TABS = {
    verdict: { screen: "verdict", path: "verdict" },
    why: { screen: "why", path: "why-you-lose" },
    how: { screen: "how", path: "how-you-win" },
    deep: { screen: "deep", path: "deep-dive" },
  };

  const FIG = { K: "♔", Q: "♕", R: "♖", B: "♗", N: "♘" };

  // "Train this" used to be three identical buttons. Name the destination so
  // the reader can tell which of the fixes actually sends them somewhere new.
  const PRACTICE_LABEL = {
    mistakes: "Your Mistakes",
    forced: "Forced Lines",
    defense: "Defense Gym",
    guess: "Guess the Eval",
  };

  let metrics = null;
  let meta = {};
  let screen = "verdict";
  let grounds = [];
  let boardObserver = null;

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fig(san) {
    return escapeHtml(String(san || "")).replace(/[KQRBN]/g, (c) => FIG[c] || c);
  }

  function pct(v) {
    if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
    return `${Math.round(Number(v) * 100)}%`;
  }

  function shortOpening(name, limit) {
    const cap = limit || 36;
    let s = String(name || "").replace(/\s+/g, " ").trim();
    if (!s) return "Unknown opening";
    const prefixes = [
      "Queens Pawn Opening ", "Queen's Pawn Opening ",
      "Queens Pawn Game ", "Queen's Pawn Game ",
      "Kings Pawn Opening ", "King's Pawn Opening ",
      "Kings Pawn Game ", "King's Pawn Game ",
    ];
    for (const p of prefixes) {
      if (s.startsWith(p) && s.length > p.length + 3) {
        s = s.slice(p.length);
        break;
      }
    }
    if (s.length <= cap) return s;
    const cut = s.lastIndexOf(" ", cap);
    const stem = (cut >= 14 ? s.slice(0, cut) : s.slice(0, cap)).replace(/[ -:,]+$/, "");
    return `${stem}…`;
  }

  function narr() {
    return (metrics && metrics.narrative) || {};
  }

  function runId() {
    return (meta && (meta.run_id || meta.runId)) || window.__insightsActiveRunId?.() || "";
  }

  function storyPath(tab) {
    const id = runId();
    const slug = (TABS[tab] || TABS.verdict).path;
    return id ? `/insights/${id}/${slug}` : "/insights";
  }

  function parsePath(pathname) {
    const path = String(pathname || "").replace(/\/+$/, "") || "/";
    const m = path.match(/^\/insights\/([^/]+)\/(verdict|why-you-lose|how-you-win|deep-dive)$/);
    if (!m) return null;
    const tab = m[2] === "why-you-lose" ? "why"
      : m[2] === "how-you-win" ? "how"
      : m[2] === "deep-dive" ? "deep"
      : "verdict";
    return { runId: m[1], tab };
  }

  function navigate(tab, { push = true } = {}) {
    const path = storyPath(tab);
    if (push && window.__shellNavigate) window.__shellNavigate(path);
    else if (push && window.location.pathname !== path) {
      history.pushState({ chessmax: true, path }, "", path);
    }
  }

  // ── Open / close ──────────────────────────────────────────────────────

  function pinToHeader() {
    const header = document.querySelector(".shell-header");
    const top = header ? Math.round(header.getBoundingClientRect().height) : 0;
    overlay.style.top = `${top}px`;
    document.documentElement.style.setProperty("--shell-header-h", `${top}px`);
  }

  function open(tab, { push = true } = {}) {
    const next = tab && TABS[tab] ? tab : "verdict";
    if (next === "deep") {
      close({ silent: true });
      if (window.__insightsOpenDeepDive) window.__insightsOpenDeepDive();
      if (push) navigate("deep");
      return;
    }
    if (window.__insightsCloseDeepDive) window.__insightsCloseDeepDive();
    if (!metrics) return;
    pinToHeader();
    screen = next;
    overlay.classList.remove("hidden");
    overlay.dataset.pmScreen = next;
    overlay.classList.toggle("pm-no-spine", next !== "why");
    document.body.style.overflow = "hidden";
    $$(".pm-tab", overlay).forEach((btn) => {
      btn.classList.toggle("is-on", btn.dataset.pm === next);
    });
    verdictEl.classList.toggle("hidden", next !== "verdict");
    whyEl.classList.toggle("hidden", next !== "why");
    howEl.classList.toggle("hidden", next !== "how");
    if (scrollEl) scrollEl.scrollTop = 0;
    renderScreen(next);
    buildSecnav(next);
    renderFoot(next);
    drawSpine();
    const path = storyPath(next);
    if (push && window.location.pathname !== path) navigate(next);
  }

  function close({ silent } = {}) {
    overlay.classList.add("hidden");
    destroyBoards();
    if (!document.getElementById("insights-dashboard")?.classList.contains("hidden")) {
      /* deep dive owns the scroll lock */
    } else {
      document.body.style.overflow = "";
    }
    if (!silent && window.location.pathname.startsWith("/insights/") && window.__shellNavigate) {
      window.__shellNavigate("/insights");
    }
  }

  // ── Render ────────────────────────────────────────────────────────────

  function adopt(nextMetrics, nextMeta) {
    metrics = nextMetrics;
    meta = nextMeta || {};
    const handle = (meta.handle || meta.chesscom_handle || "").trim();
    if (identityEl) {
      const bits = [
        handle,
        meta.time_class,
        meta.window_days ? `${meta.window_days}d` : "",
      ].filter(Boolean);
      identityEl.textContent = bits.join(" · ");
    }
    if (!overlay.classList.contains("hidden")) renderScreen(screen);
  }

  function renderScreen(tab) {
    const n = narr();
    if (tab === "verdict") renderVerdict(n);
    else if (tab === "why") renderWhy(n);
    else if (tab === "how") renderHow(n);
    mountBoards();
  }

  // ── Section rail + footer ─────────────────────────────────────────────
  //
  // "Why you lose" runs to several thousand pixels. The rail is the map of
  // what is below; the footer is the way forward. Both live outside the
  // scroller, so neither can end up sitting on top of the content.

  const SCREEN_EL = { verdict: () => verdictEl, why: () => whyEl, how: () => howEl };

  function buildSecnav(tab) {
    if (!secnavEl) return;
    const host = (SCREEN_EL[tab] || SCREEN_EL.verdict)();
    const sections = host ? $$(".pm-section", host) : [];
    if (sections.length < 3) {
      secnavEl.classList.add("hidden");
      secnavEl.innerHTML = "";
      return;
    }
    secnavEl.innerHTML = sections.map((sec, i) => {
      sec.id = `pm-sec-${tab}-${i}`;
      const label = (sec.querySelector("h2") || {}).textContent || `Section ${i + 1}`;
      return `<button type="button" data-pm-sec="${sec.id}"${i === 0 ? ' class="is-on"' : ""}>` +
        `${escapeHtml(label)}</button>`;
    }).join("");
    secnavEl.classList.remove("hidden");
    secnavEl.scrollLeft = 0;
  }

  function syncSecnav() {
    if (!secnavEl || secnavEl.classList.contains("hidden") || !scrollEl) return;
    const buttons = $$("button[data-pm-sec]", secnavEl);
    if (!buttons.length) return;
    // The section the reader is actually in: the last one whose top has
    // passed a line a little below the top of the scrollport.
    const line = scrollEl.getBoundingClientRect().top + 120;
    let active = 0;
    buttons.forEach((btn, i) => {
      const sec = document.getElementById(btn.dataset.pmSec);
      if (sec && sec.getBoundingClientRect().top <= line) active = i;
    });
    buttons.forEach((btn, i) => btn.classList.toggle("is-on", i === active));
    const on = buttons[active];
    if (on && on.offsetLeft < secnavEl.scrollLeft) {
      secnavEl.scrollLeft = Math.max(0, on.offsetLeft - 16);
    } else if (on && on.offsetLeft + on.offsetWidth > secnavEl.scrollLeft + secnavEl.clientWidth) {
      secnavEl.scrollLeft = on.offsetLeft + on.offsetWidth - secnavEl.clientWidth + 16;
    }
  }

  const FOOT = {
    verdict: { step: "Step 1 of 3 · Verdict", next: "why", nextLabel: "See why you lose" },
    why: { step: "Step 2 of 3 · Why you lose", next: "how", nextLabel: "How to fix it" },
    how: { step: "Step 3 of 3 · How to fix it", next: "deep", nextLabel: "Deep dive: the numbers" },
  };

  function renderFoot(tab) {
    if (!footEl) return;
    const f = FOOT[tab];
    if (!f) {
      footEl.innerHTML = "";
      return;
    }
    const order = ["verdict", "why", "how"];
    const prev = order[order.indexOf(tab) - 1];
    footEl.innerHTML =
      `<span class="pm-foot-step">${escapeHtml(f.step)}</span>` +
      `<span class="pm-foot-spacer"></span>` +
      (prev
        ? `<button type="button" class="pm-cta pm-cta--ghost" data-pm-go="${prev}">Back</button>`
        : "") +
      `<button type="button" class="pm-cta pm-cta--primary" data-pm-go="${f.next}">` +
        `${escapeHtml(f.nextLabel)}</button>`;
  }

  function renderVerdict(n) {
    const v = n.verdict || {};
    const rec = v.record || {};
    const losses = rec.losses ?? (typeof v.headline === "object" ? v.headline.losses : null);
    const games = rec.games ?? (typeof v.headline === "object" ? v.headline.games : null);
    const diagnosis = typeof v.diagnosis === "string" && v.diagnosis
      ? v.diagnosis
      : (typeof v.headline === "string" ? v.headline
        : (v.headline && v.headline.sentence) || "");
    const ok = (n.sufficiency || v.sufficiency || {}).ok;
    const reason = (n.sufficiency || {}).reason;

    if (!ok && reason && !(losses > 0)) {
      verdictEl.innerHTML =
        `<div class="pm-empty"><p class="pm-kicker">Verdict</p><p>${escapeHtml(reason)}</p></div>`;
      return;
    }

    const chips = (v.chips || []).map((c) => (
      `<div class="pm-chip"><span>${escapeHtml(c.label)}</span><b>${escapeHtml(c.value)}</b></div>`
    )).join("");

    const not = (v.not_the_reason || []);
    const notBlock = not.length
      ? `<div class="pm-not"><h3>Not the reason</h3><ul>${
          not.map((line) => `<li>${escapeHtml(line)}</li>`).join("")
        }</ul></div>`
      : "";

    const elo = v.elo_left;
    const eloLine = Number.isFinite(Number(elo))
      ? `<p class="pm-elo-left" title="What the same games would have scored had the flagged moves gone the ` +
        `other way, converted to rating.">` +
        `<strong>+${Math.round(Number(elo))}</strong> rating points still sitting on the board.</p>`
      : "";

    verdictEl.innerHTML =
      `<div class="pm-prose">` +
        `<p class="pm-kicker">Verdict</p>` +
        `<p class="pm-verdict-num">${escapeHtml(String(losses ?? "—"))}</p>` +
        `<p class="pm-verdict-sub">losses in your last ${escapeHtml(String(games ?? "—"))} games` +
          `${rec.wins != null ? ` · ${rec.wins}W–${rec.draws ?? 0}D–${rec.losses}L` : ""}</p>` +
        `<p class="pm-diagnosis">${escapeHtml(diagnosis)}</p>` +
        (chips ? `<div class="pm-chips">${chips}</div>` : "") +
        notBlock +
        eloLine +
      `</div>`;
  }

  function renderWhy(n) {
    const w = n.why_you_lose || {};

    whyEl.innerHTML =
      `<div class="pm-wide">` +
        `<p class="pm-kicker">Why you lose</p>` +
        `<h2 class="pm-diagnosis" style="margin-top:0">The pattern, stage by stage.</h2>` +
        funnelHtml(w.funnel) +
        trajHtml(w.shapes, metrics) +
        openingsHtml(w.openings) +
        tacticsHtml(w.tactics) +
        habitsHtml(w.habits) +
        phaseHtml(w.phase) +
        twinsHtml(w.twins) +
        momentsHtml(w.moments, "The positions") +
      `</div>`;
  }

  function funnelHtml(funnel) {
    const stages = Array.isArray(funnel) ? funnel.filter((s) => s && s.n != null && s.label) : [];
    if (!stages.length) return "";
    const top = Math.max(...stages.map((s) => Number(s.n) || 0), 1);
    let prevPct = 100;
    const rows = stages.map((s, i) => {
      const raw = Math.round(100 * (Number(s.n) || 0) / top);
      const width = i === 0 ? 100 : Math.max(24, Math.min(raw, prevPct - 10));
      prevPct = width;
      return (
        `<div class="pm-funnel-row" style="--pm-w:${width}%">` +
          `<div class="pm-funnel-stage" title="${escapeHtml(s.label)}">` +
            `<b>${escapeHtml(String(s.n))}</b>` +
            `<span>${escapeHtml(s.label)}</span>` +
          `</div>` +
        `</div>` +
        (s.caption ? `<p class="pm-funnel-cap">${escapeHtml(s.caption)}</p>` : "")
      );
    }).join("");
    return (
      `<section class="pm-section">` +
        `<h2>The funnel</h2>` +
        `<p class="pm-lede">Each stage is a narrower claim. The last one is the pattern.</p>` +
        `<div class="pm-funnel">${rows}</div>` +
      `</section>`
    );
  }

  function trajHtml(shapes, allMetrics) {
    const facts = (allMetrics && allMetrics.game_explorer) || [];
    const losses = facts.filter((f) => f.outcome === "loss" && (f.sparkline || []).length > 1);
    const svg = trajectorySvg(losses);
    const shapeRows = (Array.isArray(shapes) ? shapes : []).map((s) => {
      const width = Math.max(4, Math.round((s.share || 0) * 100));
      return (
        `<div class="pm-shape">` +
          `<div class="pm-shape-label">${escapeHtml(s.label)}</div>` +
          `<div class="pm-shape-track"><span class="pm-shape-bar" style="width:${width}%"></span></div>` +
          `<b>${escapeHtml(String(s.n))}</b>` +
        `</div>`
      );
    }).join("");
    if (!svg && !shapeRows) return "";
    return (
      `<section class="pm-section">` +
        `<h2>How the games died</h2>` +
        `<p class="pm-lede">Every loss as a winning-chance curve. A slow slide looks nothing like a single blunder.</p>` +
        (svg || "") +
        (shapeRows ? `<div class="pm-shapes" style="margin-top:22px">${shapeRows}</div>` : "") +
      `</section>`
    );
  }

  function userCurve(fact) {
    const raw = (fact.sparkline || []).map(Number).filter(Number.isFinite);
    if (fact.user_color === "black") return raw.map((v) => 1 - v);
    return raw;
  }

  function trajectorySvg(losses) {
    const sample = (losses || []).slice(0, 16);
    if (sample.length < 2) return "";
    const w = 640;
    const h = 200;
    const pad = { l: 8, r: 8, t: 10, b: 10 };
    const innerW = w - pad.l - pad.r;
    const innerH = h - pad.t - pad.b;
    const paths = sample.map((f) => {
      const c = userCurve(f);
      if (c.length < 2) return "";
      const d = c.map((v, i) => {
        const x = pad.l + (i / (c.length - 1)) * innerW;
        const y = pad.t + (1 - Math.max(0, Math.min(1, v))) * innerH;
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
      }).join(" ");
      return `<path d="${d}" fill="none" stroke="rgba(224,68,58,0.16)" stroke-width="1.15"/>`;
    }).join("");
    const midY = pad.t + innerH / 2;
    const mean = meanCurve(sample);
    let meanPath = "";
    if (mean.length > 1) {
      meanPath = mean.map((v, i) => {
        const x = pad.l + (i / (mean.length - 1)) * innerW;
        const y = pad.t + (1 - Math.max(0, Math.min(1, v))) * innerH;
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
      }).join(" ");
    }
    return (
      `<svg class="pm-traj" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Loss trajectories">` +
        `<line x1="${pad.l}" y1="${midY}" x2="${w - pad.r}" y2="${midY}" stroke="rgba(255,255,255,0.12)" stroke-dasharray="4 4"/>` +
        paths +
        (meanPath ? `<path d="${meanPath}" fill="none" stroke="#e0443a" stroke-width="2.4"/>` : "") +
      `</svg>` +
      `<div class="pm-traj-legend">` +
        `<span><i style="background:#e0443a;opacity:.35"></i>Each loss</span>` +
        `<span><i style="background:#e0443a"></i>Average</span>` +
        `<span><i style="background:rgba(255,255,255,.4);height:0;border-top:1px dashed rgba(255,255,255,.5);width:18px"></i>Even game</span>` +
      `</div>`
    );
  }

  function meanCurve(sample) {
    const n = 24;
    const acc = Array(n).fill(0);
    const cnt = Array(n).fill(0);
    (sample || []).forEach((f) => {
      const c = userCurve(f);
      if (c.length < 2) return;
      for (let i = 0; i < n; i++) {
        const x = (i / (n - 1)) * (c.length - 1);
        const j = Math.floor(x);
        const t = x - j;
        const v = c[j] * (1 - t) + c[Math.min(c.length - 1, j + 1)] * t;
        acc[i] += v;
        cnt[i] += 1;
      }
    });
    return acc.map((s, i) => (cnt[i] ? s / cnt[i] : 0.5));
  }

  function openingsHtml(openings) {
    const o = openings || {};
    const white = Array.isArray(o.white) ? o.white : [];
    const black = Array.isArray(o.black) ? o.black : [];
    if (!white.length && !black.length) return "";
    const hot = o.worst && o.worst.opening;
    const col = (rows, title) => {
      if (!rows || !rows.length) {
        return `<div class="pm-open-col"><h3>${title}</h3><p class="pm-lede">Not enough games.</p></div>`;
      }
      const body = rows.map((r) => {
        const isHot = hot && r.opening === hot && r.color === (o.worst && o.worst.color);
        const heat = Math.max(0, Math.min(1, r.loss_pct || 0));
        const bg = `rgba(224,68,58,${(0.12 + heat * 0.45).toFixed(2)})`;
        return (
          `<tr class="${isHot ? "is-hot" : ""}">` +
            `<td title="${escapeHtml(r.opening)}">${escapeHtml(shortOpening(r.opening))}</td>` +
            `<td>${escapeHtml(String(r.n))}</td>` +
            `<td><span class="pm-heat" style="background:${bg}">${pct(r.loss_pct)}</span></td>` +
          `</tr>`
        );
      }).join("");
      return (
        `<div class="pm-open-col">` +
          `<h3>${title}</h3>` +
          `<table class="pm-open-table">` +
            `<thead><tr><th>Opening</th><th>Games</th><th>Loss %</th></tr></thead>` +
            `<tbody>${body}</tbody>` +
          `</table>` +
        `</div>`
      );
    };
    return (
      `<section class="pm-section">` +
        `<h2>Openings</h2>` +
        `<p class="pm-lede">Loss rate, not score. The hot cell is the repertoire problem.</p>` +
        `<div class="pm-open-grid">${col(white, "As White")}${col(black, "As Black")}</div>` +
      `</section>`
    );
  }

  function tacticsHtml(tactics) {
    if (!tactics || !tactics.length) return "";
    const items = tactics.map((t) => {
      const board = t.moment ? momentFigure(t.moment) : "";
      return (
        `<div style="margin-bottom:22px">` +
          `<p class="pm-lede" style="margin-bottom:12px"><strong>${escapeHtml(t.caption)}</strong></p>` +
          board +
        `</div>`
      );
    }).join("");
    return (
      `<section class="pm-section">` +
        `<h2>Missed tactics</h2>` +
        `<p class="pm-lede">Not a histogram. The motif, then a position from your games.</p>` +
        items +
      `</section>`
    );
  }

  function habitsHtml(habits) {
    if (!habits || !habits.length) return "";
    const rows = habits.map((h) => (
      `<div class="pm-habit"><b>${escapeHtml(h.title)}</b><p>${escapeHtml(h.caption)}</p></div>`
    )).join("");
    return (
      `<section class="pm-section">` +
        `<h2>Habits</h2>` +
        `<p class="pm-lede">The repeating behaviour behind the tactics.</p>` +
        `<div class="pm-habits">${rows}</div>` +
      `</section>`
    );
  }

  function phaseHtml(phase) {
    const p = phase || {};
    const rows = p.rows || [];
    if (!rows.length) return "";
    const worstId = p.worst && p.worst.id;
    const bars = rows.map((r) => {
      const acc = Number(r.accuracy);
      const width = Number.isFinite(acc) ? Math.max(4, Math.round(acc)) : 0;
      return (
        `<div class="pm-phase-row ${r.id === worstId ? "is-worst" : ""}">` +
          `<span>${escapeHtml(r.label)}</span>` +
          `<div class="pm-phase-track"><span class="pm-phase-bar" style="width:${width}%"></span></div>` +
          `<b>${Number.isFinite(acc) ? `${acc.toFixed(0)}%` : "—"}</b>` +
        `</div>`
      );
    }).join("");
    return (
      `<section class="pm-section">` +
        `<h2>By phase</h2>` +
        `<p class="pm-lede">${escapeHtml(p.caption || "Accuracy by phase of the game.")}</p>` +
        `<div class="pm-phase-bars">${bars}</div>` +
      `</section>`
    );
  }

  function twinsHtml(twins) {
    if (!twins || !twins.win || !twins.loss) return "";
    return (
      `<section class="pm-section">` +
        `<h2>Twin games</h2>` +
        `<p class="pm-lede">${escapeHtml(twins.caption || "")}</p>` +
        `<div class="pm-twins">` +
          `<div class="pm-twin pm-twin--win"><h3>The win</h3>${momentFigure(twins.win)}</div>` +
          `<div class="pm-twin pm-twin--loss"><h3>The loss</h3>${momentFigure(twins.loss)}</div>` +
        `</div>` +
      `</section>`
    );
  }

  function momentsHtml(moments, title) {
    const list = (moments || []).filter((m) => m && m.fen).slice(0, 6);
    if (!list.length) return "";
    return (
      `<section class="pm-section">` +
        `<h2>${escapeHtml(title)}</h2>` +
        `<p class="pm-lede">From your games. Played move versus the one that held.</p>` +
        `<div class="pm-moments">${list.map(momentFigure).join("")}</div>` +
      `</section>`
    );
  }

  function momentFigure(m) {
    if (!m || !m.fen) return "";
    const color = m.user_color === "black" ? "black" : "white";
    const bestUci = m.best_uci || "";
    const playedUci = m.played_uci || "";
    return (
      `<figure class="pm-moment">` +
        `<div class="pm-board" data-fen="${escapeHtml(m.fen)}" data-color="${color}" ` +
          `data-best="${escapeHtml(bestUci)}" data-played="${escapeHtml(playedUci)}"></div>` +
        `<figcaption>` +
          `<div class="pm-moment-meta">${escapeHtml(m.opening || "")}` +
            `${m.opponent ? ` · vs ${escapeHtml(m.opponent)}` : ""}</div>` +
          `<p>${figCaption(m)}</p>` +
          `<div class="pm-moment-actions">` +
            (m.game_id
              ? `<button type="button" class="pm-link" data-pm-review="${escapeHtml(m.game_id)}"` +
                (m.ply ? ` data-pm-ply="${escapeHtml(m.ply)}"` : "") +
                `>Open game</button>`
              : "") +
          `</div>` +
        `</figcaption>` +
      `</figure>`
    );
  }

  function figCaption(m) {
    const played = m.san ? fig(m.san) : "";
    const best = m.best_san ? fig(m.best_san) : "";
    if (played && best && played !== best) {
      return `You played ${played}. ${best} was the move.`;
    }
    if (m.caption) {
      return fig(m.caption);
    }
    return played ? `You played ${played}.` : "A position from your games.";
  }

  function renderHow(n) {
    const h = n.how_you_win || {};
    const fixes = Array.isArray(h.fixes) ? h.fixes : [];
    const keep = Array.isArray(h.strengths) ? h.strengths : [];
    if (!fixes.length && !keep.length) {
      howEl.innerHTML = `<div class="pm-empty"><p class="pm-kicker">How to fix it</p><p>Generate a run with more games to get a practice plan.</p></div>`;
      return;
    }
    const fixBlocks = fixes.map((f, i) => {
      const figures = (f.moments || []).filter((m) => m && m.fen).slice(0, 2).map(momentFigure);
      const boards = figures.length
        ? `<div class="pm-fix-moments">${figures.join("")}</div>`
        : "";
      const kind = f.practice || "mistakes";
      return (
        `<article class="pm-fix">` +
          `<p class="pm-kicker">Fix ${i + 1}</p>` +
          `<h3>${escapeHtml(f.title)}</h3>` +
          `<p class="pm-lede">${escapeHtml(f.why)}</p>` +
          `<p class="pm-promise">${escapeHtml(f.promise)}</p>` +
          boards +
          `<div class="pm-cta-row">` +
            `<button type="button" class="pm-cta pm-cta--primary" data-pm-practice="${escapeHtml(kind)}">` +
              `Train this in ${escapeHtml(PRACTICE_LABEL[kind] || PRACTICE_LABEL.mistakes)}</button>` +
            (Number.isFinite(Number(f.n)) && Number(f.n) > 0
              ? `<span class="pm-cta-note">${escapeHtml(String(f.n))} positions queued</span>`
              : "") +
          `</div>` +
        `</article>`
      );
    }).join("");
    const keepBlock = keep.length
      ? `<section class="pm-keep"><h2>Keep doing this</h2><ul>${
          keep.map((s) => `<li><b>${escapeHtml(s.title)}</b>${escapeHtml(s.detail || "")}</li>`).join("")
        }</ul></section>`
      : "";
    howEl.innerHTML =
      `<div class="pm-wide">` +
        `<p class="pm-kicker">How to fix it</p>` +
        `<p class="pm-diagnosis" style="margin-top:0">Three things. This week.</p>` +
        fixBlocks +
        keepBlock +
      `</div>`;
  }

  // ── Spine ─────────────────────────────────────────────────────────────

  function drawSpine() {
    if (!spineEl) return;
    const s = narr().spine || {};
    const series = [
      { key: "all", color: "rgba(255,255,255,0.22)", pts: s.all || [] },
      { key: "wins", color: "#44d62c", pts: s.wins || [] },
      { key: "losses", color: "#e0443a", pts: s.losses || [] },
    ].filter((x) => x.pts.length > 1);
    if (!series.length) {
      spineEl.innerHTML = "";
      return;
    }
    const w = 52;
    const h = Math.max(320, (scrollEl && scrollEl.clientHeight) || 480);
    const pad = { t: 24, b: 24, l: 14, r: 10 };
    const innerH = h - pad.t - pad.b;
    const xAt = (v) => pad.l + Math.max(0, Math.min(1, v)) * (w - pad.l - pad.r);
    const yAt = (i, n) => pad.t + (i / (n - 1)) * innerH;
    const paths = series.map((ser) => {
      const d = ser.pts.map((v, i) => {
        const x = xAt(v).toFixed(1);
        const y = yAt(i, ser.pts.length).toFixed(1);
        return `${i === 0 ? "M" : "L"}${x} ${y}`;
      }).join(" ");
      return `<path d="${d}" fill="none" stroke="${ser.color}" stroke-width="${ser.key === "all" ? 1 : 1.8}"/>`;
    }).join("");
    const mid = xAt(0.5).toFixed(1);
    spineEl.title = "Average winning chances move by move — green: your wins, red: your losses.";
    spineEl.innerHTML =
      `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">` +
        `<line x1="${mid}" y1="${pad.t}" x2="${mid}" y2="${h - pad.b}" stroke="rgba(255,255,255,0.08)"/>` +
        paths +
      `</svg>`;
  }

  // ── Boards ────────────────────────────────────────────────────────────

  function destroyBoards() {
    grounds.forEach((g) => {
      try { g.destroy && g.destroy(); } catch { /* ignore */ }
    });
    grounds = [];
    if (boardObserver) {
      boardObserver.disconnect();
      boardObserver = null;
    }
  }

  function mountBoards() {
    destroyBoards();
    const nodes = $$(".pm-board[data-fen]", overlay);
    if (!nodes.length || !window.Chessground) return;
    let live = 0;
    boardObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const el = entry.target;
        if (entry.isIntersecting) {
          if (el._pmGround) return;
          // A tight budget meant boards evaporated as soon as you scrolled
          // past them and came back as bare placeholders. Keep the page's
          // worth alive; these are view-only and cheap.
          if (live >= 12) {
            const oldest = grounds.shift();
            if (oldest && oldest.el) {
              try { oldest.destroy && oldest.destroy(); } catch { /* ignore */ }
              oldest.el._pmGround = null;
              oldest.el.innerHTML = "";
              live -= 1;
            }
          }
          el._pmGround = mountOne(el);
          if (el._pmGround) {
            grounds.push(el._pmGround);
            live += 1;
          }
        }
      });
    }, { root: scrollEl, rootMargin: "80px", threshold: 0.15 });
    nodes.forEach((el) => boardObserver.observe(el));
  }

  function mountOne(el) {
    const fen = el.dataset.fen;
    if (!fen) return null;
    let wrap = el.querySelector(".cg-wrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.className = "cg-wrap";
      el.appendChild(wrap);
    }
    const orientation = el.dataset.color === "black" ? "black" : "white";
    const shapes = [];
    const played = el.dataset.played || "";
    const best = el.dataset.best || "";
    if (played.length >= 4) {
      shapes.push({ orig: played.slice(0, 2), dest: played.slice(2, 4), brush: "red" });
    }
    if (best.length >= 4) {
      shapes.push({ orig: best.slice(0, 2), dest: best.slice(2, 4), brush: "green" });
    }
    const ground = window.Chessground(wrap, {
      fen,
      orientation,
      viewOnly: true,
      coordinates: false,
      drawable: { enabled: false, visible: true, autoShapes: shapes },
    });
    ground.el = el;
    requestAnimationFrame(() => {
      try { ground.redrawAll && ground.redrawAll(); } catch { /* ignore */ }
    });
    return ground;
  }

  // ── Events ────────────────────────────────────────────────────────────

  overlay.addEventListener("click", (e) => {
    const sec = e.target.closest("[data-pm-sec]");
    if (sec) {
      const target = document.getElementById(sec.dataset.pmSec);
      if (target && scrollEl) {
        const delta = target.getBoundingClientRect().top - scrollEl.getBoundingClientRect().top;
        scrollEl.scrollTo({ top: scrollEl.scrollTop + delta - 16, behavior: "smooth" });
      }
      // Mark it now rather than waiting for the smooth scroll to land, so the
      // click has an immediate answer.
      $$("button[data-pm-sec]", secnavEl).forEach((b) => b.classList.toggle("is-on", b === sec));
      return;
    }
    const go = e.target.closest("[data-pm-go]");
    if (go) {
      open(go.dataset.pmGo);
      return;
    }
    const tab = e.target.closest("[data-pm]");
    if (tab && overlay.contains(tab) && tab.dataset.pm && !tab.closest(".pm-screen")) {
      open(tab.dataset.pm);
      return;
    }
    if (e.target.closest("#pm-close")) {
      close();
      return;
    }
    const practice = e.target.closest("[data-pm-practice]");
    if (practice) {
      close({ silent: true });
      if (window.__insightsGoPractice) window.__insightsGoPractice(practice.dataset.pmPractice);
      return;
    }
    const review = e.target.closest("[data-pm-review]");
    if (review) {
      close({ silent: true });
      if (window.__insightsGoReview) {
        window.__insightsGoReview(review.dataset.pmReview, review.dataset.pmPly);
      }
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (overlay.classList.contains("hidden")) return;
    e.preventDefault();
    close();
  });

  if (scrollEl) {
    let ticking = false;
    scrollEl.addEventListener("scroll", () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        syncSecnav();
      });
    }, { passive: true });
  }

  window.addEventListener("resize", () => {
    if (!overlay.classList.contains("hidden")) pinToHeader();
  });

  async function route(pathname) {
    const parsed = parsePath(pathname || window.location.pathname);
    if (!parsed) {
      if (!overlay.classList.contains("hidden")) close({ silent: true });
      return false;
    }
    if (parsed.tab === "deep") {
      close({ silent: true });
      if (window.__insightsOpenDeepDive) window.__insightsOpenDeepDive();
      return true;
    }
    if (parsed.runId && parsed.runId !== runId() && window.__insightsLoadRun) {
      try {
        await window.__insightsLoadRun(parsed.runId);
      } catch {
        return false;
      }
    }
    open(parsed.tab, { push: false });
    return true;
  }

  window.__postmortemOpen = open;
  window.__postmortemClose = close;
  window.__postmortemAdopt = adopt;
  window.__postmortemRoute = route;
  window.__postmortemIsOpen = () => !overlay.classList.contains("hidden");
})();
