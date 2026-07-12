self.addEventListener('install', event => {
    event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('push', event => {
    let data = {};
    try {
        data = event.data ? event.data.json() : {};
    } catch (e) {
        data = {body: event.data ? event.data.text() : ''};
    }

    const title = data.title || 'Пирожковый Диспетчер';
    const isAlarm = Boolean(data.is_alarm);
    const options = {
        body: data.body || '',
        icon: '/static/sirius.png',
        badge: '/static/sirius.png',
        tag: isAlarm ? 'sirius-alarm' : 'sirius-notification',
        renotify: isAlarm,
        requireInteraction: isAlarm,
        silent: false,
        timestamp: Date.now(),
        data: {url: data.url || '/events?tab=notifications'},
    };
    if (isAlarm) {
        options.vibrate = [700, 200, 700, 200, 1000];
    }

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    const url = event.notification.data && event.notification.data.url
        ? event.notification.data.url
        : '/events?tab=notifications';

    event.waitUntil((async () => {
        const allClients = await clients.matchAll({type: 'window', includeUncontrolled: true});
        for (const client of allClients) {
            if ('focus' in client) {
                await client.navigate(url);
                return client.focus();
            }
        }
        return clients.openWindow(url);
    })());
});
