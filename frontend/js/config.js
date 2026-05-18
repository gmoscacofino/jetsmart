// Valores inyectados automáticamente durante el deploy (ver .github/workflows/terraform.yml).
// No editar manualmente — correr `terraform apply` para regenerar.
const CONFIG = {
  // Cognito — terraform output cognito_user_pool_id / cognito_client_id
  cognitoRegion:   'us-east-1',
  userPoolId:      'REEMPLAZAR_CON_terraform_output_cognito_user_pool_id',
  clientId:        'REEMPLAZAR_CON_terraform_output_cognito_client_id',
  cognitoDomain:   'REEMPLAZAR_CON_terraform_output_cognito_hosted_ui_url',

  // Redirect URIs — terraform output auth_callback_url / frontend_url
  callbackUrl:     'REEMPLAZAR_CON_terraform_output_auth_callback_url',
  frontendUrl:     'REEMPLAZAR_CON_terraform_output_frontend_url',

  // Backend — terraform output chatbot_api_url
  apiUrl:          'REEMPLAZAR_CON_terraform_output_chatbot_api_url',
};

(function () {
  var missing = Object.keys(CONFIG).filter(function (k) {
    return typeof CONFIG[k] === 'string' && CONFIG[k].indexOf('REEMPLAZAR_') === 0;
  });
  if (missing.length === 0) return;
  console.error('[config] Valores sin inyectar:', missing.join(', '));
  document.addEventListener('DOMContentLoaded', function () {
    var wrap = document.createElement('div');
    wrap.style.cssText = 'padding:2rem;font-family:monospace;color:#c00';
    var h = document.createElement('h2');
    h.textContent = 'Error de configuración';
    var p1 = document.createElement('p');
    p1.textContent = 'Los siguientes valores no fueron inyectados: ' + missing.join(', ');
    var p2 = document.createElement('p');
    p2.textContent = 'Correr terraform apply y re-deployar el frontend.';
    wrap.appendChild(h);
    wrap.appendChild(p1);
    wrap.appendChild(p2);
    while (document.body.firstChild) { document.body.removeChild(document.body.firstChild); }
    document.body.appendChild(wrap);
  });
}());
