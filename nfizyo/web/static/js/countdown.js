// Restzeit (z. B. Supportzugang): <span data-countdown="Sekunden">H:MM:SS</span>. Bei 0 Seite neu laden (Zustand vom Server).
(function () {
  "use strict";
  var items = Array.prototype.slice.call(document.querySelectorAll("[data-countdown]"));
  if (!items.length) return;
  var start = Date.now();
  var reloaded = false;
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    var gone = Math.floor((Date.now() - start) / 1000);
    items.forEach(function (el) {
      var left = Math.max(0, parseInt(el.getAttribute("data-countdown"), 10) - gone);
      el.textContent = Math.floor(left / 3600) + ":" + pad(Math.floor((left % 3600) / 60)) + ":" + pad(left % 60);
      if (left === 0 && !reloaded) { reloaded = true; setTimeout(function () { window.location.reload(); }, 5000); }
    });
  }
  tick();
  setInterval(tick, 1000);
})();
