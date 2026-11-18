/* N-Fizyo Kasse: Total, Erhalten und Rückgeld laufend anzeigen, während Mengen eingegeben werden.
   Bar wird auf 5 Rappen gerundet (wie der Server, core/money.py). Nur Anzeige – beim Verkaufen rechnet der Server alles neu.
   Ohne JavaScript: Knopf «Total berechnen». */
(function () {
  'use strict';
  var form = document.querySelector('[data-cash-form]');
  if (!form) return;
  var box = form.querySelector('[data-cash-total]');
  if (!box) return;

  function num(v) {
    var n = parseFloat(String(v || '').replace(',', '.').replace(/['’\s]/g, ''));
    return isFinite(n) ? n : null;
  }
  function chf(v) {
    var neg = v < 0;
    var s = Math.abs(v).toFixed(2).split('.');
    s[0] = s[0].replace(/\B(?=(\d{3})+(?!\d))/g, '’');
    return (neg ? '−' : '') + 'CHF ' + s.join('.');
  }
  function update() {
    var total = 0;
    form.querySelectorAll('input[data-price]').forEach(function (inp) {
      var q = num(inp.value);
      if (q && q > 0 && !inp.disabled) total += Math.round(q * num(inp.getAttribute('data-price')) * 100) / 100;
    });
    total = Math.round(total * 100) / 100;
    var method = form.querySelector('input[name="method"]:checked');
    var cash = !method || method.value === 'cash';
    var rounding = 0;
    if (cash) {
      var rounded = Math.round(Math.round(total * 100) / 5) * 5 / 100;   // 5 Rappen, .025 → .05
      rounding = Math.round((rounded - total) * 100) / 100;
      total = rounded;
    }
    box.querySelectorAll('[data-rounding-row]').forEach(function (el) { el.hidden = !(cash && rounding); });
    var roundOut = box.querySelector('[data-rounding]');
    if (roundOut) roundOut.textContent = rounding ? (rounding > 0 ? '+' : '−') + Math.abs(rounding).toFixed(2) : '';
    var given = num((form.querySelector('[name="given"]') || {}).value);
    box.querySelector('[data-total]').textContent = chf(total);
    var givenOut = box.querySelector('[data-given]');
    var changeOut = box.querySelector('[data-change]');
    var changeRow = box.querySelectorAll('[data-cash-only]');
    changeRow.forEach(function (el) { if (!el.hasAttribute('data-rounding-row')) el.hidden = !cash; });
    givenOut.textContent = given !== null && cash ? chf(given) : '–';
    if (given !== null && cash && total > 0) {
      var change = Math.round((given - total) * 100) / 100;
      changeOut.textContent = chf(change);
      changeOut.classList.toggle('text-error', change < 0);
    } else {
      changeOut.textContent = '–';
      changeOut.classList.remove('text-error');
    }
  }
  var preview = form.querySelector('[data-preview]');
  if (preview) preview.hidden = true;
  form.addEventListener('keydown', function (e) {   // Enter in einem Feld verkauft nicht aus Versehen
    if (e.key === 'Enter' && e.target.matches('input')) e.preventDefault();
  });
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  update();
})();
