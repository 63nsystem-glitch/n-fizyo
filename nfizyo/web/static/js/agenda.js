/* N-Fizyo Agenda auf dem Telefon: Die Woche ist zu breit – ohne ausdrückliche Wahl gleich die Tagesansicht zeigen. */
(function () {
  'use strict';
  var box = document.querySelector('[data-agenda-auto-day]');
  if (!box || !window.matchMedia || !window.matchMedia('(max-width: 599px)').matches) return;
  var q = new URLSearchParams(window.location.search);
  q.set('view', 'tag');
  window.location.replace(window.location.pathname + '?' + q.toString());
})();

/* N-Fizyo Agenda: Klick auf eine freie Stelle einer Spalte öffnet «Neuer Termin» mit Tag, Zeit und Therapeut/in.
   «Neuer Termin»: «Titel des Blockers» nur bei «Kein Patient», das Patientenfeld nur ohne Blocker. */
(function () {
  'use strict';
  var cfg = document.querySelector('[data-agenda-new]');
  if (!cfg) return;
  document.addEventListener('click', function (e) {
    var col = e.target.closest('[data-agenda-col]');
    if (!col || e.target.closest('.appt')) return;
    var r = col.getBoundingClientRect();
    var pxMin = parseFloat(col.dataset.pxMin), h0 = parseInt(col.dataset.startHour, 10);
    var minutes = Math.floor((e.clientY - r.top) / pxMin / 15) * 15;
    var hh = h0 + Math.floor(minutes / 60), mm = minutes % 60;
    var time = (hh < 10 ? '0' : '') + hh + ':' + (mm < 10 ? '0' : '') + mm;
    var q = new URLSearchParams({ date: col.dataset.day, time: time, therapist_id: col.dataset.therapist || '',
                                  location_id: cfg.getAttribute('data-location') || '' });
    window.location.href = cfg.getAttribute('data-agenda-new') + '?' + q.toString();
  });
})();

(function () {
  'use strict';
  function sync() {
    var box = document.querySelector('input[name="blocker"]');
    if (!box) return;
    document.querySelectorAll('[data-blocker-only]').forEach(function (el) { el.hidden = !box.checked; });
    document.querySelectorAll('[data-patient-only]').forEach(function (el) { el.hidden = box.checked; });
  }
  document.addEventListener('change', function (e) {
    if (e.target.matches('input[name="blocker"]')) sync();
  });
  document.addEventListener('DOMContentLoaded', sync);
})();
