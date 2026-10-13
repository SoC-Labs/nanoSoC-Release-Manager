/* nanoSoC Release Manager: small page helpers. No data; every page works without this file (except live job
   polling, which is htmx). Copy buttons, the command bar's keys, remembered <details> state, the theme switch. */
(function () {
  "use strict";
  function store(fn, fallback) { try { return fn(); } catch (e) { return fallback; } }

  /* Copy buttons: data-copy="<id of the element whose text to copy>". */
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest("[data-copy]");
    if (!btn) return;
    var src = document.getElementById(btn.getAttribute("data-copy"));
    if (!src || !navigator.clipboard) return;
    navigator.clipboard.writeText(src.textContent).then(function () {
      var old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = old; }, 1500);
    });
  });

  /* Keep a job log scrolled to its end while it grows. */
  document.addEventListener("htmx:afterSwap", function (ev) {
    ev.target.querySelectorAll && ev.target.querySelectorAll("pre.log").forEach(function (p) { p.scrollTop = p.scrollHeight; });
  });

  /* "/" focuses the command bar (unless typing in a field); Escape closes its hits. */
  document.addEventListener("keydown", function (ev) {
    var t = ev.target, typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    var cmd = document.getElementById("cmd"), hits = document.getElementById("hits");
    if (ev.key === "/" && !typing && cmd) { ev.preventDefault(); cmd.focus(); cmd.select(); }
    else if (ev.key === "Escape" && hits && hits.innerHTML) { hits.innerHTML = ""; if (cmd) cmd.focus(); }
  });

  function ready(fn) { if (document.readyState !== "loading") fn(); else document.addEventListener("DOMContentLoaded", fn); }
  ready(function () {
    /* The rail is a menu on a narrow screen: closed until opened. */
    var rail = document.querySelector("details[data-rail]");
    if (rail && window.matchMedia && window.matchMedia("(max-width: 900px)").matches) rail.removeAttribute("open");

    /* Remember each <details id=...> open state per URL for this tab (sessionStorage; no data). */
    var key = "rm-details:" + location.pathname + location.search;
    var saved = store(function () { return JSON.parse(sessionStorage.getItem(key) || "{}"); }, {});
    document.querySelectorAll("details[id]").forEach(function (d) {
      if (Object.prototype.hasOwnProperty.call(saved, d.id)) d.open = !!saved[d.id];
      d.addEventListener("toggle", function () {
        saved[d.id] = d.open;
        store(function () { sessionStorage.setItem(key, JSON.stringify(saved)); });
      });
    });

    /* Theme: follow the system unless the person picked one (localStorage, per browser). */
    var btn = document.querySelector("[data-theme-toggle]");
    var root = document.documentElement;
    var pick = store(function () { return localStorage.getItem("rm-theme"); }, null);
    if (pick === "dark" || pick === "light") root.setAttribute("data-theme", pick);
    function isDark() {
      var t = root.getAttribute("data-theme");
      return t ? t === "dark" : !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    }
    function show() { if (btn) { btn.setAttribute("aria-pressed", isDark() ? "true" : "false"); } }
    if (btn) {
      btn.hidden = false;
      show();
      btn.addEventListener("click", function () {
        var next = isDark() ? "light" : "dark";
        root.setAttribute("data-theme", next);
        store(function () { localStorage.setItem("rm-theme", next); });
        show();
      });
    }
  });
})();
