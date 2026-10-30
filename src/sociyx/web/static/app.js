/* sociyx web UI: tiny glue, no inline scripts (CSP). */
(function () {
  "use strict";

  // Every htmx request carries the per-session CSRF token.
  document.addEventListener("htmx:configRequest", function (e) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) { e.detail.headers["X-CSRF-Token"] = meta.content; }
  });

  // Never leave silently while a deletion is running.
  window.addEventListener("beforeunload", function (e) {
    if (document.querySelector('[data-running="true"]')) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  // Keyboard: "/" focuses search, "?" opens help (not while typing in a field).
  document.addEventListener("keydown", function (e) {
    var t = e.target, tag = t && t.tagName;
    if (e.ctrlKey || e.metaKey || e.altKey || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") { return; }
    if (e.key === "/") {
      var q = document.getElementById("f-q");
      if (q) { e.preventDefault(); q.focus(); }
    } else if (e.key === "?") {
      window.location.href = "/help";
    }
  });
})();
