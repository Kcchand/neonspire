// static/js/main.js
(function () {
  // -------------------------------
  // THEME (sync with localStorage)
  // -------------------------------
  const THEME_KEY = "theme";
  const root = document.documentElement;
  const savedTheme = localStorage.getItem(THEME_KEY);
  if (savedTheme === "light") root.dataset.theme = "light";

  function wireThemeToggle(id) {
    const t = document.getElementById(id);
    if (!t) return;
    // checked = dark
    t.checked = root.dataset.theme !== "light";
    t.addEventListener("change", () => {
      if (t.checked) {
        root.dataset.theme = "dark";
        localStorage.setItem(THEME_KEY, "dark");
      } else {
        root.dataset.theme = "light";
        localStorage.setItem(THEME_KEY, "light");
      }
    });
  }
  wireThemeToggle("themeToggleTop");

  // -------------------------------------------
  // SMOOTH SCROLL for in‑page #anchor links
  // -------------------------------------------
  document.querySelectorAll('a[href^="#"]').forEach((a) => {
    a.addEventListener("click", (e) => {
      const id = a.getAttribute("href").slice(1);
      if (!id) return;
      const el = document.getElementById(id);
      if (el) {
        e.preventDefault();
        el.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });

  // ---------------------------------------------------
  // PREFILL FORMS from per‑game Deposit/Withdraw buttons
  // ---------------------------------------------------
  function smoothTo(id) {
    const el = document.getElementById(id);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const depForm = document.getElementById("depositForm");
  const wdForm = document.getElementById("withdrawForm");

  // Deposit buttons on cards
  document.querySelectorAll(".js-deposit").forEach((btn) => {
    btn.addEventListener("click", () => {
      const gameId = btn.getAttribute("data-game") || "";
      const hid = document.getElementById("depositGameId");
      if (hid) hid.value = gameId;
      smoothTo("add-funds");
      // Focus amount for faster input
      const amt = document.getElementById("depAmount");
      if (amt) setTimeout(() => amt.focus(), 220);
    });
  });

  // Withdraw buttons on cards
  document.querySelectorAll(".js-withdraw").forEach((btn) => {
    btn.addEventListener("click", () => {
      const gameId = btn.getAttribute("data-game") || "";
      const hid = document.getElementById("withdrawGameId");
      if (hid) hid.value = gameId;
      smoothTo("withdraw");
      const amt = document.getElementById("wdAmount");
      if (amt) setTimeout(() => amt.focus(), 220);
    });
  });

  // -------------------------------
  // BUTTON RIPPLE (progressive UX)
  // -------------------------------
  // CSS is required (see snippet below).
  // We keep ripples performant and short‑lived.
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".btn, .btn-mini, .btn-auth, .btn-auth-primary");
    if (!b) return;

    // Ensure container is positioned for absolute ripple
    if (!b.classList.contains("ripple-ready")) {
      b.classList.add("ripple-ready");
      // If site CSS doesn’t already do this:
      if (!b.style.position) b.style.position = "relative";
      if (!b.style.overflow) b.style.overflow = "hidden";
    }

    const r = document.createElement("span");
    r.className = "ripple";
    const rect = b.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    r.style.width = r.style.height = size + "px";
    r.style.left = e.clientX - rect.left - size / 2 + "px";
    r.style.top = e.clientY - rect.top - size / 2 + "px";
    b.appendChild(r);

    // Clean up quickly
    setTimeout(() => {
      r.remove();
    }, 500);
  });
})();