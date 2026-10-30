// Adds a "Copy" button to code blocks. The site works without it.
(function () {
  if (!navigator.clipboard) return;
  document.querySelectorAll(".code").forEach(function (box) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy commands to clipboard");
    btn.addEventListener("click", function () {
      var text = box.querySelector("code").innerText;
      navigator.clipboard.writeText(text).then(function () {
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = "Copy"; }, 1800);
      }, function () {
        btn.textContent = "Copy failed";
      });
    });
    box.appendChild(btn);
  });
})();
