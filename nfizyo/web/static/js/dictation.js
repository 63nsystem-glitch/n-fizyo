/* N-Fizyo Diktat: Aufnahme im Browser, Zwischenspeicher im Gerät (IndexedDB), Hochladen sobald Netz da ist.
   Nichts geht an fremde Dienste – Ziel ist nur die Praxis-Box. */
(function () {
  'use strict';
  var DB_NAME = 'nfizyo-diktat', STORE = 'queue';
  var root = document.querySelector('[data-recorder]');
  var meta = document.querySelector('meta[name="nf-user"]');
  var me = root ? root.dataset.user || '' : (meta ? meta.content : '');
  // Geteiltes Tablet: Aufnahmen gehören der Person, die sie gemacht hat – nie unter einem anderen Konto hochladen
  function mine(item) { return !item.user || item.user === me; }
  var queueEl = document.querySelector('[data-queue]');
  var T = {};
  try { T = JSON.parse((document.getElementById('nf-dict-t') || {}).textContent || '{}'); } catch (e) { T = {}; }

  function t(k, fallback) { return T[k] || fallback || k; }

  /* ---------------------------------------------------------------- IndexedDB */
  function openDb() {
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () { req.result.createObjectStore(STORE, { keyPath: 'uuid' }); };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }
  function tx(mode, fn) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(STORE, mode);
        var store = t.objectStore(STORE);
        var out = fn(store);
        t.oncomplete = function () { resolve(out && out.result !== undefined ? out.result : out); };
        t.onerror = function () { reject(t.error); };
      });
    });
  }
  function putItem(item) { return tx('readwrite', function (s) { return s.put(item); }); }
  function delItem(uuid) { return tx('readwrite', function (s) { return s.delete(uuid); }); }
  function allItems() {
    return openDb().then(function (db) {
      return new Promise(function (resolve) {
        var req = db.transaction(STORE, 'readonly').objectStore(STORE).getAll();
        req.onsuccess = function () { resolve(req.result || []); };
        req.onerror = function () { resolve([]); };
      });
    });
  }

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'x' + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }

  /* ---------------------------------------------------------------- Hochladen */
  var syncing = false;
  function upload(item) {
    var fd = new FormData();
    fd.append('client_uuid', item.uuid);
    fd.append('patient_id', item.patient_id || '');
    fd.append('appointment_id', item.appointment_id || '');
    fd.append('recorded_at', item.recorded_at);
    fd.append('lang', item.lang || '');
    if (item.user) fd.append('user', item.user);
    fd.append('audio', item.blob, 'aufnahme');
    return NF.fetch('/therapie/diktat/upload', { method: 'POST', body: fd }).then(function (r) {
      if (r.status === 400 || r.status === 413 || r.status === 415) {
        var e = new Error('HTTP ' + r.status); e.permanent = r.status; throw e;   // nochmals senden hilft nicht
      }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }
  function sync() {
    if (syncing || !navigator.onLine) return Promise.resolve();
    syncing = true;
    return allItems().then(function (items) {
      var chain = Promise.resolve();
      items.forEach(function (item) {
        if (item.failed || !mine(item)) return;   // abgelehnt (zu gross / ungültig) oder von einer anderen Person
        chain = chain.then(function () {
          item.state = 'sending'; render();
          return upload(item).then(function (res) {
            return delItem(item.uuid).then(function () {
              if (item.goAfter && res.url && root) window.location.href = res.url;
              else addDone(item, res);
            });
          }).catch(function (e) {
            item.state = 'waiting';
            if (e && e.permanent) { item.failed = e.permanent; return putItem(item); }
          });
        });
      });
      return chain;
    }).then(function () { syncing = false; render(); }, function () { syncing = false; render(); });
  }
  window.addEventListener('online', sync);
  setInterval(sync, 30000);

  var done = [];
  function addDone(item, res) { done.unshift({ name: item.patient_name, url: res.url }); }

  function render() {
    if (!queueEl) return;
    allItems().then(function (items) {
      var html = '';
      items.forEach(function (i) {
        var who = escapeHtml(i.patient_name || t('no_patient', 'ohne Patient')) + ' · ' + hhmm(i.recorded_at) +
          ' · ' + Math.round(i.seconds || 0) + ' s';
        if (!mine(i)) {
          html += '<div class="queue-item"><span class="pill-label warn">' + escapeHtml(t('other_user', 'andere Person')) + '</span><span class="grow">' +
            escapeHtml(hhmm(i.recorded_at)) + ' · ' + Math.round(i.seconds || 0) + ' s<br><span class="small muted">' +
            escapeHtml(t('other_user_hint', 'Wird gesendet, sobald sich diese Person auf diesem Gerät anmeldet.')) + '</span></span></div>';
          return;
        }
        if (i.failed) {
          html += '<div class="queue-item"><span class="pill-label error">' + escapeHtml(t('failed', 'nicht gesendet')) + '</span><span class="grow">' + who +
            '<br><span class="small muted">' + escapeHtml(t('failed_hint', 'Aufnahme zu gross oder ungültig – bitte Text eingeben.')) + '</span></span>' +
            '<button type="button" class="btn text" data-drop="' + escapeHtml(i.uuid) + '">' + escapeHtml(t('drop', 'Löschen')) + '</button></div>';
          return;
        }
        html += '<div class="queue-item"><span class="pill-label warn">' + (navigator.onLine ? t('sending', 'wird gesendet') : t('waiting', 'wartet auf Netz')) +
          '</span><span class="grow">' + who + '</span></div>';
      });
      done.slice(0, 5).forEach(function (d) {
        html += '<div class="queue-item"><span class="pill-label ok">' + t('sent', 'gesendet') + '</span><span class="grow">' +
          escapeHtml(d.name || '') + '</span><a class="btn text" href="' + d.url + '">' + t('open', 'Öffnen') + '</a></div>';
      });
      queueEl.innerHTML = html || '<p class="muted small">' + t('empty', 'Keine offenen Aufnahmen.') + '</p>';
      var badge = document.querySelector('[data-offline]');
      if (badge) badge.hidden = navigator.onLine;
    });
  }
  function hhmm(iso) {   // Schweizer Format 08:05, unabhängig von der Sprache des Geräts (sonst z. B. «6:38:»)
    var d = new Date(iso);
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }
  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  window.addEventListener('offline', render);
  if (queueEl) queueEl.addEventListener('click', function (e) {
    var b = e.target.closest('[data-drop]');
    if (b) delItem(b.getAttribute('data-drop')).then(render);
  });

  /* ---------------------------------------------------------------- Aufnahme */
  if (root) {
    var btn = root.querySelector('[data-rec-btn]');
    var timeEl = root.querySelector('[data-rec-time]');
    var hint = root.querySelector('[data-rec-hint]');
    var select = root.querySelector('[data-patient-select]');
    var rec = null, chunks = [], started = 0, timer = null, stream = null;

    function pickMime() {
      var types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
      for (var i = 0; i < types.length; i++) {
        if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(types[i])) return types[i];
      }
      return '';
    }
    function tick() {
      var s = Math.floor((Date.now() - started) / 1000);
      timeEl.textContent = String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
    }
    function start() {
      if (!navigator.mediaDevices || !window.MediaRecorder) {
        hint.textContent = t('no_mic', 'Dieses Gerät kann im Browser nicht aufnehmen. Bitte Text eingeben.');
        return;
      }
      if (select && !select.value) { hint.textContent = t('choose_patient', 'Bitte zuerst Patient wählen.'); select.focus(); return; }
      navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }).then(function (s) {
        stream = s;
        var mime = pickMime();
        // Sprache: 48 kbit/s reichen für die Spracherkennung – kleinere Dateien, schneller hochgeladen (auch übers Handynetz)
        var opts = { audioBitsPerSecond: 48000 };
        if (mime) opts.mimeType = mime;
        try { rec = new MediaRecorder(s, opts); } catch (err) { rec = mime ? new MediaRecorder(s, { mimeType: mime }) : new MediaRecorder(s); }
        chunks = [];
        rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
        rec.onstop = finish;
        rec.start(1000);
        started = Date.now();
        timer = setInterval(tick, 250);
        btn.classList.add('recording');
        btn.setAttribute('aria-pressed', 'true');
        btn.setAttribute('aria-label', t('stop', 'Aufnahme beenden'));
        btn.querySelector('[data-ic-mic]').hidden = true;
        btn.querySelector('[data-ic-stop]').hidden = false;
        hint.textContent = t('recording', 'Aufnahme läuft – sprechen Sie frei. Erneut tippen zum Beenden.');
      }).catch(function () {
        hint.textContent = t('denied', 'Kein Zugriff auf das Mikrofon. Bitte in den Browser-Einstellungen erlauben.');
      });
    }
    function stop() {
      if (rec && rec.state !== 'inactive') rec.stop();
      if (stream) stream.getTracks().forEach(function (tr) { tr.stop(); });
      clearInterval(timer);
      btn.classList.remove('recording');
      btn.setAttribute('aria-pressed', 'false');
      btn.setAttribute('aria-label', t('start', 'Aufnahme starten'));
      btn.querySelector('[data-ic-mic]').hidden = false;
      btn.querySelector('[data-ic-stop]').hidden = true;
    }
    function finish() {
      var blob = new Blob(chunks, { type: (rec && rec.mimeType) ? rec.mimeType.split(';')[0] : 'audio/webm' });
      var opt = select ? select.options[select.selectedIndex] : null;
      var item = {
        uuid: uuid(),
        patient_id: root.dataset.patientId || (select ? select.value : ''),
        appointment_id: root.dataset.appointmentId || (opt ? opt.dataset.appointment || '' : ''),
        patient_name: root.dataset.patientName || (opt ? opt.textContent.trim() : ''),
        recorded_at: new Date().toISOString(),
        lang: document.documentElement.lang,
        seconds: (Date.now() - started) / 1000,
        blob: blob,
        goAfter: root.dataset.goAfter === '1',
        user: me,
        state: 'waiting'
      };
      hint.textContent = navigator.onLine ? t('uploading', 'Wird an die Praxis-Box gesendet …') : t('saved_offline', 'Gespeichert im Gerät – wird gesendet, sobald Netz da ist.');
      // Browser bitten, die Aufnahmen nicht bei Speicherknappheit zu löschen (vor allem iPhone/iPad, wenn lange offline)
      if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(function () {});
      putItem(item).then(function () { render(); return sync(); });
    }
    btn.addEventListener('click', function () {
      if (rec && rec.state === 'recording') stop(); else start();
    });
  }

  if ('serviceWorker' in navigator && document.querySelector('[data-offline-shell]')) {
    navigator.serviceWorker.register('/therapie/diktat/sw.js', { scope: '/therapie/diktat' }).catch(function () {});
  }
  function go() { render(); sync(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go); else go();   // auch nachgeladen (app.js)
})();
