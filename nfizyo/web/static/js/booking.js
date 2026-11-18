/* N-Fizyo Online-Anfragen: wer im Suchfeld tippt, wählt damit «andere bestehende Person» (Radio-Knopf) aus. */
(function () {
  'use strict';
  function pick(e) {
    var field = e.target.closest('[data-choose]');
    if (!field) return;
    var radio = document.getElementById(field.getAttribute('data-choose'));
    if (radio) radio.checked = true;
  }
  document.addEventListener('focusin', pick);
  document.addEventListener('input', pick);
})();
