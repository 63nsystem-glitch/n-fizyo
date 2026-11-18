/* N-Fizyo MTT-Fläche: Live-Ansicht alle 20 Sekunden neu laden (nur wenn die Seite sichtbar ist). */
(function () {
  'use strict';
  var box = document.getElementById('mtt-live');
  if (!box || !window.fetch) return;
  var url = box.getAttribute('data-refresh');
  function load() {
    if (document.hidden) return;
    var active = document.activeElement;
    if (active && box.contains(active) && active.tagName !== 'BODY') return;   // nicht stören, während jemand klickt
    fetch(url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) { if (html !== null) box.innerHTML = html; })
      .catch(function () {});
  }
  setInterval(load, 20000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) load(); });
})();
