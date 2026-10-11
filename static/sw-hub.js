/* Drop Driver Hub service worker — minimal pass-through.
   Exists so the Hub is installable (Add to Home Screen). No caching logic. */
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.clients.claim());
self.addEventListener('fetch', () => {});
