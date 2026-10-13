/* nanoSoC Release Manager: small page helpers. No data; every page works without this file. */
document.addEventListener("click", function (ev) {
  var btn = ev.target.closest("[data-copy]");
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
