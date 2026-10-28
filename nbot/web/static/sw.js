// NekoBot Service Worker
const CACHE_NAME = 'nekobot-v20261028-review-fixes-v2';
const STATIC_ASSETS = [
    '/',
    '/static/css/app.css',
    '/static/js/i18n.js',
    '/static/js/nbot-shared.js',
    '/static/js/nbot-methods.js',
    '/static/svg/opencode.svg',
    '/static/vendor/socket.io.min.js',
    '/static/vendor/vue.global.prod.js',
    '/static/vendor/vue-router.global.prod.js',
    '/static/vendor/axios.min.js',
    '/static/vendor/marked.min.js',
    '/static/vendor/highlight.min.js',
    '/static/vendor/styles/highlight-github-dark.min.css',
    '/static/neko.png'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then((cache) => cache.addAll(STATIC_ASSETS))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((cacheNames) => Promise.all(
                cacheNames
                    .filter((name) => name !== CACHE_NAME)
                    .map((name) => caches.delete(name))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const { request } = event;
    const url = new URL(request.url);

    if (request.method !== 'GET') return;
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/socket.io/')) return;
    if (url.pathname.startsWith('/static/uploads/')) return;

    if (request.headers.get('accept')?.includes('text/html')) {
        event.respondWith(
            fetch(request, { cache: 'no-store' })
                .then((response) => {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
                    return response;
                })
                .catch(() => caches.match(request).then((cached) => cached || caches.match('/')))
        );
        return;
    }

    event.respondWith(
        fetch(request)
            .then((response) => {
                if (response.ok) {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
                }
                return response;
            })
            .catch(() => caches.match(request).then(
                (cached) => cached || new Response('', { status: 408, statusText: 'Offline' })
            ))
    );
});

self.addEventListener('push', (event) => {
    let data = {};

    try {
        data = event.data ? event.data.json() : {};
    } catch {
        data = {
            title: 'NekoBot',
            body: event.data ? event.data.text() : 'You have a new message.',
        };
    }

    const title = data.title || 'NekoBot';
    const options = {
        body: data.body || 'You have a new message.',
        icon: '/static/neko.png',
        badge: '/static/neko.png',
        tag: data.tag || 'nekobot-message',
        renotify: true,
        data: {
            url: data.url || '/',
        },
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const targetUrl = event.notification.data?.url || '/';

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
            for (const client of clientList) {
                if ('focus' in client) {
                    client.navigate(targetUrl);
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
            return undefined;
        })
    );
});
