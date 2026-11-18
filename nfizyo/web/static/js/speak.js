/* N-Fizyo: Text vorlesen mit der Stimme des eigenen Geräts (Web Speech API).
   Es werden nur Stimmen benutzt, die auf dem Gerät selbst laufen (localService) – der Text geht nicht ins Internet.
   Knopf: <button data-speak="ID-des-Textes" data-lang="tr" hidden> – er erscheint nur, wenn das Gerät eine Stimme für die
   Sprache hat (sonst bleibt er unsichtbar, kein deaktivierter Knopf mit Erklärung). */
(function () {
  'use strict';
  var buttons = Array.prototype.slice.call(document.querySelectorAll('[data-speak]'));
  if (!buttons.length) return;
  if (!('speechSynthesis' in window) || typeof SpeechSynthesisUtterance === 'undefined') {
    buttons.forEach(function (b) { b.hidden = true; });
    return;
  }
  var ALIAS = { bks: ['bs', 'hr', 'sr'], gsw: ['de'], uk: ['uk'], sq: ['sq'] };
  var synth = window.speechSynthesis;
  var current = null;

  function voiceFor(lang) {
    var wanted = (ALIAS[lang] || [lang]).map(function (c) { return c.toLowerCase(); });
    var voices = synth.getVoices().filter(function (v) { return v.localService; });
    for (var i = 0; i < wanted.length; i++) {
      var exact = voices.filter(function (v) { return v.lang.toLowerCase().split(/[-_]/)[0] === wanted[i]; });
      if (exact.length) return exact[0];
    }
    return null;
  }

  function refresh() {
    buttons.forEach(function (b) {
      b.hidden = !voiceFor(b.getAttribute('data-lang') || document.documentElement.lang);
    });
  }

  function stop() {
    synth.cancel();
    if (current) { current.setAttribute('aria-pressed', 'false'); current = null; }
  }

  buttons.forEach(function (b) {
    b.setAttribute('aria-pressed', 'false');
    b.addEventListener('click', function () {
      var wasMe = current === b;
      stop();
      if (wasMe) return;
      var el = document.getElementById(b.getAttribute('data-speak'));
      var lang = b.getAttribute('data-lang') || document.documentElement.lang;
      var voice = voiceFor(lang);
      if (!el || !voice) return;
      var u = new SpeechSynthesisUtterance(el.innerText || el.textContent);
      u.voice = voice;
      u.lang = voice.lang;
      u.rate = 0.9;
      u.onend = u.onerror = function () { if (current === b) stop(); };
      current = b;
      b.setAttribute('aria-pressed', 'true');
      synth.speak(u);
    });
  });
  refresh();
  if (typeof synth.onvoiceschanged !== 'undefined') synth.onvoiceschanged = refresh;
  window.addEventListener('pagehide', stop);
})();
