/* ViaRhôna Reiseplan — Service Worker
   Zweck: Seite und bereits betrachtete Kartenkacheln offline verfügbar halten.
   Ablage: im selben Ordner wie index.html (also im Wurzelverzeichnis von /via-rhona/). */

const APP_CACHE  = 'viarhona-app-v2';
const TILE_CACHE = 'viarhona-tiles-v2';
const MAX_TILES  = 3000;          // ca. 60–120 MB, reicht für alle 15 Etappen

/* Dateien, die beim ersten Aufruf fest gespeichert werden */
const CORE = [
  './',
  './index.html',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
  'https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.47.0/tabler-icons.min.css'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(APP_CACHE).then(cache =>
      // einzeln, damit ein fehlgeschlagener CDN-Abruf nicht die ganze Installation kippt
      Promise.all(CORE.map(url =>
        cache.add(new Request(url, { mode: 'no-cors' })).catch(() => null)
      ))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== APP_CACHE && k !== TILE_CACHE).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

/* Kachelspeicher begrenzen: älteste Einträge zuerst löschen */
async function trimTiles() {
  const cache = await caches.open(TILE_CACHE);
  const keys  = await cache.keys();
  if (keys.length <= MAX_TILES) return;
  for (let i = 0; i < keys.length - MAX_TILES; i++) await cache.delete(keys[i]);
}

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  /* 1) Wetterabruf niemals aus dem Speicher bedienen — veraltete Werte wären irreführend */
  if (url.hostname === 'api.open-meteo.com') return;

  /* 2) Kartenkacheln: erst Speicher, dann Netz (und dabei nachspeichern) */
  if (/tile\.openstreetmap\.org$/.test(url.hostname)) {
    event.respondWith(
      caches.open(TILE_CACHE).then(cache =>
        cache.match(req).then(hit => {
          if (hit) return hit;
          return fetch(req).then(res => {
            if (res && (res.ok || res.type === 'opaque')) {
              cache.put(req, res.clone());
              trimTiles();
            }
            return res;
          }).catch(() => hit || Response.error());
        })
      )
    );
    return;
  }

  /* 3) Seite selbst: erst Netz (damit Aktualisierungen ankommen), bei Ausfall Speicher */
  if (req.mode === 'navigate' || url.pathname.endsWith('index.html') || url.pathname.endsWith('/')) {
    event.respondWith(
      fetch(req).then(res => {
        const copy = res.clone();
        caches.open(APP_CACHE).then(c => c.put(req, copy)).catch(() => {});
        return res;
      }).catch(() => caches.match(req).then(hit => hit || caches.match('./index.html')))
    );
    return;
  }

  /* 4) Alles Übrige (Leaflet, Symbolschrift, Schriften): erst Speicher, dann Netz */
  event.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      const copy = res.clone();
      caches.open(APP_CACHE).then(c => c.put(req, copy)).catch(() => {});
      return res;
    }).catch(() => hit))
  );
});
