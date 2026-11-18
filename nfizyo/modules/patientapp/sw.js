/* N-Fizyo Patienten-App: Hülle offline verfügbar (Startseite und Übungen zuletzt geladen).
   Datenschutz: Ist die Anmeldung weg (Abmelden, Zugang beendet, Praxis hat die App abgemeldet, Sitzung abgelaufen), leitet
   der Server auf /p/anmelden um – dann werden alle gespeicherten Seiten gelöscht. Abmelden löscht sie zusätzlich per
   «Clear-Site-Data». Gespeichert wird nur eine Antwort ohne Umleitung. */
const CACHE = 'nfizyo-p-v2';
function forgetPages() {
  return caches.open(CACHE).then((c) => c.keys().then((ks) => Promise.all(
    ks.filter((k) => new URL(k.url).pathname.startsWith('/p/')).map((k) => c.delete(k)))));
}
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/static/css/app.css', '/static/js/app.js']).catch(() => {})).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('message', (e) => {
  if (e.data === 'forget') e.waitUntil(forgetPages());
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (!(url.pathname.startsWith('/p/') || url.pathname.startsWith('/static/'))) return;
  if (url.pathname.startsWith('/p/einladung') || url.pathname.startsWith('/p/anmelden') || url.pathname.startsWith('/p/abmelden')) return;
  e.respondWith(fetch(req).then((res) => {
    if (res.redirected && new URL(res.url).pathname.startsWith('/p/anmelden')) {
      forgetPages();
    } else if (res.ok && !res.redirected && (url.pathname === '/p/heute' || url.pathname.startsWith('/p/uebungen') || url.pathname.startsWith('/static/'))) {
      const copy = res.clone();
      // Nach einem Update (?v=… neu) die alte Fassung derselben Datei wegräumen, sonst wächst der Speicher mit jedem Update
      caches.open(CACHE).then((c) => c.put(req, copy).then(() => url.pathname.startsWith('/static/') && c.keys().then((ks) =>
        Promise.all(ks.filter((k) => { const u = new URL(k.url); return u.pathname === url.pathname && u.search !== url.search; })
          .map((k) => c.delete(k))))));
    }
    return res;
  }).catch(() => caches.match(req)));
});
