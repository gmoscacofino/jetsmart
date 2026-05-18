// Actualizar estos valores con los outputs de `terraform output` luego del apply.
// Ver README.md — Paso 5 para los comandos exactos.
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
