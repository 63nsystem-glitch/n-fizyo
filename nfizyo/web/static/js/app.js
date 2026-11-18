/* N-Fizyo – gemeinsame Oberfläche. Ohne Fremdbibliotheken, funktioniert offline. */
(function () {
  'use strict';

  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  window.NF = window.NF || {};
  NF.csrf = csrf;

  /* fetch mit CSRF-Schutz */
  NF.fetch = function (url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'X-CSRF-Token': csrf }, opts.headers || {});
    opts.credentials = 'same-origin';
    return fetch(url, opts);
  };

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  /* ---------------------------------------------------------------- Dialoge */
  function openDialog(dlg) {
    if (!dlg) return;
    if (typeof dlg.showModal === 'function') { if (!dlg.open) dlg.showModal(); }
    else dlg.setAttribute('open', '');
  }
  NF.openDialog = openDialog;
  document.addEventListener('click', function (e) {
    var opener = e.target.closest('[data-dialog]');
    if (opener) {
      e.preventDefault();
      var dlg = document.getElementById(opener.getAttribute('data-dialog'));
      // Vorbelegung: data-fill='{"name":"wert"}'
      var fill = opener.getAttribute('data-fill');
      if (dlg && fill) {
        try {
          var values = JSON.parse(fill);
          Object.keys(values).forEach(function (k) {
            var field = dlg.querySelector('[name="' + k + '"]');
            if (field) field.value = values[k];
          });
        } catch (err) { /* ungültige Vorbelegung ignorieren */ }
      }
      openDialog(dlg);
      return;
    }
    var closer = e.target.closest('[data-close]');
    if (closer) {
      var d = closer.closest('dialog');
      if (d) { e.preventDefault(); d.close(); }
    }
  });
  /* Hilfe-Karten (data-help): auf schmaleren Bildschirmen zugeklappt, damit die Arbeitsliste die ganze Breite bekommt */
  if (window.matchMedia && window.matchMedia('(max-width: 1399px)').matches) {
    $all('details[data-help]').forEach(function (d) { d.open = false; });
  }

  /* Klick auf den Hintergrund schliesst Dialoge */
  document.addEventListener('click', function (e) {
    if (e.target.tagName === 'DIALOG' && e.target.open) {
      var r = e.target.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) e.target.close();
    }
  });

  /* Bestätigung vor heiklen Aktionen (eigener Dialog statt Browser-Fenster) */
  document.addEventListener('submit', function (e) {
    var form = e.target;
    var msg = form.getAttribute('data-confirm');
    if (!msg || form.dataset.confirmed === '1') return;
    e.preventDefault();
    var dlg = document.getElementById('nf-confirm');
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.id = 'nf-confirm';
      dlg.className = 'dlg';
      dlg.innerHTML = '<p class="nf-msg" style="font-size:16px;line-height:24px;margin-bottom:24px"></p>' +
        '<div class="form-actions"><button type="button" class="btn text" data-close></button>' +
        '<button type="button" class="btn nf-ok"></button></div>';
      document.body.appendChild(dlg);
    }
    $('.nf-msg', dlg).textContent = msg;
    $('[data-close]', dlg).textContent = form.getAttribute('data-confirm-no') || (document.documentElement.lang === 'fr' ? 'Annuler' : 'Abbrechen');
    $('.nf-ok', dlg).textContent = form.getAttribute('data-confirm-yes') || 'OK';
    $('.nf-ok', dlg).onclick = function () { form.dataset.confirmed = '1'; dlg.close(); form.requestSubmit ? form.requestSubmit() : form.submit(); };
    openDialog(dlg);
  });

  /* Doppelte Absendung verhindern */
  document.addEventListener('submit', function (e) {
    if (e.defaultPrevented) return;
    var btn = e.submitter || $('button[type=submit]', e.target);
    if (btn && !e.target.hasAttribute('data-multi')) {
      setTimeout(function () { btn.disabled = true; }, 0);
      setTimeout(function () { btn.disabled = false; }, 8000);
    }
  });

  /* Auswahlfelder, die sofort absenden */
  document.addEventListener('change', function (e) {
    if (e.target.matches('[data-autosubmit]')) e.target.form.submit();
  });

  /* ---------------------------------------------------------------- Seitenblatt (Anatomie, Original-Dokument) */
  function sheet() {
    var dlg = document.getElementById('nf-sheet');
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.id = 'nf-sheet';
      dlg.className = 'sheet';
      document.body.appendChild(dlg);
    }
    return dlg;
  }
  NF.openSheet = function (url) {
    var dlg = sheet();
    dlg.innerHTML = '<div class="sheet-body"><div class="progress" style="margin-top:24px"><span style="width:40%"></span></div></div>';
    openDialog(dlg);
    NF.fetch(url, { headers: { 'Accept': 'text/html' } }).then(function (r) { return r.text(); }).then(function (html) {
      dlg.innerHTML = html;
      initFigures(dlg);
    }).catch(function () { dlg.close(); });
  };
  document.addEventListener('click', function (e) {
    var el = e.target.closest('[data-sheet]');
    if (!el) return;
    e.preventDefault();
    NF.openSheet(el.getAttribute('data-sheet'));
  });

  /* ---------------------------------------------------------------- Körperfigur */
  function initFigures(root) {
    $all('[data-bodyfig]', root).forEach(function (fig) {
      if (fig.dataset.ready) return;
      fig.dataset.ready = '1';
      function show() {
        var layer = fig.dataset.layer, view = fig.dataset.view;
        $all('[data-pane]', fig).forEach(function (p) {
          p.hidden = !(p.dataset.layer === layer && p.dataset.view === view);
        });
        $all('[data-bf-view]', fig).forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.bfView === view)); });
        $all('[data-bf-layer]', fig).forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.bfLayer === layer)); });
      }
      fig.addEventListener('click', function (e) {
        var vb = e.target.closest('[data-bf-view]');
        if (vb) { fig.dataset.view = vb.dataset.bfView; show(); return; }
        var lb = e.target.closest('[data-bf-layer]');
        if (lb) { fig.dataset.layer = lb.dataset.bfLayer; show(); return; }
        var zb = e.target.closest('[data-bf-zoom]');
        if (zb) { zb.setAttribute('aria-pressed', String(fig.classList.toggle('zoomed'))); return; }
        var region = e.target.closest('[data-region]');
        var bone = e.target.closest('[data-bone]');
        if (!region && !bone) return;
        var detail = { region: region ? region.dataset.region : null, bone: bone ? bone.dataset.bone : null, el: region || bone };
        fig.dispatchEvent(new CustomEvent('nf:pick', { bubbles: true, detail: detail }));
      });
      fig.addEventListener('keydown', function (e) {
        if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('[data-region],[data-bone]')) {
          e.preventDefault();
          e.target.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        }
      });
    });
  }
  NF.initFigures = initFigures;

  /* Klick in eine Figur: Befund-Dialog vorbelegen (Patienten-Bildschirm) oder Struktur im Atlas öffnen */
  document.addEventListener('nf:pick', function (e) {
    var fig = e.target.closest('[data-bodyfig]');
    if (!fig || fig.hasAttribute('data-pick-custom') || !fig.classList.contains('editable')) return;
    var dlgId = fig.getAttribute('data-pick-dialog');
    if (dlgId) {
      var dlg = document.getElementById(dlgId);
      if (!dlg) return;
      var region = e.detail.region || '', bone = e.detail.bone || '';
      dlg.querySelector('[name="region"]').value = region;
      dlg.querySelector('[name="bone"]').value = bone;
      var side = (region || bone).match(/_(l|r)$/);
      var sideSel = dlg.querySelector('[name="side"]');
      if (sideSel) sideSel.value = side ? side[1] : '';
      var title = e.detail.el ? (e.detail.el.querySelector('title') || {}).textContent || '' : '';
      var lbl = dlg.querySelector('[data-pick-label]');
      if (lbl) lbl.textContent = title;
      openDialog(dlg);
      var t = dlg.querySelector('[name="title"]');
      if (t) t.focus();
      return;
    }
    var base = fig.getAttribute('data-bone-href');
    if (e.detail.bone && base) window.location.href = base + encodeURIComponent(e.detail.bone);
  });

  /* ---------------------------------------------------------------- Warten auf Hintergrundaufgaben */
  function initPolling() {
    $all('[data-poll]').forEach(function (el) {
      var url = el.getAttribute('data-poll');
      var tries = 0;
      function tick() {
        tries++;
        NF.fetch(url, { headers: { 'Accept': 'application/json' } }).then(function (r) { return r.json(); }).then(function (data) {
          if (data.done) { window.location.reload(); return; }
          if (tries < 200) setTimeout(tick, tries < 10 ? 2000 : 5000);
        }).catch(function () { if (tries < 200) setTimeout(tick, 8000); });
      }
      setTimeout(tick, 1500);
    });
  }

  /* Zeichenzähler und automatisch wachsende Textfelder */
  document.addEventListener('input', function (e) {
    if (e.target.matches('textarea[data-autogrow]')) {
      e.target.style.height = 'auto';
      e.target.style.height = (e.target.scrollHeight + 2) + 'px';
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    initFigures(document);
    initPolling();
  });
})();

/* Ohne Inline-JavaScript (Content-Security-Policy): Drucken, Feld auswählen, Auswahl als Navigation. */
(function () {
  'use strict';
  document.addEventListener('click', function (e) {
    var p = e.target.closest('[data-print]');
    if (p) { e.preventDefault(); window.print(); return; }
    var sel = e.target.closest('[data-select-all]');
    if (sel && sel.select) sel.select();
  });
  document.addEventListener('change', function (e) {
    var s = e.target.closest('select[data-nav-select]');
    if (s && s.value) window.location.href = s.getAttribute('data-nav-select') + encodeURIComponent(s.value);
  });
})();


/* Reiterleiste, die seitlich weitergeht: Rand ausblenden (Hinweis "da kommt noch mehr") und aktiven Reiter zeigen. */
(function () {
  'use strict';
  function update(bar) {
    var more = bar.scrollWidth - bar.clientWidth > 2;
    var rtl = getComputedStyle(bar).direction === 'rtl';
    var x = Math.abs(bar.scrollLeft);
    var start = more && x > 2;
    var end = more && x < bar.scrollWidth - bar.clientWidth - 2;
    bar.classList.toggle('fade-end', end);
    bar.classList.toggle('fade-start', start);
    bar.classList.toggle('rtl', rtl);
  }
  function init() {
    Array.prototype.forEach.call(document.querySelectorAll('.tabs:not(.tabs-wrap)'), function (bar) {
      var cur = bar.querySelector('[aria-current="page"]');
      if (cur && bar.scrollWidth > bar.clientWidth) {
        var r = cur.getBoundingClientRect(), b = bar.getBoundingClientRect();
        if (r.right > b.right || r.left < b.left) bar.scrollLeft += (r.left - b.left) - (b.width - r.width) / 2;
      }
      update(bar);
      bar.addEventListener('scroll', function () { update(bar); }, { passive: true });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  window.addEventListener('resize', function () {
    Array.prototype.forEach.call(document.querySelectorAll('.tabs:not(.tabs-wrap)'), update);
  });
})();

/* Anmeldeseite (nach dem Abmelden): Offline-Kopie der Diktatseite löschen – sie enthält Namen von Patient/innen und wäre sonst
   auf einem geteilten Tablet ohne Netz weiter lesbar. Nicht gesendete Aufnahmen (IndexedDB) bleiben und gehören ihrer Person. */
(function () {
  'use strict';
  if (location.pathname !== '/login' || !window.caches || !caches.keys) return;
  caches.keys().then(function (keys) {
    keys.filter(function (k) { return k.indexOf('nfizyo-diktat') === 0; }).forEach(function (k) { caches.delete(k); });
  }).catch(function () {});
})();

/* Therapie-Seiten ohne Aufnahmeknopf: warten im Gerät noch Aufnahmen dieser Person (z. B. offline beim Hausbesuch gemacht,
   danach nur die Agenda geöffnet), wird das Hochladen hier gestartet – nicht erst, wenn jemand die Diktatseite öffnet. */
(function () {
  'use strict';
  var meta = document.querySelector('meta[name="nf-user"]');
  if (!meta || document.querySelector('[data-recorder]') || !window.indexedDB) return;
  function start() {
    var own = document.querySelector('script[src*="/static/js/app.js"]');
    if (!own) return;
    var s = document.createElement('script');
    s.src = own.getAttribute('src').replace('/static/js/app.js', '/static/js/dictation.js');
    s.defer = true;
    document.body.appendChild(s);
  }
  function check() {
    var req = indexedDB.open('nfizyo-diktat', 1);
    req.onupgradeneeded = function () { req.result.createObjectStore('queue', { keyPath: 'uuid' }); };
    req.onsuccess = function () {
      try {
        var all = req.result.transaction('queue', 'readonly').objectStore('queue').getAll();
        all.onsuccess = function () {
          var mine = (all.result || []).filter(function (i) { return !i.failed && (!i.user || i.user === meta.content); });
          if (mine.length) start();
        };
      } catch (e) { /* kein Speicher (privates Fenster) */ }
    };
  }
  if (indexedDB.databases) {
    indexedDB.databases().then(function (dbs) {
      if (dbs.some(function (d) { return d.name === 'nfizyo-diktat'; })) check();
    }).catch(function () {});
  } else {
    check();
  }
})();
