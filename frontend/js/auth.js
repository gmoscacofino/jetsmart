const Auth = (() => {
  const TOKEN_KEY = 'jetsmart_id_token';

  // crypto.randomUUID() solo funciona en secure contexts (HTTPS).
  // El frontend se sirve por S3 website hosting (HTTP), así que en ese caso
  // generamos un UUIDv4 con crypto.getRandomValues() — sí disponible en HTTP
  // y criptográficamente seguro (relevante porque el state previene CSRF).
  function uuid() {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      try { return crypto.randomUUID(); } catch (_) {}
    }
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
    const hex = [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }

  function buildCognitoUrl(path, responseType) {
    const state = uuid();
    sessionStorage.setItem('oauth_state', state);
    const params = new URLSearchParams({
      response_type: responseType,
      client_id:     CONFIG.clientId,
      redirect_uri:  CONFIG.callbackUrl,
      scope:         'openid email profile',
      state,
    });
    return `${CONFIG.cognitoDomain}/${path}?${params}`;
  }

  return {
    login() {
      window.location.href = buildCognitoUrl('oauth2/authorize', 'code');
    },

    register() {
      window.location.href = buildCognitoUrl('signup', 'code');
    },

    logout() {
      // Limpiar el token local primero — si algo del flujo Cognito falla,
      // el user al menos queda sin token en el browser.
      localStorage.removeItem(TOKEN_KEY);
      sessionStorage.removeItem('oauth_state');
      // Disparar el logout de Cognito para invalidar la cookie de sesión del
      // Hosted UI. Sin esto, un nuevo "Login" auto-loguea sin password.
      //
      // Cognito requiere HTTPS en logout_uri (igual que en redirect_uri del
      // login), pero el frontend está en S3 HTTP. Solución: usar el endpoint
      // /logout del auth-api (HTTPS) como bridge — la Lambda hace el 302 final
      // al frontend. Mismo patrón que el callback. Derivamos la URL del logout
      // bridge reemplazando /callback por /logout en callbackUrl.
      const logoutBridge = CONFIG.callbackUrl.replace(/\/callback$/, '/logout');
      const params = new URLSearchParams({
        client_id:  CONFIG.clientId,
        logout_uri: logoutBridge,
      });
      window.location.href = `${CONFIG.cognitoDomain}/logout?${params}`;
    },

    // Called from App.init() when URL hash contains tokens
    // Tokens arrive here after the Lambda auth_callback redirects back with #token=...
    handleCallback(hash) {
      // Cognito requires HTTPS for the redirect URI, but the API is HTTP-only (no ACM in Academy).
      // If we're on the HTTPS S3 REST endpoint, move immediately to the HTTP website endpoint
      // so the browser allows calls to the HTTP ALB (mixed-content block is avoided).
      // The token travels in the URL hash, which is never sent to any server.
      if (window.location.protocol === 'https:' && window.location.hostname.endsWith('amazonaws.com')) {
        window.location.replace(CONFIG.frontendUrl + hash);
        return;
      }

      const params        = new URLSearchParams(hash.replace(/^#/, ''));
      const returnedState = params.get('state');
      const savedState    = sessionStorage.getItem('oauth_state');
      sessionStorage.removeItem('oauth_state');
      if (!returnedState || returnedState !== savedState) return;

      const token = params.get('id_token') || params.get('token');
      if (token) {
        localStorage.setItem(TOKEN_KEY, token);
      }
      // Clean the hash from the URL so tokens don't stay visible
      history.replaceState(null, '', window.location.pathname);
      // Re-init now that token is saved
      App.init();
    },

    getToken() {
      const token = localStorage.getItem(TOKEN_KEY);
      if (!token) return null;
      const payload = this.parseJWT(token);
      if (payload && payload.exp && payload.exp < Math.floor(Date.now() / 1000)) {
        localStorage.removeItem(TOKEN_KEY);
        return null;
      }
      return token;
    },

    parseJWT(token) {
      try {
        const base64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
        const json = atob(base64);
        return JSON.parse(json);
      } catch {
        return null;
      }
    },
  };
})();
