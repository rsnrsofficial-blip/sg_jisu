const CACHE_NAME = 'sgjisu-v1';
const STATIC_ASSETS = [
  '/',
  '/manifest.json',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  // Supabase, 외부 API, 자체 API 요청은 캐시 안 함 (서비스워커 개입 금지)
  if (event.request.url.includes('supabase.co') ||
      event.request.url.includes('/analyze') ||
      event.request.url.includes('/investor') ||
      event.request.url.includes('/popular') ||
      event.request.url.includes('/movers') ||
      event.request.url.includes('/log')) {
    return;
  }

  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request))
  );
});
