/* N-Fizyo Patienteninfo-Karte: Standardtext einfügen, auf KI-Vorschlag warten. */
(function () {
  var card = document.getElementById('pi-card');
  if (!card) return;
  var btn = card.querySelector('[data-pi-default]');
  if (btn) btn.addEventListener('click', function () {
    var ta = card.querySelector('#pi_contact');
    ta.value = (ta.value ? ta.value.trim() + ' ' : '') + btn.getAttribute('data-pi-default');
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
  var wait = card.querySelector('[data-pi-wait]');
  if (!wait) return;
  var tries = 0;
  function tick() {
    tries++;
    NF.fetch(wait.getAttribute('data-pi-wait'), { headers: { 'Accept': 'application/json' } }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.done) { if (tries < 120) setTimeout(tick, 2500); return; }
      var h = card.querySelector('#pi_home'), c = card.querySelector('#pi_contact');
      if (!h.value) h.value = d.home; if (!c.value) c.value = d.contact;
      [h, c].forEach(function (x) { x.dispatchEvent(new Event('input', { bubbles: true })); });
      wait.remove();
    }).catch(function () { if (tries < 120) setTimeout(tick, 5000); });
  }
  setTimeout(tick, 1500);
})();
