# 04 — Flujos del sistema

## Flujo 1 — Autenticación (login con Cognito)

### Por qué se necesita un workaround con Lambda

Cuando el usuario inicia sesión en Cognito, el servicio le devuelve un `code` temporal a una URL que vos configuraste. Para convertir ese `code` en tokens reales (Access Token, ID Token), hace falta ejecutar código — hacer un POST a Cognito con el code.

S3 no puede ejecutar código. Solo sirve archivos estáticos. Por eso se necesita una Lambda como intermediario.

### El flujo paso a paso

```
1. Usuario abre el frontend en S3
           ↓
2. Hace click en "Iniciar sesión"
   Frontend redirige a la Cognito Hosted UI
   (una página de login que AWS genera automáticamente)
           ↓
3. Usuario ingresa email y contraseña
   Cognito verifica las credenciales
   Cognito genera un "code" temporal de un solo uso
           ↓
4. Cognito redirige al usuario a:
   API Gateway → /callback?code=abc123
           ↓
5. API Gateway invoca la Lambda "auth-callback"
   Lambda hace el intercambio:
     POST a Cognito: "dame los tokens reales a cambio de este code"
     Cognito responde con: Access Token + ID Token + Refresh Token
           ↓
6. Lambda redirige al usuario de vuelta al frontend en S3:
   https://jetsmart-frontend.s3.../index.html#token=xyz
           ↓
7. El JavaScript del frontend lee los tokens desde la URL
   Los guarda en localStorage del navegador
           ↓
8. A partir de ahora, cada request al backend incluye el Access Token
   en el header: Authorization: Bearer <token>
           ↓
9. La Lambda de chat verifica el token antes de procesar cada request
```

### Cognito trigger — post-registro

Cuando un usuario se registra por primera vez (no cuando inicia sesión), Cognito puede invocar automáticamente una Lambda. Esta Lambda:
- Asigna al usuario nuevo al grupo `users` de Cognito
- Crea su perfil inicial en DynamoDB (historial vacío, sin reservas)

```
Usuario completa el registro en Cognito Hosted UI
        ↓
Cognito trigger invoca Lambda "cognito-trigger"
        ↓
Lambda asigna el usuario al grupo "users"
Lambda crea perfil en DynamoDB
```

---

## Flujo 2 — Mensaje del chatbot

El chat es **sincrónico**: la Lambda responde en la misma invocación, sin colas ni async.

```
Usuario escribe "quiero volar de Buenos Aires a Mendoza el 15 de junio"
        ↓
Frontend incluye el mensaje + Access Token en el header
Hace POST a: https://<api-gateway-url>/api/chat
        ↓
API Gateway invoca la Lambda chat-handler
        ↓
Lambda verifica el Access Token con Cognito
Si el token es inválido → responde 401 Unauthorized
Si el token es válido → continúa
        ↓
Lambda identifica al usuario por el ID del token
Carga el historial de conversación de esta sesión desde DynamoDB
        ↓
Lambda construye el prompt para Claude:
  [system prompt: "Sos el asistente de JetSmart..."]
  [historial: todos los mensajes anteriores]
  [mensaje nuevo: "quiero volar de BUE a MZA el 15 de junio"]
        ↓
Lambda llama a la API de Anthropic (claude-sonnet-4-6)
usando la API key leída de Secrets Manager
        ↓
Anthropic devuelve la respuesta generada
        ↓
Lambda guarda el intercambio en DynamoDB (sincrónico — necesario para el próximo mensaje):
  { rol: "usuario",    mensaje: "quiero volar..." }
  { rol: "asistente", mensaje: "Encontré vuelos para el 15 de junio..." }
        ↓
Lambda publica evento en SNS "events" (asincrónico — no espera):
  { tipo: "busqueda_vuelo", origen: "BUE", destino: "MZA", fecha: "2026-06-15", usuario_id: "..." }
        ↓
Lambda devuelve la respuesta al frontend
        ↓
Frontend muestra el mensaje
```

**¿Por qué DynamoDB es sincrónico y SNS es asincrónico?**

Si el historial se escribiera asincrónico, el segundo mensaje del usuario podría llegar antes de que el primer mensaje se guardara — y el LLM no lo tendría en el historial. Con DynamoDB se hace sync (~1ms, es rápido). Con SNS/SQS se hace async porque analytics no es urgente y no queremos que una lentitud de RDS frene al usuario.

---

## Flujo 3 — Pago de vuelo (patrón TALO)

El flujo de pago es **asincrónico**: la Lambda de inicio devuelve `202 Accepted` inmediatamente y la cadena corre en background.

```
Usuario confirma el pago en el chat
        ↓
Frontend hace POST a: /api/payment
con { vuelo, pasajeros, tarifa, ... }
        ↓
API Gateway invoca Lambda payment-initiate
Lambda valida el request básico
Lambda pone mensaje en SQS payment-validate-queue
Lambda devuelve 202 Accepted + { payment_id: "pay-xyz" }
        ↓ (usuario recibe respuesta inmediata)

[CADENA ASYNC — corre en background]

SQS payment-validate-queue → Lambda payment-validate
        ↓
Lambda verifica disponibilidad del vuelo en DynamoDB
Si no hay asientos → publica en SNS "payment-failed" → notificación al usuario
Si hay asientos → publica en SNS "payment-validated"
        ↓
SQS payment-reserve-queue → Lambda payment-reserve
        ↓
Lambda decrementa asientos disponibles en DynamoDB
Lambda escribe reserva con estado PENDIENTE en DynamoDB
Lambda publica en SNS "payment-reserved"
        ↓
SQS payment-process-queue → Lambda payment-process
        ↓
Lambda procesa el cobro (mock — en producción sería la pasarela de pagos)
Si el cobro falla → compensa: publica en SNS para liberar asientos
Si el cobro OK → publica en SNS "payment-processed"
        ↓
SQS payment-confirm-queue → Lambda payment-confirm
        ↓
Lambda actualiza la reserva de PENDIENTE → CONFIRMADA en DynamoDB
Lambda publica en SNS "payment-completed"
        ↓
SNS "payment-completed" hace FAN-OUT a 3 queues en paralelo:

├─→ SQS boarding-queue → Lambda boarding-pass
│           ↓
│   Lambda genera el boarding pass (texto/PDF)
│   Lambda sube el archivo a S3: boarding-passes/{reserva_id}/{pasajero_id}.txt
│   Lambda genera pre-signed URL (válida 15 min)
│   Lambda notifica al usuario con la URL

├─→ SQS notifications-queue → Lambda notification
│           ↓
│   Lambda envía confirmación de compra al usuario
│   (email de confirmación con los datos de la reserva)

└─→ SQS analytics-queue → Lambda analytics-processor
            ↓
    Lambda escribe evento "compra_completada" en RDS PostgreSQL
    Dato disponible en el dashboard del admin
```

### Transacciones compensatorias

Si un paso falla (por ejemplo, el cobro es rechazado), el sistema ejecuta la compensación:

```
payment-process falla
        ↓
Lambda publica en SNS "payment-failed"
        ↓
Lambda libera los asientos reservados en DynamoDB (compensación)
Lambda actualiza la reserva a estado FALLIDA
Lambda notifica al usuario que el pago fue rechazado
```

---

## Flujo 4 — Analytics (procesamiento de eventos)

El procesamiento de analytics está desacoplado del chat. La Lambda de chat no escribe directamente en RDS — publica un evento en SNS y sigue. La Lambda `analytics-processor` lo procesa de forma asincrónica.

```
Lambda chat-handler termina de procesar un mensaje
        ↓
Lambda publica en SNS "events":
  { tipo: "busqueda_vuelo", origen: "BUE", destino: "MZA",
    fecha: "2026-06-15", usuario_id: "..." }
        ↓
        [asincrónico — Lambda chat ya respondió al usuario]
        ↓
SQS analytics-queue recibe el mensaje
Lambda "analytics-processor" se activa (trigger de SQS)
        ↓
Lambda obtiene las credenciales de RDS desde Secrets Manager
Lambda conecta a RDS y ejecuta INSERT en la tabla de eventos
        ↓
Dato disponible en RDS para el dashboard del admin
```

### Eventos que se registran en RDS

| Tipo de evento | Cuándo |
|---|---|
| `busqueda_vuelo` | Usuario busca vuelos para una ruta/fecha |
| `vuelo_seleccionado` | Usuario elige un vuelo y tarifa |
| `compra_completada` | Pago completado exitosamente |
| `abandono_flujo` | Usuario llega a un paso pero no continúa |
| `checkin_realizado` | Usuario hace check-in |
| `reclamo_iniciado` | Usuario abre un reclamo |
| `boarding_pass_descargado` | Usuario descarga su pase de abordaje |

---

## Flujo 5 — Boarding pass

```
Lambda boarding-pass recibe mensaje de SQS (triggered por SNS payment-completed)
        ↓
Lambda lee los datos de la reserva y pasajeros desde DynamoDB
Lambda genera el contenido del boarding pass
        ↓
Lambda sube el archivo al bucket S3 "jetsmart-assets":
  ruta: boarding-passes/{reserva_id}/{pasajero_id}.txt
        ↓
Lambda genera una pre-signed URL de S3
(URL temporal válida por 15 minutos, solo para este archivo)
        ↓
Lambda notifica al usuario con la URL de descarga
        ↓
Usuario descarga el boarding pass directamente desde S3
(el bucket es privado — solo funciona con la URL firmada)
```

---

## Flujo 6 — Dashboard del admin

```
Admin inicia sesión en Cognito
El token incluye que pertenece al grupo "admins"
        ↓
Frontend detecta el grupo "admins" en el token
Muestra el dashboard de analytics en lugar del chatbot
        ↓
Frontend hace requests autenticados:
  GET /api/admin/metrics
        ↓
API Gateway invoca Lambda admin-metrics
        ↓
Lambda verifica que el token pertenece al grupo "admins"
Si no es admin → responde 403 Forbidden
Si es admin → conecta a RDS y ejecuta consultas SQL:
  SELECT ruta, COUNT(*) as total
  FROM eventos
  WHERE tipo = 'busqueda_vuelo'
  GROUP BY ruta
  ORDER BY total DESC
        ↓
Lambda devuelve los datos al frontend
Frontend renderiza los gráficos y tablas del dashboard
```

---

## Flujo 7 — Backups

### DynamoDB → S3

DynamoDB tiene una feature nativa de Export to S3. Se puede disparar manualmente o configurar Point-in-Time Recovery (PITR) para restaurar a cualquier momento de los últimos 35 días.

```
DynamoDB Export to S3 (manual o con PITR habilitado):
  Exporta tabla completa en formato JSON/Parquet
        ↓
  Archivo guardado en: s3://jetsmart-assets/backups/dynamodb/
```

### RDS — backups automáticos

RDS realiza backups automáticos diarios del cluster sin configuración adicional más allá de activarlos. Se configura en Terraform con retención de 7 días.

```
RDS backup automático (diario, medianoche UTC):
  AWS toma un snapshot del cluster completo
  Se retiene por 7 días
  Permite restaurar a cualquier punto dentro de la ventana de retención
```
