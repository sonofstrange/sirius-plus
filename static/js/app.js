let _pollingInterval = null;
let _alarmAudioCtx = null;
let _alarmUnlocked = localStorage.getItem('alarmAudioEnabled') === '1';
let _alarmRepeatTimer = null;

function startNotificationPolling(userId) {
    if (_pollingInterval) return;
    _pollingInterval = setInterval(async () => {
        try {
            const resp = await fetch('/api/notifications', { method: 'POST' });
            const data = await resp.json();
            if (data.ok && data.notifications && data.notifications.length > 0) {
                const container = document.getElementById('notifications-live');
                for (const msg of data.notifications) {
                    const el = document.createElement('div');
                    el.className = 'notification';
                    el.innerHTML = `
                        <span class="notification__text">${escapeHtml(msg)}</span>
                        <button class="notification__close" onclick="this.parentElement.remove()">
                            <span class="material-symbols-outlined">close</span>
                        </button>
                    `;
                    container.appendChild(el);
                    handleNotificationMessage(msg);
                    setTimeout(() => {
                        if (el.parentElement) el.remove();
                    }, 10000);
                }
                if (typeof refreshNotificationBadge === 'function') refreshNotificationBadge();
            }
        } catch (e) {
            // ignore polling errors
        }
    }, 3000);
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
        outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
}

async function enableStrongNotifications() {
    const result = document.getElementById('strong-notify-result');
    if (result) {
        result.style.display = 'block';
        result.className = 'alert';
        result.textContent = 'Включаю уведомления...';
    }

    try {
        if ('Notification' in window && Notification.permission !== 'granted') {
            const permission = await Notification.requestPermission();
            if (permission !== 'granted') {
                throw new Error('Браузер не дал разрешение на уведомления');
            }
        }

        unlockAlarmAudio();
        await registerPushNotifications();

        if (result) {
            result.className = 'alert alert-success';
            result.textContent = 'Мощные уведомления включены. Для звука держи вкладку открытой.';
        }
        if (typeof updateNotificationSettingsStatus === 'function') {
            updateNotificationSettingsStatus();
        }
    } catch (e) {
        if (result) {
            result.className = 'alert alert-error';
            result.textContent = e.message || String(e);
        } else {
            alert(e.message || e);
        }
    }
}

function unlockAlarmAudio() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;
    if (!_alarmAudioCtx) {
        _alarmAudioCtx = new AudioContextClass();
    }
    if (_alarmAudioCtx.state === 'suspended') {
        _alarmAudioCtx.resume();
    }
    _alarmUnlocked = true;
    localStorage.setItem('alarmAudioEnabled', '1');
    playAlarmTone(0.08);
}

async function getStrongNotificationState() {
    const state = {
        notification: !('Notification' in window) ? 'unsupported' : Notification.permission,
        pushSupported: 'serviceWorker' in navigator && 'PushManager' in window,
        pushEnabled: false,
        soundEnabled: localStorage.getItem('alarmAudioEnabled') === '1',
    };
    if (state.pushSupported) {
        try {
            const registration = await getPushRegistration(false);
            if (registration) {
                state.pushEnabled = Boolean(await registration.pushManager.getSubscription());
            }
        } catch (e) {
            state.pushEnabled = false;
        }
    }
    return state;
}

async function disableStrongNotifications() {
    const result = document.getElementById('strong-notify-result');
    if (result) {
        result.style.display = 'block';
        result.className = 'alert';
        result.textContent = 'Отключаю на этом устройстве...';
    }
    try {
        if ('serviceWorker' in navigator) {
            const registration = await getPushRegistration(false);
            if (registration) {
                const subscription = await registration.pushManager.getSubscription();
                if (subscription) {
                    await fetch('/api/push/unsubscribe', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({endpoint: subscription.endpoint}),
                    });
                    await subscription.unsubscribe();
                }
            }
        }
        localStorage.removeItem('alarmAudioEnabled');
        _alarmUnlocked = false;
        if (result) {
            result.className = 'alert alert-success';
            result.textContent = 'Мощные уведомления отключены на этом устройстве.';
        }
        if (typeof updateNotificationSettingsStatus === 'function') {
            updateNotificationSettingsStatus();
        }
    } catch (e) {
        if (result) {
            result.className = 'alert alert-error';
            result.textContent = e.message || String(e);
        }
    }
}

async function testStrongNotification() {
    const result = document.getElementById('strong-notify-result');
    if (result) {
        result.style.display = 'block';
        result.className = 'alert';
        result.textContent = 'Проверяю уведомления...';
    }
    unlockAlarmAudio();
    showAlarmOverlay('🔔 Тестовый будильник\nЕсли вкладка открыта, должен быть звук, вибрация и крупное окно.');

    try {
        if (!('Notification' in window)) {
            throw new Error('Этот браузер не поддерживает системные уведомления');
        }
        if (Notification.permission !== 'granted') {
            const permission = await Notification.requestPermission();
            if (permission !== 'granted') {
                throw new Error('Браузер не дал разрешение на уведомления');
            }
        }

        await registerPushNotifications();
        const pushResp = await fetch('/api/push/test', {method: 'POST'});
        const pushData = await pushResp.json();
        if (!pushData.ok) {
            throw new Error(pushData.error || 'Не удалось отправить push');
        }

        await showSystemNotification('Пирожковый Диспетчер', {
            body: 'Тест локального уведомления на этом устройстве',
            icon: '/static/sirius.png',
            badge: '/static/sirius.png',
            tag: 'sirius-local-test',
            renotify: true,
            requireInteraction: true,
        });

        if (result) {
            result.className = pushData.sent > 0 ? 'alert alert-success' : 'alert alert-warning';
            result.textContent = pushData.sent > 0
                ? 'Тест отправлен: локальное уведомление + настоящий push через сервер.'
                : 'Локальный тест показан, но сохранённой push-подписки сервер не нашёл. Нажми «Включить» ещё раз.';
        }
    } catch (e) {
        if (result) {
            result.className = 'alert alert-error';
            result.textContent = e.message || String(e);
        } else {
            alert(e.message || e);
        }
    } finally {
        if (typeof updateNotificationSettingsStatus === 'function') {
            updateNotificationSettingsStatus();
        }
    }
}

async function getPushRegistration(createIfMissing = true) {
    if (!('serviceWorker' in navigator)) return null;
    let registration = await navigator.serviceWorker.getRegistration('/');
    if (!registration && createIfMissing) {
        registration = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
    }
    if (registration) {
        try { await registration.update(); } catch (e) {}
    }
    if (createIfMissing && navigator.serviceWorker.ready) {
        try { registration = await navigator.serviceWorker.ready; } catch (e) {}
    }
    return registration;
}

async function registerPushNotifications() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        throw new Error('Этот браузер не поддерживает push-уведомления');
    }
    const keyResp = await fetch('/api/push/public-key');
    const keyData = await keyResp.json();
    if (!keyData.ok) {
        throw new Error(keyData.error || 'Не удалось получить ключ push');
    }
    const registration = await getPushRegistration(true);
    if (!registration) {
        throw new Error('Не удалось зарегистрировать service worker');
    }
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
        subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(keyData.public_key),
        });
    }
    const saveResp = await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(subscription),
    });
    const saveData = await saveResp.json();
    if (!saveData.ok) {
        throw new Error(saveData.error || 'Не удалось сохранить push-подписку');
    }
}

async function showSystemNotification(title, options) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return false;
    const registration = await getPushRegistration(false);
    if (registration && registration.showNotification) {
        await registration.showNotification(title, options);
        return true;
    }
    try {
        new Notification(title, options);
        return true;
    } catch (e) {
        return false;
    }
}

function handleNotificationMessage(msg) {
    const text = String(msg);
    if (text.startsWith('🚨')) {
        showAlarmOverlay(text, {icon: '🚨', title: 'Тревога БПЛА', radar: true});
    } else if (text.startsWith('🔔')) {
        showAlarmOverlay(text);
    }
}

function playAlarmTone(duration) {
    if (!_alarmUnlocked) return;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;
    if (!_alarmAudioCtx) {
        _alarmAudioCtx = new AudioContextClass();
    }
    const ctx = _alarmAudioCtx;
    if (ctx.state === 'suspended') {
        ctx.resume();
    }
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'square';
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.22, ctx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + duration);
}

function playAlarmPattern() {
    if (!_alarmUnlocked) return;
    [0, 350, 700, 1300, 1650, 2000].forEach(delay => {
        setTimeout(() => playAlarmTone(0.22), delay);
    });
}

function startAlarmRepeat() {
    stopAlarmRepeat();
    playAlarmPattern();
    if (navigator.vibrate) {
        navigator.vibrate([700, 200, 700, 200, 1000]);
    }
    _alarmRepeatTimer = setInterval(() => {
        playAlarmPattern();
        if (navigator.vibrate) {
            navigator.vibrate([700, 200, 700, 200, 1000]);
        }
    }, 5500);
}

function stopAlarmRepeat() {
    if (_alarmRepeatTimer) {
        clearInterval(_alarmRepeatTimer);
        _alarmRepeatTimer = null;
    }
    if (navigator.vibrate) {
        navigator.vibrate(0);
    }
}

function showAlarmOverlay(msg, options = {}) {
    let overlay = document.getElementById('alarm-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'alarm-overlay';
        overlay.className = 'alarm-overlay';
        document.body.appendChild(overlay);
    }
    overlay.innerHTML = `
        <div class="alarm-card${options.radar ? ' alarm-card--radar' : ''}">
            <div class="alarm-card__icon">${options.icon || '🔔'}</div>
            <div class="alarm-card__title">${options.title || 'Напоминание'}</div>
            <div class="alarm-card__text">${escapeHtml(msg)}</div>
            <button class="btn btn-primary btn-block" onclick="dismissAlarm()">ОК</button>
        </div>
    `;
    overlay.style.display = 'flex';
    startAlarmRepeat();
}

function dismissAlarm() {
    stopAlarmRepeat();
    const overlay = document.getElementById('alarm-overlay');
    if (overlay) overlay.style.display = 'none';
}

// Auto-dismiss notifications after 8 seconds
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.notification').forEach(el => {
        setTimeout(() => {
            if (el.parentElement) el.remove();
        }, 8000);
    });
});
