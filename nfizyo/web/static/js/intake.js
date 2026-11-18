/* Anliegen: Körperstellen antippen (mehrere), eigene Sprache sprechen (lokale Spracherkennung). */
(function () {
  'use strict';
  var picker = document.querySelector('[data-region-picker]');
  if (picker) {
    var fig = picker.querySelector('[data-bodyfig]');
    fig.setAttribute('data-pick-custom', '1');
    var summary = picker.querySelector('[data-region-summary]');
    var emptyText = summary.textContent;
    function box(region) { return picker.querySelector('[data-region-box][value="' + region + '"]'); }
    function sync() {
      var names = [];
      picker.querySelectorAll('[data-region-box]').forEach(function (b) {
        picker.querySelectorAll('[data-region="' + b.value + '"]').forEach(function (p) { p.classList.toggle('hl-sel', b.checked); });
        if (b.checked) names.push(b.parentNode.textContent.trim());
      });
      summary.textContent = names.length ? names.join(', ') : emptyText;
    }
    fig.addEventListener('nf:pick', function (e) {
      if (!e.detail.region) return;
      var b = box(e.detail.region);
      if (b) { b.checked = !b.checked; sync(); }
    });
    picker.addEventListener('change', function (e) { if (e.target.matches('[data-region-box]')) sync(); });
    sync();
  }

  var vbtn = document.querySelector('[data-voice]');
  if (vbtn && navigator.mediaDevices && window.MediaRecorder) {
    var status = document.querySelector('[data-voice-status]');
    var label = vbtn.querySelector('span');
    var text = document.getElementById('text');
    var rec = null, chunks = [], stream = null;
    vbtn.addEventListener('click', function () {
      if (rec && rec.state === 'recording') { rec.stop(); return; }
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
        stream = s; chunks = [];
        rec = new MediaRecorder(s);
        rec.ondataavailable = function (e) { if (e.data.size) chunks.push(e.data); };
        rec.onstop = function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          vbtn.classList.remove('recording'); label.textContent = vbtn.dataset.labelBusy; vbtn.disabled = true;
          var fd = new FormData();
          fd.append('audio', new Blob(chunks, { type: (rec.mimeType || 'audio/webm').split(';')[0] }), 'stimme');
          NF.fetch(vbtn.dataset.voice, { method: 'POST', body: fd }).then(function (r) { return r.json(); }).then(function (j) {
            if (j.text) text.value = (text.value ? text.value + ' ' : '') + j.text;
            text.dispatchEvent(new Event('input', { bubbles: true }));
          }).catch(function () {}).then(function () { vbtn.disabled = false; label.textContent = vbtn.dataset.labelStart; });
        };
        rec.start();
        vbtn.classList.add('recording'); label.textContent = vbtn.dataset.labelStop;
      }).catch(function () { if (status) status.textContent = '🎤 ✕'; });
    });
  }
})();
