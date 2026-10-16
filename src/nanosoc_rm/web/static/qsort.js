/* Sortable tables (the quality pages): click a header to sort by it. Served from /static: no inline script (CSP). */
document.querySelectorAll("table.sortable").forEach(function (t) {
  t.querySelectorAll("th").forEach(function (th, i) {
    th.addEventListener("click", function () {
      var asc = th.getAttribute("aria-sort") !== "ascending";
      t.querySelectorAll("th").forEach(function (h) { h.removeAttribute("aria-sort"); });
      th.setAttribute("aria-sort", asc ? "ascending" : "descending");
      var body = t.tBodies[0], rows = Array.prototype.slice.call(body.rows);
      rows.sort(function (a, b) {
        var x = a.cells[i].getAttribute("data-v") || a.cells[i].textContent.trim();
        var y = b.cells[i].getAttribute("data-v") || b.cells[i].textContent.trim();
        var nx = parseFloat(x), ny = parseFloat(y);
        var c = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : x.localeCompare(y);
        return asc ? c : -c;
      });
      rows.forEach(function (r) { body.appendChild(r); });
    });
  });
});
