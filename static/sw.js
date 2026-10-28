// Service worker для офлайн-перезагрузки страницы комнаты.
// Стратегия: сеть — первым, при обрыве отдаём последнюю сохранённую копию.
// Страница комнаты самодостаточна (CSS и JS внутри), поэтому кешируется целиком.

const CACHE = 'svoyak-shell-v1';

async function cachePage(url) {
    const response = await fetch(url, { cache: 'reload' });
    if (!response || !response.ok) return;
    const cache = await caches.open(CACHE);
    await cache.put(new Request(url), response);
}

self.addEventListener('install', () => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(
            keys
                .filter((key) => key.startsWith('svoyak-shell-') && key !== CACHE)
                .map((key) => caches.delete(key))
        );
        await self.clients.claim();
    })());
});

self.addEventListener('message', (event) => {
    const data = event.data || {};
    if (data.type === 'cache-page' && data.url) {
        event.waitUntil(cachePage(data.url).catch(() => {}));
    }
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;
    if (url.pathname === '/api/ping') return;

    if (request.mode === 'navigate') {
        event.respondWith((async () => {
            try {
                const response = await fetch(request);
                if (response && response.ok) {
                    const cache = await caches.open(CACHE);
                    await cache.put(request, response.clone());
                }
                return response;
            } catch (error) {
                const cached = await caches.match(request, { ignoreSearch: false });
                if (cached) return cached;
                return new Response(
                    '<!doctype html><html lang="ru"><meta charset="utf-8">' +
                    '<h1>Нет связи с сервером</h1>' +
                    '<p>Откройте комнату, когда сервер снова будет доступен.</p>',
                    { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
                );
            }
        })());
    }
});
