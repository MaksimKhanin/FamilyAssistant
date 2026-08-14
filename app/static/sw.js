/* Service worker: уведомления и немного офлайна.
 *
 * Данные тут намеренно не кешируются. Ассистент показывает баланс дня и события с
 * камер — устаревшая копия хуже честного «нет сети». Кешируется только оболочка:
 * стили, скрипт и иконки, чтобы приложение открывалось мгновенно.
 */

const CACHE = 'family-assistant-v4';
// Кириллица каждого шрифта — первой: панель по-русски, и латиница нужна ей
// только под имена инструментов.
const SHELL = [
  '/static/style.css',
  '/static/app.js',
  '/static/htmx.min.js',
  '/static/icons/icon-192.png',
  '/static/fonts/onest-cyrillic.woff2',
  '/static/fonts/onest-latin.woff2',
  '/static/fonts/literata-cyrillic.woff2',
  '/static/fonts/literata-latin.woff2',
  '/static/fonts/ibm-plex-mono-cyrillic.woff2',
  '/static/fonts/golos-text-cyrillic.woff2',
  '/static/fonts/golos-text-latin.woff2',
  '/static/fonts/jetbrains-mono-cyrillic.woff2',
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

  // Ссылка на файл несёт версию его содержимого (?v=хэш, см. static_url), и
  // кеш ищется по полному URL вместе с ней. Совпало — значит в кеше ровно тот
  // файл, который просят, и сеть не нужна вовсе. Не совпало — файл изменился, и
  // за ним идём в сеть.
  //
  // Раньше здесь искалось с ignoreSearch и отдавалось «из кеша, обновим в фоне»:
  // после каждой выкладки браузер получал новую разметку со старыми стилями, и
  // экран приезжал наполовину переехавшим. Свежесть оболочки важнее лишнего
  // запроса: без совпадающей версии панель нарисована неправильно.
  event.respondWith(caches.open(CACHE).then(async cache => {
    const exact = await cache.match(request);
    if (exact) return exact;
    try {
      const response = await fetch(request);
      if (response.ok) {
        await dropOtherVersions(cache, url.pathname);
        await cache.put(request, response.clone());
      }
      return response;
    } catch (error) {
      // Сети нет: прошлая версия файла лучше пустого экрана.
      const stale = await cache.match(request, { ignoreSearch: true });
      if (stale) return stale;
      throw error;
    }
  }));
});

/** Одна версия файла в кеше за раз: иначе копия копилась бы на каждую выкладку. */
async function dropOtherVersions(cache, pathname) {
  const keys = await cache.keys();
  await Promise.all(
    keys.filter(key => new URL(key.url).pathname === pathname).map(key => cache.delete(key))
  );
}

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
