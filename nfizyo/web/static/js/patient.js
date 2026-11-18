/* N-Fizyo Patienten-App: Offline-Hülle (Service Worker) registrieren. */
(function () {
  'use strict';
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/p/sw.js', { scope: '/p/' }).catch(function () {});
  }
})();

/* Untere Leiste: Beschriftung 14 px; nur wenn ein Wort nicht passt (z. B. «Appointments»), etwas kleiner statt abgeschnitten. */
(function () {
  'use strict';
  var labels = Array.prototype.slice.call(document.querySelectorAll('.papp-nav a .ellipsis'));
  if (!labels.length) return;
  function fit() {
    labels.forEach(function (el) {
      el.style.fontSize = '';
      var size = 14;
      while (el.scrollWidth > el.clientWidth + 1 && size > 12) {
        size -= 0.5;
        el.style.fontSize = size + 'px';
      }
    });
  }
  fit();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
  window.addEventListener('resize', fit);
})();
