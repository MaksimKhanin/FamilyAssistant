/* Service worker: уведомления и немного офлайна.
 *
 * Данные тут намеренно не кешируются. Ассистент показывает баланс дня и события с
 * камер — устаревшая копия хуже честного «нет сети». Кешируется только оболочка:
 * стили, скрипт и иконки, чтобы приложение открывалось мгновенно.
 */

const CACHE = 'family-assistant-v2';
const SHELL = [
  '/static/style.css',
  '/static/app.js',
  '/static/htmx.min.js',
  '/static/icons/icon-192.png',
  '/static/fonts/manrope-cyrillic.woff2',
  '/static/fonts/manrope-latin.woff2',
  '/static/fonts/playfair-display-cyrillic.woff2',
  '/static/fonts/playfair-display-latin.woff2',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(names => Promise.all(names.filter(n => n !== CACHE).map(n => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/static/')) return;

  // Оболочка: отдаём из кеша, но обновляем в фоне. ignoreSearch — потому что
  // ссылки несут версию в query (?v=хэш), а кеш хранит файл по чистому пути.
  event.respondWith(
    caches.match(request, { ignoreSearch: true }).then(cached => {
      const network = fetch(request).then(response => {
        // Кладём по чистому пути, без ?v= — иначе копилась бы копия на каждую версию.
        if (response.ok) caches.open(CACHE).then(cache => cache.put(url.origin + url.pathname, response.clone()));
        return response;
      }).catch(() => cached);
      return cached || network;
    })
  );
});

self.addEventListener('push', event => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = { body: event.data ? event.data.text() : '' };
  }

  const title = payload.title || 'Ассистент';
  event.waitUntil(self.registration.showNotification(title, {
    body: payload.body || '',
    tag: payload.tag || 'assistant',
    renotify: true,
    requireInteraction: Boolean(payload.requireInteraction),
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/favicon-32.png',
    data: { url: payload.url || '/' },
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';

  // Если панель уже открыта — переводим её на нужный экран, а не плодим вкладки.
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
      for (const client of clients) {
        if ('focus' in client) {
          client.navigate(target);
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
