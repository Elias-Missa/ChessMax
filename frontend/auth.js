/* ChessMax auth gate.

The #auth-overlay is visible by default (markup carries no `hidden` class) so it
covers the app on first paint. On load we check the session; if valid we hide the
overlay and reveal the user chip, otherwise we show the login/signup form. On a
successful login/register we reload so both apps boot cleanly against the new
session cookie. */
(function () {
  "use strict";

  const overlay = document.getElementById("auth-overlay");
  const form = document.getElementById("auth-form");
  const emailEl = document.getElementById("auth-email");
  const passEl = document.getElementById("auth-password");
  const confirmEl = document.getElementById("auth-confirm");
  const errEl = document.getElementById("auth-error");
  const submitBtn = document.getElementById("auth-submit");
  const subtitle = document.getElementById("auth-subtitle");
  const toggleText = document.getElementById("auth-toggle-text");
  const toggleBtn = document.getElementById("auth-toggle-btn");
  const userWrap = document.getElementById("shell-user-wrap");
  const userEmail = document.getElementById("shell-user");
  const logoutBtn = document.getElementById("logout-btn");

  let mode = "login"; // "login" | "register"

  function showError(msg) {
    errEl.textContent = msg;
    errEl.classList.remove("hidden");
  }

  // FastAPI's `detail` is a string for app errors, but a list of objects for
  // request-validation errors — render those readably instead of
  // "[object Object]".
  function detailToMessage(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && d.msg ? d.msg : ""))
        .filter(Boolean)
        .join(" · ");
    }
    return "";
  }
  function clearError() {
    errEl.textContent = "";
    errEl.classList.add("hidden");
  }

  function applyMode() {
    const login = mode === "login";
    subtitle.textContent = login ? "Sign in to your account" : "Create your account";
    submitBtn.textContent = login ? "Log in" : "Sign up";
    toggleText.textContent = login ? "No account?" : "Already have an account?";
    toggleBtn.textContent = login ? "Create one" : "Log in";
    passEl.setAttribute("autocomplete", login ? "current-password" : "new-password");
    confirmEl.classList.toggle("hidden", login);
    if (login) confirmEl.value = "";
    clearError();
  }

  toggleBtn.addEventListener("click", () => {
    mode = mode === "login" ? "register" : "login";
    applyMode();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();
    const email = emailEl.value.trim();
    const password = passEl.value;
    if (!email || !password) {
      showError("Email and password are required.");
      return;
    }
    if (mode === "register") {
      if (password.length < 6) {
        showError("Password must be at least 6 characters.");
        return;
      }
      if (password !== confirmEl.value) {
        showError("Passwords don't match.");
        return;
      }
    }
    submitBtn.disabled = true;
    const path = mode === "login" ? "/api/auth/login" : "/api/auth/register";
    try {
      const resp = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(detailToMessage(data.detail) || "Authentication failed.");
      }
      location.reload();
    } catch (err) {
      showError(err.message);
      submitBtn.disabled = false;
    }
  });

  if (logoutBtn) {
    logoutBtn.addEventListener("click", async () => {
      try {
        await fetch("/api/auth/logout", { method: "POST" });
      } catch (err) {
        /* best effort */
      }
      location.reload();
    });
  }

  async function boot() {
    try {
      const resp = await fetch("/api/auth/me");
      if (resp.ok) {
        const me = await resp.json();
        overlay.classList.add("hidden");
        if (userWrap) userWrap.classList.remove("hidden");
        if (userEmail) userEmail.textContent = me.email || "";
        // Let other modules (e.g. the Library migration) react to login.
        document.dispatchEvent(new CustomEvent("chessmax:authenticated", { detail: me }));
        return;
      }
    } catch (err) {
      /* fall through to showing the form */
    }
    overlay.classList.remove("hidden");
    emailEl.focus();
  }

  async function init() {
    // First run (no accounts yet) → default to the signup form, so the very
    // first user isn't stuck on a login that can't succeed.
    try {
      const resp = await fetch("/api/auth/status");
      if (resp.ok) {
        const data = await resp.json();
        if (!data.has_accounts) mode = "register";
      }
    } catch (err) {
      /* default to login */
    }
    applyMode();
    await boot();
  }

  init();
})();
