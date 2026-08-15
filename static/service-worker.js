/* ═══════════════════════════════════════
   SmartAgro Service Worker
   Handles: offline cache, background sync
═══════════════════════════════════════ */

const CACHE_NAME = 'smartagro-v1';
const OFFLINE_URL = '/offline';

// Files to cache for offline use
const STATIC_ASSETS = [
    '/',
    '/diagnose',
    '/market',
    '/alerts',
    '/offline',
    '/static/css/main.css',
    '/static/css/dashboard.css',
    '/static/css/diagnose.css',
    '/static/css/market.css',
    '/static/css/alerts.css',
    '/static/js/main.js',
    '/static/js/dashboard.js',
    '/static/js/diagnose.js',
    '/static/js/market.js',
    '/static/js/alerts.js',
    '/static/js/translations.js',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    'https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=Inter:wght@300;400;500;600&display=swap',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css',
];

// Install — cache all static assets
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(STATIC_ASSETS);
        })
    );
    self.skipWaiting();
});

// Activate — delete old caches
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
            )
        )
    );
    self.clients.claim();
});

// Fetch — serve from cache, fallback to network
self.addEventListener('fetch', event => {
    const { request } = event;
    const url = new URL(request.url);

    // API calls — always go to network, don't cache
    if (url.pathname.startsWith('/api/')) {
        event.respondWith(
            fetch(request).catch(() =>
                new Response(JSON.stringify({ error: 'Offline — no internet connection' }), {
                    headers: { 'Content-Type': 'application/json' }
                })
            )
        );
        return;
    }

    // HTML pages — network first, fallback to cache, then offline page
    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request)
            .then(res => {
                const clone = res.clone();
                caches.open(CACHE_NAME).then(cache => cache.put(request, clone));
                return res;
            })
            .catch(() =>
                caches.match(request).then(cached => cached || caches.match(OFFLINE_URL))
            )
        );
        return;
    }

    // Static assets — cache first, fallback to network
    // Static assets — network first, fallback to cache (so local edits
    // always show up; offline users still get the last-cached version)
    event.respondWith(
        fetch(request)
            .then(res => {
                const clone = res.clone();
                caches.open(CACHE_NAME).then(cache => cache.put(request, clone));
                return res;
            })
            .catch(() => caches.match(request))
    );
});

/* ═══════════════════════════════════════
   Device Pop-up Notifications Handlers
═══════════════════════════════════════ */

// Notification click — focus app tab or open /alerts
self.addEventListener('notificationclick', event => {
    event.notification.close();
    const targetUrl = (event.notification.data && event.notification.data.url) ? event.notification.data.url : '/alerts';
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
            for (const client of clientList) {
                if (client.url.includes(targetUrl) || client.url.endsWith('/alerts')) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});

// Push notification payload receiver
self.addEventListener('push', event => {
    let payload = {
        title: '🌾 SmartAgro Alert',
        body: 'New agricultural advisory for your location.',
        icon: '/static/icons/icon-192.png',
        badge: '/static/icons/icon-192.png',
        url: '/alerts'
    };
    if (event.data) {
        try {
            payload = Object.assign(payload, event.data.json());
        } catch (e) {
            payload.body = event.data.text();
        }
    }
    const options = {
        body: payload.body,
        icon: payload.icon || '/static/icons/icon-192.png',
        badge: payload.badge || '/static/icons/icon-192.png',
        vibrate: [200, 100, 200],
        data: { url: payload.url || '/alerts' },
        tag: payload.tag || 'smartagro-alert',
        renotify: true
    };
    event.waitUntil(self.registration.showNotification(payload.title, options));
});

// Message listener from web app
self.addEventListener('message', event => {
    if (event.data && event.data.type === 'SHOW_NOTIFICATION') {
        const { title, body, icon, url, tag } = event.data;
        const options = {
            body: body || 'New severe weather or crop alert detected for your location.',
            icon: icon || '/static/icons/icon-192.png',
            badge: '/static/icons/icon-192.png',
            vibrate: [200, 100, 200],
            data: { url: url || '/alerts' },
            tag: tag || ('smartagro-alert-' + Date.now()),
            renotify: true
        };
        event.waitUntil(self.registration.showNotification(title || '🌾 SmartAgro Weather Alert', options));
    }
});