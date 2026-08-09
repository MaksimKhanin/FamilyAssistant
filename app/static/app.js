/* Установка приложения и подписка на уведомления.
 *
 * Всё, что связано с push, живёт здесь, а экран профиля только вызывает
 * familyPush.enable() / disable() / refresh().
 */

(function () {
  'use strict';

  const SW_URL = '/sw.js';
  let registration = null;

  function base64ToUint8(base64) {
    const padded = (base64 + '='.repeat((4 - base64.length % 4) % 4))
      .replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(padded);
    return Uint8Array.from([...raw].map(ch => ch.charCodeAt(0)));
  }

  async function ready() {
    if (!('serviceWorker' in navigator)) return null;
    if (registration) return registration;
    registration = await navigator.serviceWorker.register(SW_URL);
    await navigator.serviceWorker.ready;
    return registration;
  }

  async function currentSubscription() {
    const reg = await ready();
    return reg ? reg.pushManager.getSubscription() : null;
  }

  /** Что показывать на экране профиля: поддержка, разрешение, подписка. */
  async function status() {
    const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
    if (!supported) {
      // Самый частый случай — iPhone, где сайт не добавлен на главный экран.
      const iOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
      return { supported: false, reason: iOS ? 'ios-needs-install' : 'unsupported', subscribed: false };
    }
    const subscription = await currentSubscription();
    return {
      supported: true,
      permission: Notification.permission,
      subscribed: Boolean(subscription),
      standalone: window.matchMedia('(display-mode: standalone)').matches,
    };
  }

  async function enable() {
    const state = await status();
    if (!state.supported) return state;

    const permission = await Notification.requestPermission();
    if (permission !== 'granted') return { ...state, permission, subscribed: false };

    const keyResponse = await fetch('/push/key');
    const { key, configured } = await keyResponse.json();
    if (!configured || !key) return { ...state, error: 'not-configured' };

    const reg = await ready();
    const subscription = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: base64ToUint8(key),
    });

    await fetch('/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(subscription.toJSON()),
    });
    return await status();
  }

  async function disable() {
    const subscription = await currentSubscription();
    if (subscription) {
      await fetch('/push/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: subscription.endpoint }),
      });
      await subscription.unsubscribe();
    }
    return await status();
  }

  async function test() {
    const response = await fetch('/push/test', { method: 'POST' });
    return response.json();
  }

  window.familyPush = { status, enable, disable, test, ready };

  // Регистрируем воркер сразу: без него не работают ни уведомления, ни установка.
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => ready().catch(() => {
      /* HTTP без TLS — service worker недоступен, панель работает как обычный сайт */
    }));
  }

  // Кнопка «Установить» появляется, только когда браузер сам считает это уместным.
  let installPrompt = null;
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installPrompt = event;
    document.querySelectorAll('[data-install-app]').forEach(el => el.hidden = false);
  });

  window.familyInstall = async function () {
    if (!installPrompt) return 'unavailable';
    installPrompt.prompt();
    const { outcome } = await installPrompt.userChoice;
    installPrompt = null;
    document.querySelectorAll('[data-install-app]').forEach(el => el.hidden = true);
    return outcome;
  };
})();
