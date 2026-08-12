/* Весь клиентский код панели.
 *
 * Здесь он лежит не для порядка, а по необходимости: переход подменяет тело
 * документа целиком (ADR-0001), поэтому скрипт внутри тела выполнялся бы заново
 * при каждом переходе — `const` падал бы с SyntaxError, а слушатели копились бы
 * дубликатами. Этот файл подключён из <head>, который не подменяется, и
 * выполняется ровно один раз за сеанс.
 *
 * Отсюда же следует деление внутри файла:
 *   — «однажды» — то, что настраивается один раз: htmx, чат, горячие клавиши;
 *   — «на каждом экране» — initScreen(), которая вызывается и на первой загрузке,
 *     и после каждого перехода, потому что элементы экрана каждый раз новые.
 *
 * Шаблоны зовут отсюда две вещи: familyPush (экран профиля) и panel (всё
 * остальное, из onclick).
 */

(function () {
  'use strict';

  /* ======================= уведомления и установка ======================= */

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
  // Событие приходит один раз за сеанс, а кнопка после каждого перехода новая,
  // поэтому ответ браузера храним здесь, а показывает кнопку initScreen().
  let installPrompt = null;
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installPrompt = event;
    initScreen();
  });

  window.familyInstall = async function () {
    if (!installPrompt) return 'unavailable';
    installPrompt.prompt();
    const { outcome } = await installPrompt.userChoice;
    installPrompt = null;
    document.querySelectorAll('[data-install-app]').forEach(el => el.hidden = true);
    return outcome;
  };

  /* =========================== каркас панели ============================ */

  // Переписка тянется по первому открытию, а не вместе с каждым экраном:
  // человек чаще открывает панель посмотреть баланс дня, чем поговорить.
  // Признак «уже загружено» — сама переписка в панели, а не отдельный флаг:
  // после неудачной попытки панель осталась бы пустой навсегда.
  let chatLoading = false;

  function loadChat() {
    const panelElement = document.getElementById('chat-panel');
    if (chatLoading || panelElement.querySelector('#chat-body')) return;
    chatLoading = true;
    htmx.ajax('GET', '/chat/panel', { target: '#chat-panel', swap: 'innerHTML' })
        .finally(() => { chatLoading = false; });
  }

  function openChat() {
    loadChat();
    setChatOpen(true);
  }

  function closeChat() {
    setChatOpen(false);
  }

  /** Панель переживает переход, а подложка под ней рисуется заново — состояние
   *  этих двух вещей нужно держать вместе, иначе панель остаётся висеть поверх
   *  нового экрана без возможности её закрыть. */
  function setChatOpen(open) {
    document.getElementById('chat-panel').classList.toggle('open', open);
    document.getElementById('chat-backdrop').classList.toggle('open', open);
    document.body.classList.toggle('no-scroll', open);
  }

  function openDrawer() {
    document.getElementById('sidebar').classList.add('open');
    document.getElementById('drawer-backdrop').classList.add('open');
  }

  function closeDrawer() {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('drawer-backdrop').classList.remove('open');
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeChat(); closeDrawer(); }
  });

  /* ================================ чат ================================= */

  // Чат ведёт себя как мессенджер: лента всегда прокручена вниз.
  function scrollChat() {
    const body = document.getElementById('chat-body');
    if (body) body.scrollTop = body.scrollHeight;
  }

  document.body.addEventListener('htmx:afterSwap', scrollChat);

  // Сообщение показывается сразу, не дожидаясь ответа: ответ идёт секунды, и
  // всё это время человек видел свой текст в поле ввода и не понимал, ушло ли
  // оно. Поле при этом заперто (hx-disabled-elt на форме) — второй раз не отправить.
  let sending = '';
  document.body.addEventListener('htmx:beforeRequest', e => {
    if (e.detail.elt.id !== 'chat-form') return;
    const input = document.getElementById('chat-input');
    // htmx уже забрал значение в тело запроса, поле можно чистить.
    sending = input.value;
    input.value = '';
    const text = sending.trim();
    if (text) {
      const bubble = document.createElement('div');
      bubble.className = 'bubble user';
      bubble.textContent = text;
      document.getElementById('chat-body').appendChild(bubble);
    }
    scrollChat();
  });

  // Не дошло — возвращаем текст в поле, чтобы не набирать заново.
  document.body.addEventListener('htmx:afterRequest', e => {
    if (e.detail.elt.id !== 'chat-form') return;
    const input = document.getElementById('chat-input');
    if (!e.detail.successful) {
      const failed = document.createElement('div');
      failed.className = 'bubble assistant';
      failed.textContent = 'Сообщение не дошло. Проверьте связь и попробуйте ещё раз.';
      document.getElementById('chat-body').appendChild(failed);
      input.value = sending;
    }
    sending = '';
    scrollChat();
    input.focus();
  });

  /* ============================== переходы ============================== */

  // Снимки экранов в истории не храним: «назад» всегда идёт на сервер за
  // свежим. Причина та же, по которой ничего не кеширует service worker —
  // устаревшая копия баланса дня или событий с камер хуже честного ожидания
  // (ADR-0003).
  htmx.config.historyCacheSize = 0;

  // Мягкий кроссфейд между экранами там, где браузер это умеет. Где не умеет —
  // просто не будет анимации, ничего не ломается. На телефоне кроссфейд выключен:
  // View Transition снимает снапшот всей страницы на каждый переход, и на
  // средних телефонах именно эта пауза ощущалась как «медленное переключение».
  // Вместо него там остаётся короткий fadeUp контента (style.css).
  htmx.config.globalViewTransitions = !window.matchMedia('(max-width: 900px)').matches;

  // Переход по устаревшей ссылке htmx по умолчанию просто проглатывает: ответы
  // 4xx он не показывает. Вкладывать 404 в тело панели тоже нельзя — это
  // отдельная страница без каркаса. Поэтому уходим на неё обычным переходом:
  // документ и так пора начать заново.
  document.body.addEventListener('htmx:beforeSwap', e => {
    if (e.detail.xhr.status === 404) {
      e.detail.shouldSwap = false;
      window.location.href = e.detail.pathInfo.requestPath;
    }
  });

  // Полоса прогресса сверху — но не раньше, чем через четверть секунды, иначе
  // она мигала бы на каждом быстром переходе. Чат исключён: у него своя
  // обратная связь, поле ввода запирается на время отправки.
  // Считаем запросы, а не держим один флаг: ответ чата, прилетевший посреди
  // перехода, иначе погасил бы полосу этого перехода.
  let progressTimer = null;
  let inFlight = 0;

  function watched(event) {
    return event.detail.elt.id !== 'chat-form';
  }

  function startProgress() {
    inFlight += 1;
    if (progressTimer) return;
    progressTimer = setTimeout(() => {
      const bar = document.getElementById('nav-progress');
      if (bar) bar.classList.add('on');
    }, 250);
  }

  function stopProgress() {
    inFlight = Math.max(0, inFlight - 1);
    if (inFlight > 0) return;
    clearTimeout(progressTimer);
    progressTimer = null;
    const bar = document.getElementById('nav-progress');
    if (bar) bar.classList.remove('on');
  }

  document.body.addEventListener('htmx:beforeRequest', e => {
    if (watched(e)) startProgress();
  });
  document.body.addEventListener('htmx:afterRequest', e => {
    if (watched(e)) stopProgress();
  });

  /* ====================== то, что нужно каждому экрану =================== */

  /** Вызывается на первой загрузке и после каждого перехода: элементы новые. */
  function initScreen() {
    // Выдвижное меню на телефоне после перехода должно закрыться само —
    // человек по нему только что и перешёл.
    closeDrawer();

    // Панель чата переход пережила (hx-preserve), подложка под ней — нет.
    // Приводим подложку к тому, что с панелью.
    setChatOpen(document.getElementById('chat-panel').classList.contains('open'));

    const toast = document.getElementById('toast');
    if (toast) setTimeout(() => toast.remove(), 2200);

    document.querySelectorAll('[data-install-app]').forEach(el => {
      el.hidden = !installPrompt;
      el.onclick = window.familyInstall;
    });

    if (document.getElementById('push-state')) paintPushCard();

    collapseSectionStrip();
  }

  /* ============================ экран профиля =========================== */

  const PUSH_HINTS = {
    'ios-needs-install': 'На iPhone уведомления работают только у приложения: откройте «Поделиться» → ' +
                         '«На экран Домой», и кнопка появится.',
    'unsupported': 'Этот браузер не умеет уведомления. Панель работает, но о тревоге узнаете, только открыв её.',
    'not-configured': 'На сервере не заданы ключи VAPID — уведомления пока не отправляются.',
    'denied': 'Уведомления запрещены в настройках браузера для этого сайта — разрешить можно только там.',
  };

  function paintPushCard() {
    status().then(paintPushState);
  }

  function paintPushState(state) {
    const stateBadge = document.getElementById('push-state');
    const hint = document.getElementById('push-hint');
    const toggle = document.getElementById('push-toggle');
    const testButton = document.getElementById('push-test');
    if (!stateBadge) return;   // ушли с экрана, пока ходили за статусом

    if (!state.supported) {
      stateBadge.textContent = 'недоступны';
      hint.textContent = PUSH_HINTS[state.reason] || PUSH_HINTS.unsupported;
      return;
    }
    if (state.permission === 'denied') {
      stateBadge.textContent = 'запрещены';
      hint.textContent = PUSH_HINTS.denied;
      return;
    }
    if (state.error === 'not-configured') {
      stateBadge.textContent = 'не настроены';
      hint.textContent = PUSH_HINTS['not-configured'];
      return;
    }

    stateBadge.textContent = state.subscribed ? 'включены' : 'выключены';
    stateBadge.className = 'badge spacer ' + (state.subscribed ? 'ok' : 'neutral');
    toggle.hidden = false;
    toggle.textContent = state.subscribed ? 'Выключить на этом устройстве' : 'Включить на этом устройстве';
    toggle.className = state.subscribed ? 'btn ghost' : 'btn';
    testButton.hidden = !state.subscribed;
    toggle.onclick = () => (state.subscribed ? disable() : enable()).then(paintPushState);
    testButton.onclick = () => test().then(showTestResult);
  }

  function showTestResult(result) {
    const text = { ok: 'Отправил — уведомление должно прийти через секунду.',
                   no_devices: 'Ни одного подключённого устройства.',
                   not_configured: 'На сервере не заданы ключи VAPID.' }[result.status];
    document.getElementById('push-devices').textContent = text || 'Не получилось отправить.';
  }

  /* ============================ экран знаний ============================ */

  // Полоса разделов: всё, что не влезло по ширине, уходит в меню «⋯».
  // Активный раздел не прячется никогда — иначе непонятно, где ты находишься.
  // Без JavaScript полоса просто переносится на новые строки (style.css).
  function collapseSectionStrip() {
    const strip = document.querySelector('[data-section-strip]');
    if (!strip) return;
    const more = strip.querySelector('[data-strip-more]');
    const menu = strip.querySelector('[data-strip-menu]');
    // Меню чистится до сбора полос: иначе повторный прогон (resize) собрал бы
    // и собственные клоны из меню и плодил бы дубликаты.
    menu.hidden = true;
    menu.innerHTML = '';
    const chips = [...strip.querySelectorAll('[data-strip-item]')];

    chips.forEach(chip => { chip.hidden = false; });
    more.hidden = true;
    strip.classList.add('nowrap');

    const fits = () => strip.scrollWidth <= strip.clientWidth;
    if (fits()) return;

    more.hidden = false;
    for (let i = chips.length - 1; i >= 0 && !fits(); i--) {
      if (chips[i].hasAttribute('data-strip-active')) continue;
      chips[i].hidden = true;
      const clone = chips[i].cloneNode(true);
      clone.hidden = false;
      menu.prepend(clone);
    }
    // Клоны появились после разбора страницы — иначе переход по ним
    // перезагружал бы документ вместо подмены тела (ADR-0001).
    htmx.process(menu);

    strip.querySelector('[data-strip-more-btn]').onclick = event => {
      event.stopPropagation();
      menu.hidden = !menu.hidden;
    };
  }

  window.addEventListener('resize', collapseSectionStrip);

  // Меню «⋯» закрывается кликом мимо него — как любое выпадающее меню.
  document.addEventListener('click', () => {
    const menu = document.querySelector('[data-strip-menu]');
    if (menu) menu.hidden = true;
  });

  /* ===================== мелочи отдельных экранов ======================= */

  /** Ползунок самостоятельности: подпись рядом с ним. */
  function autonomyLabel(input) {
    const levels = JSON.parse(input.dataset.levels);
    document.getElementById('autonomy-label').textContent = levels[input.value];
  }

  /** Приглашение на экране онбординга: ссылка приходит аргументом. */
  function copyInvite(url, button) {
    const done = () => { button.textContent = 'Ссылка скопирована';
                         setTimeout(() => button.textContent = 'Скопировать приглашение', 2000); };
    // clipboard API есть только на HTTPS — на локальном HTTP показываем ссылку как есть
    if (navigator.clipboard) navigator.clipboard.writeText(url).then(done, () => prompt('Ссылка:', url));
    else prompt('Ссылка для приглашения:', url);
  }

  /** Приглашение на экране семьи: ссылка лежит в поле рядом с кнопкой. */
  function copyInviteField() {
    const field = document.getElementById('invite-link');
    field.select();
    if (navigator.clipboard) navigator.clipboard.writeText(field.value);
    else document.execCommand('copy');   // http без TLS: clipboard API недоступен
  }

  /** Правка КБЖУ руками: кнопки «−» и «+» рядом с числом. */
  function step(id, delta) {
    const input = document.getElementById(id);
    input.value = Math.max(0, (parseInt(input.value, 10) || 0) + delta);
  }

  /** Запись еды: фото или текстом. */
  function showMode(mode, button) {
    document.getElementById('mode-photo').style.display = mode === 'photo' ? '' : 'none';
    document.getElementById('mode-text').style.display  = mode === 'text'  ? '' : 'none';
    button.parentNode.querySelectorAll('button').forEach(b => b.classList.remove('on'));
    button.classList.add('on');
  }

  /** Активность: прикидка потраченных калорий, пока человек печатает. */
  function recalc() {
    const selected = document.querySelector('input[name="kind"]:checked');
    const value = parseFloat(document.getElementById('activity-value').value) || 0;
    const kcal = Math.round(value * parseFloat(selected.dataset.rate));
    document.getElementById('activity-estimate').textContent = '≈ ' + kcal + ' ккал';
  }

  window.panel = {
    openChat, closeChat, openDrawer, closeDrawer,
    autonomyLabel, copyInvite, copyInviteField, step, showMode, recalc,
  };

  // Первая загрузка и каждый последующий переход проходят через это.
  // Именно afterSettle, а не htmx.onLoad: onLoad срабатывает на каждый
  // вставленный узел, то есть по разу на каждый блок экрана, и экран профиля
  // уходил бы за статусом уведомлений полдюжины раз подряд.
  document.addEventListener('DOMContentLoaded', initScreen);
  document.body.addEventListener('htmx:afterSettle', initScreen);
})();
