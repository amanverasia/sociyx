// Plays the illustrated run once. Without JS, or with reduced motion, the final state in the HTML stays as is.
(function () {
  var d = document.getElementById("demo");
  if (!d) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  var rows = Array.prototype.slice.call(d.querySelectorAll(".post"));
  var stage = d.querySelector(".stage"), cmd = d.querySelector(".typed"), clock = d.querySelector(".clock"), replay = d.querySelector(".replay");
  var labels = { v: "deleted, verified", u: "deleted, unverified", f: "failed", s: "skipped, edited since preview" };
  var sel = rows.filter(function (r) { return r.dataset.sel; });
  var run = 0;
  function wait(ms, id) { return new Promise(function (ok) { setTimeout(function () { ok(id === run); }, ms); }); }
  function fmt(s) { var p = function (n) { return (n < 10 ? "0" : "") + n; }; return "00:" + p(Math.floor(s / 60)) + ":" + p(s % 60); }
  function reset() {
    rows.forEach(function (r) {
      r.classList.remove("sel", "dim", "active", "struck");
      r.removeAttribute("data-out");
      r.querySelector(".res").textContent = r.dataset.sel ? "" : r.dataset.note;
    });
    cmd.textContent = "";
    clock.textContent = fmt(0);
    stage.textContent = "Scanning your history (read-only)";
  }
  async function play() {
    var id = ++run, t = 0;
    replay.hidden = true;
    reset();
    if (!(await wait(1400, id))) return;
    rows.forEach(function (r) { r.classList.add(r.dataset.sel ? "sel" : "dim"); });
    stage.textContent = "Filter matches " + sel.length + " of " + rows.length + ". Export saved first. Nothing deleted yet.";
    if (!(await wait(1800, id))) return;
    stage.textContent = "Waiting for you to type the confirmation";
    var word = "delete " + sel.length;
    for (var i = 1; i <= word.length; i++) { cmd.textContent = word.slice(0, i); if (!(await wait(110, id))) return; }
    if (!(await wait(700, id))) return;
    for (var k = 0; k < sel.length; k++) {
      var r = sel[k];
      r.classList.add("active");
      stage.textContent = "Deleting " + (k + 1) + " of " + sel.length + ". Real runs wait here between items.";
      r.querySelector(".res").textContent = "working";
      if (!(await wait(1100, id))) return;
      t += 45; clock.textContent = fmt(t);
      var o = r.dataset.out_;
      r.dataset.out = o;
      r.classList.remove("active");
      r.querySelector(".res").textContent = labels[o];
      if (o === "v" || o === "u") r.classList.add("struck");
      if (!(await wait(350, id))) return;
    }
    stage.textContent = "Done: 2 deleted and verified, 1 unverified, 1 failed, 1 skipped.";
    replay.hidden = false;
  }
  rows.forEach(function (r) { r.dataset.out_ = r.dataset.out || ""; r.dataset.note = r.querySelector(".res").textContent; });
  replay.addEventListener("click", play);
  play();
})();
