const Chat = (() => {
  // crypto.randomUUID() requires HTTPS (secure context). Use a fallback for HTTP (S3 website endpoint).
  let sessionId = (crypto.randomUUID ?? (() =>
    'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    })
  ))();

  // Cached DOM references — set once in init(), reused in hot paths
  let _container, _typingIndicator, _chatInput, _btnSend;

  function init() {
    _container       = document.getElementById('messages-container');
    _typingIndicator = document.getElementById('typing-indicator');
    _chatInput       = document.getElementById('chat-input');
    _btnSend         = document.getElementById('btn-send');

    const chatMain          = document.querySelector('.chat-main');
    const reservationsPanel = document.getElementById('panel-reservations');

    document.querySelectorAll('[data-panel]').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = btn.dataset.panel;
        document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        if (target === 'chat') {
          chatMain.style.display = 'flex';
          reservationsPanel.classList.remove('active');
        } else if (target === 'reservations') {
          chatMain.style.display = 'none';
          reservationsPanel.classList.add('active');
          loadReservations();
        }
      });
    });
  }

  function formatTime(date) {
    return date.toLocaleTimeString('es-AR', { hour: '2-digit', minute: '2-digit' });
  }

  function buildBubble(text) {
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    // Escape raw HTML entities before Markdown parsing so any injected HTML from
    // echoed user input is neutralised. marked.parse() then only produces its own
    // structural tags (strong, em, table, ul, etc.) — no arbitrary HTML passes through.
    const safe = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    // eslint-disable-next-line no-unsanitized/property
    bubble.innerHTML = marked.parse(safe, { breaks: true, gfm: true });
    return bubble;
  }

  function appendMessage(role, text) {
    const container = _container;
    const isUser = role === 'user';

    const wrapper = document.createElement('div');
    wrapper.className = `message message-${isUser ? 'user' : 'assistant'}`;

    if (!isUser) {
      const avatar = document.createElement('div');
      avatar.className = 'message-avatar';
      avatar.insertAdjacentHTML('beforeend', '<svg width="16" height="16"><use href="#icon-jet"/></svg>');
      wrapper.appendChild(avatar);
    }

    wrapper.appendChild(buildBubble(text));

    const time = document.createElement('span');
    time.className = 'message-time';
    time.textContent = formatTime(new Date());
    wrapper.appendChild(time);

    container.appendChild(wrapper);
    container.scrollTop = container.scrollHeight;
    return wrapper;
  }

  function setTyping(visible) {
    _typingIndicator.style.display = visible ? 'flex' : 'none';
  }

  function setInputDisabled(disabled) {
    _chatInput.disabled = disabled;
    _btnSend.disabled   = disabled;
    document.querySelectorAll('.quick-btn').forEach(b => { b.disabled = disabled; });
  }

  function renderOptions(msgWrapper, options) {
    const actions = document.createElement('div');
    actions.className = 'quick-actions';
    let handled = false;  // guard contra doble click en el mismo frame
    options.forEach(opt => {
      const btn = document.createElement('button');
      btn.className = 'quick-btn';
      btn.textContent = opt;
      btn.addEventListener('click', () => {
        if (handled) return;
        handled = true;
        actions.remove();
        sendMessage(opt);
      });
      actions.appendChild(btn);
    });
    msgWrapper.querySelector('.message-bubble').appendChild(actions);
    _container.scrollTop = _container.scrollHeight;
  }

  function _errorMessage(err) {
    if (!navigator.onLine) {
      return 'Sin conexión a internet. Revisá tu red e intentá de nuevo.';
    }
    const status = err.httpStatus;
    if (!status) {
      return `No se pudo conectar con el servidor. Verificá tu conexión e intentá de nuevo.\n\nDetalle técnico: ${err.message}`;
    }
    const detail = err.serverMsg ? `\n\nDetalle: ${err.serverMsg}` : '';
    if (status === 502 || status === 503) {
      return `El servicio de IA no está disponible en este momento (error ${status}). Intentá en unos segundos.${detail}`;
    }
    if (status === 429) {
      return `Demasiadas solicitudes. Esperá unos segundos antes de volver a intentarlo (error ${status}).${detail}`;
    }
    if (status >= 500) {
      return `Error interno del servidor (${status}). Si el problema persiste, recargá la página.${detail}`;
    }
    if (status >= 400) {
      return `Error en la solicitud (${status}). Intentá de nuevo o recargá la página.${detail}`;
    }
    return `Error inesperado (${status}). Intentá de nuevo.${detail}`;
  }

  async function sendMessage(text) {
    if (!text.trim()) return;
    appendMessage('user', text);
    setTyping(true);
    setInputDisabled(true);

    try {
      const token = Auth.getToken();
      const resp = await fetch(`${CONFIG.apiUrl}/api/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`,
        },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });
      if (resp.status === 401) { Auth.logout(); return; }
      if (!resp.ok) {
        let serverMsg = '';
        try { serverMsg = (await resp.json()).error || ''; } catch { /* no JSON */ }
        throw Object.assign(new Error(`HTTP ${resp.status}`), { httpStatus: resp.status, serverMsg });
      }
      const data = await resp.json();
      setTyping(false);
      const wrapper = appendMessage('assistant', data.response || 'Lo siento, no pude procesar tu consulta.');
      if (data.options && data.options.length > 0) {
        renderOptions(wrapper, data.options);
      }
      // Hold countdown — el backend manda metadata.hold cuando el user
      // acaba de holdear un asiento, o metadata.hold_cleared al confirmar/liberar
      if (data.metadata) {
        if (data.metadata.hold && data.metadata.hold.expires_at_epoch) {
          HoldBanner.start(data.metadata.hold);
        } else if (data.metadata.hold_cleared) {
          HoldBanner.hide();
        }
      }
    } catch (err) {
      setTyping(false);
      const msg = _errorMessage(err);
      appendMessage('assistant', msg);
      console.error('[Chat]', err);
    } finally {
      setInputDisabled(false);
      _chatInput.focus();
    }
  }

  async function loadReservations() {
    const panel = document.getElementById('panel-reservations');

    const loading = document.createElement('p');
    loading.className = 'reservations-loading';
    loading.textContent = 'Cargando reservas...';
    panel.replaceChildren(loading);

    try {
      const token = Auth.getToken();
      const resp = await fetch(`${CONFIG.apiUrl}/api/reservations`, {
        headers: { 'Authorization': `Bearer ${token}` },
      });
      if (resp.status === 401) { Auth.logout(); return; }
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const reservations = data.reservations || [];

      if (reservations.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'reservations-empty';
        const icon = document.createElement('p');
        icon.className = 'reservations-empty-icon';
        icon.textContent = '✈️';
        const msg = document.createElement('p');
        msg.textContent = 'No tenés reservas activas todavía.';
        empty.append(icon, msg);
        panel.replaceChildren(empty);
        return;
      }

      const cards = reservations.map(r => {
        const card = document.createElement('div');
        card.className = 'reservation-card';

        const route = document.createElement('div');
        route.className = 'reservation-route';
        route.textContent = `${r.origen} → ${r.destino}`;

        const detail = document.createElement('div');
        detail.className = 'reservation-detail';
        detail.textContent = `${r.vuelo_numero} · ${r.fecha} · ${r.pasajeros} pasajero(s)`;

        const status = document.createElement('div');
        status.className = 'reservation-status';
        status.textContent = r.status;

        const code = document.createElement('div');
        code.className = 'reservation-code';
        code.textContent = r.reservation_id || '';

        card.append(route, detail, status, code);
        return card;
      });
      panel.replaceChildren(...cards);
    } catch {
      const err = document.createElement('p');
      err.className = 'reservations-error';
      err.textContent = 'Error al cargar reservas. Intentá de nuevo.';
      panel.replaceChildren(err);
    }
  }

  return {
    init,
    send(event) {
      event.preventDefault();
      const text = _chatInput.value.trim();
      _chatInput.value = '';
      sendMessage(text);
    },
    sendQuick(text) {
      sendMessage(text);
    },
  };
})();

/**
 * HoldBanner — countdown visual del soft-hold de asiento.
 * Maneja el banner sticky con timer regresivo.
 * Estado vive en memoria (no localStorage) para que se resetee al refrescar
 * la página — la fuente de verdad es el backend (se restablece con check_hold_status).
 */
const HoldBanner = (() => {
  let _intervalId = null;
  let _expiresAtMs = 0;

  function _$(id) { return document.getElementById(id); }
  function _pad(n) { return n < 10 ? '0' + n : String(n); }

  function _tick() {
    const now = Date.now();
    const remainingMs = _expiresAtMs - now;
    const banner = _$('hold-banner');
    const timer = _$('hold-banner-timer');
    if (!banner || !timer) return;

    if (remainingMs <= 0) {
      timer.textContent = '0:00';
      banner.classList.add('expiring');
      _stopInterval();
      // Después de un segundo, mostrar mensaje informativo dentro del chat
      setTimeout(() => {
        const msgContainer = _$('messages-container');
        if (msgContainer) {
          const div = document.createElement('div');
          div.className = 'message message-assistant';

          const avatar = document.createElement('div');
          avatar.className = 'message-avatar';
          const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
          svg.setAttribute('width', '16');
          svg.setAttribute('height', '16');
          const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
          use.setAttribute('href', '#icon-jet');
          svg.appendChild(use);
          avatar.appendChild(svg);

          const bubble = document.createElement('div');
          bubble.className = 'message-bubble';
          const p = document.createElement('p');
          p.textContent = '⏱️ Tu hold de asiento venció. Decile al asistente "verificá mi asiento" para ver si sigue libre.';
          bubble.appendChild(p);

          const time = document.createElement('span');
          time.className = 'message-time';
          time.textContent = 'Ahora';

          div.append(avatar, bubble, time);
          msgContainer.appendChild(div);
          msgContainer.scrollTop = msgContainer.scrollHeight;
        }
        hide();
      }, 1200);
      return;
    }

    const totalSec = Math.floor(remainingMs / 1000);
    const mins = Math.floor(totalSec / 60);
    const secs = totalSec % 60;
    timer.textContent = `${mins}:${_pad(secs)}`;

    // Last 60 seconds → pulse rojo
    if (remainingMs <= 60000) {
      banner.classList.add('expiring');
    } else {
      banner.classList.remove('expiring');
    }
  }

  function _stopInterval() {
    if (_intervalId) { clearInterval(_intervalId); _intervalId = null; }
  }

  function start(holdData) {
    if (!holdData || !holdData.expires_at_epoch) return;
    _expiresAtMs = holdData.expires_at_epoch * 1000;
    const banner = _$('hold-banner');
    if (!banner) return;
    _$('hold-banner-seat').textContent = holdData.seat_id || '--';
    const flight = holdData.vuelo_numero
      ? `${holdData.vuelo_numero}${holdData.fecha ? ' · ' + holdData.fecha : ''}`
      : '--';
    _$('hold-banner-flight').textContent = flight;
    banner.style.display = 'flex';
    banner.classList.remove('expiring');
    _stopInterval();
    _tick();
    _intervalId = setInterval(_tick, 1000);
  }

  function hide() {
    _stopInterval();
    const banner = _$('hold-banner');
    if (banner) {
      banner.style.display = 'none';
      banner.classList.remove('expiring');
    }
  }

  return { start, hide };
})();
