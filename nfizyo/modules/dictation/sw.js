/* N-Fizyo Diktat – Offline-Hülle. Aufnahmen selbst liegen in IndexedDB (dictation.js). */
const CACHE = 'nfizyo-diktat-v2';
const SHELL = ['/therapie/diktat', '/static/css/app.css', '/static/js/app.js', '/static/js/dictation.js'];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const isShell = url.pathname === '/therapie/diktat' || url.pathname.startsWith('/static/');
  if (!isShell) return;
  // Netz zuerst (immer aktuelle Patientenliste), sonst Kopie aus dem Cache
  e.respondWith(fetch(req).then((res) => {
    if (res.ok && url.pathname === '/therapie/diktat') {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put('/therapie/diktat', copy));
    }
    return res;
  }).catch(() => caches.match(url.pathname === '/therapie/diktat' ? '/therapie/diktat' : req, { ignoreSearch: true })));
});
