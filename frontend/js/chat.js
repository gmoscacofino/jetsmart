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
        route.textContent = `${r.origin} → ${r.destination}`;

        const detail = document.createElement('div');
        detail.className = 'reservation-detail';
        detail.textContent = `${r.flight_number} · ${r.date} · ${r.passengers} pasajero(s)`;

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
