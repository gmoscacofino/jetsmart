# Justificaciones — TP4 (cheat sheet de presentación)

Cada decisión arquitectónica con: qué se hizo, alternativas consideradas, trade-off explícito y razón final. Pensado para tener abierto durante la presentación.

---

## 1. Con VPC — el core del chatbot corre dentro de la red privada

**Decisión:** la arquitectura tiene VPC propia (`10.0.0.0/16`). El core del chatbot corre como servicio Fargate en **subnets privadas** (`private-fargate`, `assign_public_ip=false`) detrás de un ALB; las Lambdas de negocio corren en subnets privadas (`private-lambda`). Esto responde directamente al feedback de Faustino del 17/06: el cómputo del chatbot ahora vive DENTRO de la VPC, no como Lambdas sueltas regionales.

**Topología (ver `networking.tf`):**
- 2 subnets públicas → ALB + NAT Gateway.
- 2 subnets `private-fargate` → Fargate (chat-handler + weather-poller).
- 2 subnets `private-lambda` → Lambdas de negocio (Saga payment/refund, workers).
- 1 IGW, 1 NAT Gateway, 2 gateway endpoints (S3, DynamoDB gratis), 8 interface endpoints (sns, sqs, secretsmanager, states, ecr.api, ecr.dkr, logs, kinesis-firehose).

**Alternativas:**
- (a) Mantener todo serverless sin VPC (diseño rechazado el 17/06) → Faustino lo marcó: el core del chatbot quedaba sin perímetro de red.
- (b) VPC con sólo una Lambda adentro (TP3) → marcado como inconsistente por Faustino.
- (c) **VPC con el core en contenedor + Lambdas de negocio adentro, endpoints para el tráfico AWS (elegida).** → El cómputo persistente (Fargate, awsvpc) necesita identidad de red; los VPC endpoints le dan acceso least-privilege a los servicios AWS sin salir a internet.

**Trade-off:** se paga 1 NAT Gateway (~USD/hora) y los interface endpoints; se agrega código Terraform (SGs, routing, endpoints). Se gana: perímetro de red real para el core, ECR/Logs alcanzables desde subnet privada, egress controlado por NAT, y el feedback de Faustino resuelto.

**Por qué es la decisión correcta:** Fargate con `network_mode=awsvpc` necesita ENIs en subnets — es cómputo con identidad de red persistente, exactamente el caso de uso de una VPC. El ALB internet-facing es el único punto expuesto; las tasks viven en subnets privadas sin IP pública. VPC Flow Logs se podrían sumar para visibilidad L3/L4.

---

## 2. Sin RDS — Data Lake S3 + Athena

**Decisión:** la capa de analytics es un data lake S3 (eventos en JSON Lines gzip) + 4 tablas Glue tipadas estáticas con partition projection + Athena, alimentado por Kinesis Firehose. El schema está declarado en Terraform (`analytics.tf`), no se descubre con un crawler.

**Alternativas:**
- (a) RDS PostgreSQL como TP3 → carga el OLTP con queries de OLAP; no escala por costo; mala práctica.
- (b) Redshift → over-engineered para el volumen.
- (c) Athena Federated Query a RDS → costo dual.
- (d) **S3 + Athena (elegida).** → Estándar de data lake 2026.

**Trade-off:** Athena tiene latencia de 1-5s por query (vs ms en RDS); no es real-time. Para business analytics (reportes diarios, semanales) esto es irrelevante.

**Costo:** Athena cobra ~5 USD por TB escaneado. Con el volumen del chatbot el costo es despreciable. S3 storage es ~0.023 USD/GB vs ~0.10 USD/GB de RDS.

**Frescura de datos:** no hay crawler. Las 4 tablas Glue son estáticas (declaradas en Terraform) y las particiones se proyectan en consulta (partition projection sobre `dt`/`hh`). Los datos quedan consultables apenas Firehose los escribe a S3 — buffer de ~60s — sin esperar una corrida de crawler ni un descubrimiento de schema.

**Ingesta (ver `firehose.tf`):** 4 Kinesis Firehose delivery streams (reservation_events, flight_events, claim_events, interaction_events), buffer 5 MB / 60 s, GZIP → `lake/<entidad>/dt=YYYY-MM-DD/hh=HH/*.gz`. Dos fuentes: CDC desde la tabla `business` (DynamoDB Stream → Lambda `business-analytics-emitter` → `PutRecord` al Firehose de la entidad) y eventos de comportamiento (suscripción SNS del topic central → Firehose `interaction_events`).

---

## 3. Sin bastion

**Decisión:** el bastion EC2 del TP3 se eliminó.

**Alternativas:**
- (a) Mantener bastion en subnet pública → señalado por Faustino.
- (b) Mover bastion a subnet privada + VPC Endpoints SSM → aunque ahora hay VPC, no hay RDS al que forwardear.
- (c) **Eliminar bastion (elegida).** → Sin RDS, no hay caso de uso, aunque la VPC ahora exista para el core en Fargate.

**Trade-off:** ninguno relevante. El bastion solo servía para que un DBA hiciera port-forwarding a RDS via SSM — sin RDS no hay nada que forwardear. La VPC actual existe para el cómputo en contenedor, no para alojar una base de datos.

**Si tuviéramos que dar acceso DBA en producción real:** Lambda one-shot con permisos limitados invocada por el DBA via AWS CLI.

---

## 4. Validación del JWT in-app en el contenedor (no Cognito Authorizer)

**Decisión:** el chatbot entra por **ALB** (no API Gateway), y el JWT de Cognito lo valida el propio contenedor Fargate. `server.py` verifica la firma RS256 contra el JWKS del User Pool, el `issuer` y el `exp`, y pasa los claims a la lógica. No existe Cognito Authorizer en ningún lado (verificado: no hay `aws_api_gateway_authorizer` ni `COGNITO_USER_POOLS` en la infra).

**Alternativas:**
- (a) Validación manual con `python-jose` dentro de la Lambda (TP3).
- (b) Cognito Authorizer en API Gateway (diseño serverless intermedio) → descartado: el core dejó de entrar por API Gateway y pasó a un ALB delante de Fargate (no soporta Cognito Authorizer; en producción real sería auth Cognito nativa del ALB sobre listener HTTPS).
- (c) **Validación in-app en el contenedor (elegida).** → El core ya es un servicio HTTP nativo (FastAPI); validar el JWT en el request handler es el patrón natural y no acopla la auth a API Gateway.

**Trade-off:** el código de validación vive en la app (hay que mantenerlo y testearlo) en lugar de delegarlo al perímetro. A cambio, la validación es independiente de API Gateway y el contenedor controla exactamente qué claims usa.

**Ganancias:**
- Sin token válido → `401` desde el propio servicio antes de tocar la lógica de negocio.
- El ALB hace health-check a `/health` (sin auth); las rutas `POST /api/chat`, `GET /api/reservations`, `POST /api/payment` exigen JWT válido.
- La auth no depende de API Gateway: si mañana se cambia el ALB por otro ingress, la validación viaja con el contenedor.

**Excepción documentada:** el API GW de `auth-callback` (único API Gateway que queda, `jetsmart-prod-auth-api`, bridge OAuth del Hosted UI) tiene `authorization = "NONE"` por el workaround Cognito (ver `teoria/notas-de-clase/workaround-cognito.md`).

---

## 5. Step Functions para la Saga (no SNS→SQS)

**Decisión:** el flujo de pago se orquesta con Step Functions y patrón Saga con compensaciones.

**Alternativas:**
- (a) SNS → SQS → Lambda encadenados → orquestación distribuida, código de tracking de estado disperso.
- (b) Lambda gigante con todos los pasos → no resiliente, sin reintentos por paso.
- (c) **Step Functions Saga (elegida).** → Orquestación con estado, compensaciones declarativas, reintentos automáticos.

**Trade-off:** Step Functions cuesta por transición de estado (~25 USD/millón). Para el volumen del TP es despreciable.

**Por qué es correcto:** los flujos de pago son transacciones distribuidas — necesitan rollback consistente si fallan a mitad de camino. Step Functions resuelve esto con `Catch` y estados de compensación en ASL.

---

## 6. DynamoDB para datos operacionales

**Decisión:** DynamoDB Single Table Design para sesiones, vuelos mock, reservas, reclamos.

**Alternativas:**
- (a) Aurora Serverless → menos managed, requiere VPC.
- (b) Postgres en RDS → menos elastic, requiere VPC.
- (c) **DynamoDB (elegida).** → On-demand billing, sin VPC, latencia <5ms.

**Trade-off:** DynamoDB es malo para queries complejas (joins, aggregates). Para los access patterns conocidos de la app es perfecto. Para analytics se complementa con S3+Athena.

**Por qué Single Table Design:** cada operación toca un solo PK, evita N+1 queries entre tablas, optimiza costo.

---

## 7. SNS → SQS → Lambda para analytics (no Lambda directa)

**Decisión:** el pipeline de analytics usa fan-out con SNS y buffer con SQS.

**Alternativas:**
- (a) SNS → Lambda directo → si Lambda falla, se pierde el mensaje.
- (b) chat-handler escribe directo a S3 → bloquea el path sincrónico del chat.
- (c) **SNS → SQS → Lambda (elegida).** → Desacople + buffer + reintentos + DLQ.

**Trade-off:** un nivel más de indirección (SQS). Ganancia: chat termina inmediatamente; SQS desacopla picos; DLQ retiene errores 14 días.

---

## 8. chat-handler en Fargate dentro de la VPC (subnets privadas)

**Decisión:** el core "chat-handler" dejó de ser Lambda y corre como **servicio FastAPI nativo en ECS Fargate**, en subnets privadas (`private-fargate`, `assign_public_ip=false`), detrás de un ALB internet-facing (HTTP:80). 2 tasks Multi-AZ (1 por AZ), Auto Scaling 2→6 con target tracking de CPU al 60%. El egress a la API de Anthropic sale por el NAT Gateway; ECR/Logs/Secrets/DynamoDB se alcanzan por VPC endpoints. Esto responde al feedback de Faustino: el cómputo del chatbot ahora vive en la VPC.

**Alternativa eliminada:** mantenerlo como Lambda regional sin VPC (diseño serverless rechazado el 17/06) → Faustino marcó que el core no tenía perímetro de red.

**Por qué Fargate en VPC:**
- Cómputo con identidad de red persistente (`network_mode=awsvpc`, ENIs en subnet) → caso de uso natural de una VPC.
- Servicio HTTP de larga vida con health-checks del ALB, sin cold start por request como Lambda.
- Auto Scaling por CPU para absorber picos (elasticidad real, 2→6 tasks).

**Seguridad:**
- El contenedor valida el JWT de Cognito in-app (decisión #4) y rechaza requests sin token válido.
- Tasks en subnet privada sin IP pública; solo el ALB está expuesto.
- Security groups: el SG de las tasks solo acepta tráfico del SG del ALB.
- LabRole limita las acciones AWS; API key Anthropic en Secrets Manager (no en código).

**Código:** `app/chat-handler/` → `chat_core.py` (tool-use loop de Anthropic, DynamoDB, PII) + `server.py` (router FastAPI, validación JWT). Imagen Docker en ECR, pulleada por Fargate. El nombre lógico "chat-handler" se mantiene; lo que cambió es el runtime (Lambda→Fargate) y el envoltorio (event Lambda→HTTP nativo).

---

## 9. Workaround Cognito (auth-callback Lambda)

**Decisión:** Lambda `auth-callback` detrás de API Gateway funciona como bridge HTTPS entre Cognito y el frontend S3 HTTP.

**Razón:** Cognito Hosted UI sólo redirige a URLs HTTPS. El frontend en S3 es HTTP (sin CloudFront en este TP). La Lambda es la única forma de tener un endpoint HTTPS estable que reciba el `code` y redirija al frontend con el token.

**Por qué `auth-callback` no tiene Cognito Authorizer:** Cognito redirige sin Authorization header — el `code` está en el query string. Activar el authorizer rompería el workaround.

**Documentado en:** `teoria/notas-de-clase/workaround-cognito.md`.

---

## 10. No QuickSight para visualización

**Decisión:** el equipo de business analytics usa cliente SQL externo (DBeaver / DataGrip) con Athena JDBC driver.

**Alternativa:** QuickSight + VPC connection a Athena.

**Razón concreta:** **QuickSight no está disponible con LabRole** en AWS Academy. Si se hubiera podido, sería la elección estándar (dashboards drag-and-drop, scheduled reports, embedding).

**Trade-off:** menos visual, más técnico (los analistas tienen que saber SQL). Aceptable para el escenario académico — el equipo de business analytics se asume entrenado en SQL.

---

## 11. No layer `python-jose`

**Decisión:** eliminamos el Lambda layer `python-jose`. Quedan solo dos layers (`anthropic` y `system-prompt`, ver `layers.tf`).

**Razón:** el `python-jose` era usado por la Lambda `chat-handler` del TP3 para validar el JWT manualmente. Al migrar el core a Fargate, la validación del JWT pasó a ser in-app en el contenedor (decisión #4) usando **PyJWT + cryptography** (ver `app/chat-handler/requirements.txt`), que se empaquetan en la imagen Docker, no en un layer. Ninguna Lambda restante valida JWT, así que el layer dejó de tener consumidor.

**Beneficio colateral:** un layer menos que construir y versionar; menos superficie en el empaquetado de las Lambdas.

---

## 13. Dos tablas DynamoDB en TP4 (bounded contexts)

**Decisión:** la única tabla del TP3 se partió en dos single-design: `jetsmart-prod-conversations` (estado del chatbot) + `jetsmart-prod-business` (PSS-like).

**Alternativas:**
- (a) Mantener single-table con todo (TP3) → mezcla conceptos, dificulta retention y reemplazo del canal.
- (b) Una tabla por entidad (USERS, FLIGHTS, RESERVATIONS, ...) → rompe single-table design, multiplica RCU/WCU.
- (c) **Dos tablas, una por bounded context (elegida)** → bounded contexts del DDD, manteniendo single-table dentro de cada uno.

**Trade-off:** dos conexiones de cliente DynamoDB en el servicio chat-handler (`chat_core.py`), una operación más en `terraform apply`, PITR en ambas tablas. Ganancia: separación clara de responsabilidades, failure isolation, retention policies independientes, reemplazabilidad del canal sin tocar el negocio.

**Por qué es la decisión correcta:** el chatbot y el negocio JetSmart son dominios distintos. Las conversaciones son efímeras (TTL días), las reservas son persistentes (años). Las conversaciones son propiedad del canal, las reservas son propiedad de la aerolínea — si mañana sumás un canal web/mobile/IVR, comparten la business table pero cada uno tiene su propio conversation store.

---

## 14. PNR-céntrico (estilo PSS real) en lugar de USER#/RESERVATION#

**Decisión:** la reserva canónica vive en `PNR#{pnr}/#METADATA` con sub-items SEGMENT#, PAX#, BP#. `USER#{user_id}/RESERVATION#{pnr}` es solo un thin pointer denormalizado.

**Alternativas:**
- (a) Mantener `USER#/RESERVATION#{id}` como en TP3 → no escala a múltiples segments/pax.
- (b) **PNR-céntrico (elegida)** → modelo estándar de la industria (Navitaire, Amadeus, Sabre).

**Trade-off:** doble escritura al crear la reserva (canonical + pointer). Aceptable porque la lectura "mis reservas" es la más frecuente y queda O(1) Query del pointer.

**Por qué es correcto:** habilita queries del PSS real:
- "Quién está en el vuelo X del día Y" → Query GSI2 ReservationsByFlight (clave para notificaciones proactivas).
- "Encontrar PNR de Juan Pérez" → en TP3 era Query GSI ReservationsByPassenger; en TP4 final el GSI fue eliminado porque el canal de call center no se implementó. Si llegara a hacerse, se reintroduce el GSI o se resuelve con Scan + filter para volúmenes bajos.
- Pasajero CRM separado (`PASSENGER#{dni}`) con back-refs históricos.

---

## 15. Derivación a humano vía SQS (no llamada directa al call center)

**Decisión:** la tool `escalate_to_human` del chatbot publica a SQS `human-handoff`; la Lambda `human_handoff_processor` consume y simula el POST al call center.

**Alternativas:**
- (a) el chat-handler llama directo a la API del call center → acopla disponibilidad y latencia.
- (b) **SQS intermediario (elegida)** → desacople + reintentos + DLQ.

**Trade-off:** un componente más en el path (SQS). Ganancia: si el call center está caído, el pedido queda esperando 14 días en la cola; reintentos automáticos con DLQ para alarma; trazabilidad de todos los handoffs en conversations table.

**Por qué SQS y no SNS:** un pedido de handoff tiene un único consumer lógico (el sistema del call center). SNS sería over-engineering. Si en el futuro queremos fan-out (analytics + call center + Slack del equipo de soporte), agregamos un SNS por delante; hoy no hay necesidad.

---

## 16. Notificaciones proactivas vs polling

**Decisión:** las cancelaciones de vuelo se notifican proactivamente vía SNS central `events` (`event_type=flight_cancelled`) → Lambda `proactive_notifications` (suscripta directo, SNS→Lambda con filter policy) → Query GSI `ReservationsByFlight` → emails de los afectados vía SNS `notifications`.

**Alternativas:**
- (a) El usuario consulta periódicamente (polling) → mala UX, carga la tabla.
- (b) Una Lambda cron que escanee la tabla buscando vuelos cancelados → carga el GSI innecesariamente y agrega latencia (los pasajeros se enteran cuando corre la cron, no cuando se canceló).
- (c) **Event-driven push (elegida)** → el módulo de ops de la aerolínea publica un evento, los suscriptores se enteran al instante.

**Por qué GSI `ReservationsByFlight`:** sin él, encontrar los pasajeros afectados requiere Scan de toda la business table — O(n) lineal. Con el GSI, una sola Query devuelve la lista — O(log n). Es **el habilitador técnico** del feature.

**Trigger en TP4:** el flujo se dispara automáticamente cuando ops (o el weather-poller) cambia `estado_vuelo=CANCELADO` en el master row del vuelo (consola DynamoDB o dashboard interno). Un DynamoDB Stream sobre la tabla `business` propaga el cambio a la Lambda `stream-emitter`, que detecta la transición de estado y publica `event_type=flight_cancelled` al SNS central `events`. Ver justificación #28.

---

## 17. Boarding pass async vía SQS

**Decisión:** el Saga ya no invoca la Lambda de boarding pass directamente — el estado terminal de éxito publica `booking_confirmed` al SNS central `events`, y la Lambda `boarding_pass_async` (suscripta directo al topic, filter policy `event_type=booking_confirmed`) genera el BP de forma asíncrona. El mismo evento hace fan-out también a la Lambda `notification`.

**Alternativas:**
- (a) Mantener sync en el Saga (TP3, Parallel interno) → un error en BP frena la confirmación post-pago.
- (b) `.waitForTaskToken` pattern → el Saga espera al BP. Más complejo en ASL.
- (c) **Fire-and-forget vía SNS→Lambda directo (elegida)** → simple, desacopla, y reusa el backbone de eventos: el Saga solo publica el hecho, los consumidores se enganchan por filter policy sin que el Saga los conozca.

**Trade-off:** el BP no está inmediatamente disponible. El usuario consulta y, si todavía no se generó, recibe "tu boarding pass se está generando, intentá en unos segundos". En la práctica el BP está listo en <2 segundos.

**Por qué SNS→Lambda directo y no una SQS amortiguadora:** la generación del BP es un downstream elástico (Lambda escala sola) y el resultado es re-derivable desde DynamoDB — si la Lambda fallara, el dato de la reserva sigue ahí para regenerar. No se justifica una cola intermedia ni una DLQ por agregar; la durabilidad/visibilidad la dan el retry de SNS + una **alarma de Lambda Errors** (ver `cloudwatch.tf`). La única SQS funcional del sistema es `human-handoff`, donde el downstream (call center mock) SÍ es no elástico.

**Por qué es correcto:** la reserva confirmada es lo crítico — no debe esperar al BP. El Saga publica `booking_confirmed` y termina; el BP se genera fuera del path crítico. Demuestra el patrón de **publicar el hecho al backbone y dejar que el fan-out por filter policy desacople a los consumidores** desde Step Functions.

---

## 18. Auditoría con CloudTrail — fuera de alcance de esta entrega

**Scoped out:** la gobernanza/auditoría a nivel cuenta (trail multi-region de CloudTrail) quedó fuera de alcance de esta entrega — se priorizó el core funcional. No está desplegada. En producción real iría un trail multi-region con management events hacia un bucket S3 dedicado, o un SIEM.

---

## 19. Email del JWT, no preguntárselo al usuario en el chat

**Decisión:** la tool `create_reservation` ignora el campo `email_contacto` del input y usa el claim `email` del JWT de Cognito. El system prompt instruye explícitamente *"NUNCA preguntar el email al usuario"*.

**Razón:** el usuario ya se autenticó vía Cognito Hosted UI — su email está validado por el IdP, llega firmado en el JWT y el contenedor lo extrae del claim `email` tras validar el token in-app (`server.py`). Preguntárselo de nuevo en el chat es:
1. **Mala UX** — el usuario lo escribió hace 10 segundos en el login.
2. **Riesgo de tipos** — un email mal tipeado en el chat dispara mails fallidos sin que el sistema se entere.
3. **Inconsistente con el design de validación in-app del JWT** (decisión #4): si confiamos en el JWT para autenticación, también confiamos en sus claims para identidad.

**Trade-off:** el `email_contacto` queda como opcional en el schema de la tool — útil sólo si el usuario quiere especificar un mail distinto al del login (caso edge, no documentado en el flujo). El handler hace `user_email or inputs.email_contacto` por compatibilidad.

---

## 20. Boarding pass entregado por mail (mock), no como link en el chat

**Decisión:** `get_boarding_pass` retorna los datos del BP + un flag `enviado_por_mail`. El system prompt prohíbe explícitamente mostrar links/URLs del BP. El bucket S3 con el archivo del BP sigue existiendo (lo escribe `boarding_pass_async`) pero la URL presigned no se expone al chat.

**Alternativas:**
- (a) Link presigned URL en el chat (estado anterior) → expone un link largo, feo, con credenciales temporales que se vencen.
- (b) SES con el PDF adjunto (producción real) → requiere domain validation en SES, fuera del scope del TP.
- (c) **Mock por mail (elegida)** → el chat dice "te lo enviamos por mail"; en realidad no se envía nada de SES, pero el bucket S3 con el archivo queda como evidencia para auditoría.

**Trade-off explícito:** el "envío por mail" es un mock — el archivo está en S3 pero no se envía físicamente. Es deuda técnica honesta: el flujo de UX queda correcto y migrar a SES más adelante es cuestión de configurar el sender en `boarding_pass_async`.

**Por qué es correcto:** el chat es para conversar, no para distribuir archivos. Los archivos se distribuyen por canales asíncronos (mail, app, portal). El estado anterior mezclaba ambas responsabilidades y exponía detalles de infraestructura (`s3.amazonaws.com/...?AWSAccessKeyId=...`) que no le importan al pasajero.

---

## 21. SNS topic global de notificaciones, no destino por usuario

**Decisión:** el topic `jetsmart-prod-notifications` hace fan-out broadcast a todos los suscriptos (configurados en `var.notification_email_subscribers`). Para el TP, el único suscripto es el email del demo (`gmoscacofino@itba.edu.ar`), confirmado manualmente vía el link de "Confirm subscription".

**Alternativas:**
- (a) SES con destino dinámico por usuario (producción real) → requiere domain validation y sandbox approval; no factible en AWS Academy.
- (b) Una subscription email por usuario, filtrada por `MessageAttributes` y `SubscriptionFilterPolicy` → costosa en gestión (crear subscriptions en cada signup) y el confirm subscription rompe la UX.
- (c) **SNS broadcast con un único endpoint para el demo (elegida)** → simple, suficiente para mostrar el patrón Saga → notificación.

**Trade-off honesto:** con más de un usuario activo todos reciben los mails de todos. **Esto es un anti-patrón para producción y hay que decirlo en la presentación**. En la realidad este punto se resuelve con SES (`SendEmail` con destinatario dinámico) — el patrón Saga publica un evento, una Lambda específica formatea el mail y lo manda al destinatario correcto vía SES.

**Por qué se quedó así:** el TP4 evalúa elasticidad (SQS/SNS presentes), no la solución de email transaccional. Migrar a SES era scope creep y AWS Academy no tiene SES totalmente habilitado sin domain validation.

**Pregunta esperable en oral:** *"¿Y si un atacante se suscribe al topic?"* → buena pregunta. La bucket policy del topic (faltante en el TP) debería restringir `sns:Subscribe` al LabRole. En producción real esta sería una vulnerabilidad seria — para el TP el topic no es público y nadie puede suscribirse desde fuera de la cuenta.

---

## 22. Seat map real con ítems SEAT# individuales (no counter)

**Decisión:** cada vuelo tiene 120 ítems `FLIGHT#<o>#<d>/DATE#<f>#FLIGHT#<v>#SEAT#<row><letter>` con un atributo `reserved_by` ausente cuando el asiento está libre. La reserva atómica usa `UpdateItem` con `ConditionExpression: attribute_exists(PK) AND attribute_not_exists(reserved_by)`. La liberación (compensación de la Saga) usa `ConditionExpression: reserved_by = :owned_pnr` para no liberar asientos de otros PNRs.

**Alternativas:**
- (a) Counter `asientos_disponibles` (TP3) → previene oversell global pero no double-assignment del mismo número de asiento. El boarding pass decía "ALEATORIO" porque no había seat real.
- (b) Set-attribute en el FLIGHT item con lista de seats ocupados → DynamoDB no permite operaciones atómicas sobre elementos de set, expone la lista entera en cada read (>=400 KB con vuelo lleno).
- (c) **Items individuales con ConditionExpression (elegida)** → atomicidad real, prevención de double-assignment, queryable con `Select=COUNT` y `begins_with(SK, "...#SEAT#")`.

**Decisión de atomicidad:** el PNR se genera en `app/chat-handler/chat_core.py` (servicio Fargate) con SHA-256 del payment_id ANTES de iniciar el Saga y se pasa en el input del state machine. ReserveFlight escribe `reserved_by = "PNR#XXXXXX"` desde el primer paso. Esto permite que la compensación `ReleaseFlight` libere exactamente el seat de este PNR (sin riesgo de tocar uno ajeno) y que el flujo sea idempotente bajo retry.

**Trade-off:** el volumen de ítems se multiplica por ~121× (20 rutas × 30 fechas × 121 ítems = ~72k). En DynamoDB es despreciable. El seed tarda ~3 min en lugar de ~10 seg.

**Edge cases manejados:**
- Seat `99Z` que no existe → `ConditionalCheckFailedException` → `get_item` para distinguir vs "ya reservado" → `ValueError` específico → Saga rollea.
- Seat ya tomado → falla atómica → Saga rollea.
- Asignación random sin seat_id → helper `_claim_random_seat` con retry hasta 3 veces (race condition).
- Saga muere entre ReserveFlight y ReserveBooking → CancelBooking es no-op (no hay PNR) → ReleaseFlight libera por `_seat_sk` guardado en `flight_info`.

**Pregunta esperable en oral:** *"¿Por qué no usaste DynamoDB Transactions?"* → para reservar 1 seat alcanza con UpdateItem condicional. Transactions tienen sentido si tuviéramos que reservar N seats simultáneos atómicamente (grupos de N pasajeros con M asientos contiguos), pero para 1 PAX = 1 seat es overkill — Transactions cuestan 2× el throughput y tienen latencia mayor.

---

## 23. Pricing server-side con multiplicadores, no monto fijo

**Decisión:** las tarifas (BASIC/LIGHT/SMART/FULL FLEX) son multiplicadores sobre el precio base del vuelo (×1.00, ×1.10, ×1.25, ×1.50). Los extras (mascota, asiento, equipaje, etc.) son monto fijo en USD por reserva. El cálculo lo hace `lambda/pricing.py:compute_total` server-side; el LLM (Claude) NO calcula el total y ni siquiera lo pasa como input — el campo `total` fue removido del schema de `create_reservation`.

**Por qué multiplicadores y no monto fijo en tarifas:** el costo marginal de servicio premium (cabin crew, refunds, asiento elegido) escala con el costo del vuelo. Antes (`base+$15` para LIGHT) un vuelo de $50 se convertía en LIGHT de $65 (×1.30); uno de $300 en LIGHT de $315 (×1.05). El premium se diluía. Con multiplicadores la proporción es estable.

**Por qué monto fijo en extras:** un sandwich, un kilo de bodega, una jaula de mascota tienen costo operativo fijo. No escalan con el precio del ticket.

**Persistencia de extras:** cada extra contratado se persiste como ítem `PNR#<pnr>/EXTRA#<nn>` con `extra_type`, `amount`, `created_at`. Habilita auditoría por PNR ("¿qué llevó cada pasajero?") y queries del estilo "cuántas mascotas viajaron este mes".

**Validación:**
- En el chat: `pricing.validate_inputs(tarifa, extras)` rechaza tarifa o extra alucinados antes de iniciar el Saga.
- En el Saga: `pricing.compute_total` recalcula el total real desde el precio del inventory + tarifa + extras, ignorando cualquier `total` que pudiera haber venido del cliente. **Imposible underpricing por inyección o alucinación.**

**Testeable sin AWS:** `lambda/tests/test_pricing.py` con 13 tests unitarios cubre tarifas, extras incluidos en tarifa superior, redondeo, validación de inputs.

**Pregunta esperable en oral:** *"¿Y si Claude pasa total=1 igual?"* → el server lo ignora; reserve_booking llama a compute_total y usa ese valor. El input total ni siquiera existe en el JSON schema de la tool — Anthropic SDK lo rechazaría antes de invocar.

---

## 24. Vocabulario unificado a español en datos del thin pointer

**Decisión:** el thin pointer `USER#<sub>/RESERVATION#<pnr>` ahora se escribe con keys en español: `origen`, `destino`, `vuelo_numero`, `fecha`, `pasajeros`, `nombre_pasajero`, `telefono`. Antes mezclaba inglés (`origin`, `destination`, `flight_number`, `flight_date`, `passenger_count`, `passenger_name`, `phone`) con español en el resto del sistema. Frontend `chat.js` y endpoint `/api/reservations` también consumen español.

**Por qué se unifica:** el LLM, el system prompt, los logs y el seed ya estaban en español. La inconsistencia era de un solo módulo (`payment_processor.py`) y se propagaba al frontend.

**Qué quedó en inglés (y por qué):**
- `email`, `created_at`, `status`, `total`, `tarifa`, `pnr` → son neutros o estándares ISO/industria. Cambiarlos sería ruido.
- PASSENGER#/#PROFILE (CRM) y SEGMENT# atributos internos → no los lee el chat, no cambian la UX. Reducir blast radius del rename.

**Trade-off:** no hay backwards compat para reservas viejas — el destroy+reseed del lab cubre la migración. En producción real haría falta una migración o un dual-read transitorio.

---

## 25. Sacar atributo `aerolinea`

**Decisión:** se eliminó la columna `aerolinea: "JetSmart"` del seed y de las lecturas en el chat-handler (`chat_core.py`). Era un valor constante en toda la tabla.

**Razón:** YAGNI. La tabla `business` ya está namespaced por `name_prefix = jetsmart-prod-` y el sistema es mono-aerolínea. Tener un campo que siempre dice "JetSmart" sólo añade ruido en la consola y en los logs.

**Si en el futuro fuera multi-tenant:** se reintroduce con un GSI por aerolínea. No retornará por defecto, sería una decisión consciente vinculada a un cambio de scope.

---

## 26. Soft-hold de asiento con TTL de 10 minutos

**Decisión:** cuando el usuario elige un asiento específico en el chat, Claude llama `hold_seat` que lockea el ítem `SEAT#<row><letter>` con `held_by = "USER#<sub>"` y `hold_expires_at = now + 600`. El asiento queda bloqueado para otros users hasta que (a) el user confirme la reserva (el hold se convierte en `reserved_by`), (b) el user libere o cambie a otro seat (auto-release), o (c) expire el TTL.

**Alternativas consideradas:**
- (a) Optimistic — refrescar lista al fallar → simple pero el race window queda abierto los minutos que tarda el user en PASOS 4-5-6 (mascota, asiento, datos). Vio en pruebas con dos tabs simultáneos: pisada constante.
- (b) Re-validar con `check_seat_available` antes de confirmar → reduce race a milisegundos pero igual existe ventana entre check y reserve_flight.
- (c) Auto-asignar uno cercano cuando el preferido falla → cómodo, pero el user pierde control sobre la elección.
- (d) **Soft-hold con TTL (elegida)** → patrón estándar de la industria (Expedia, Booking.com, Decolar). Garantiza que el seat queda reservado durante el flujo de checkout sin requerir WebSocket en tiempo real.
- (e) WebSocket + DDB Streams para updates live → UX premium pero requiere API GW WebSocket + Lambda broadcaster + connection registry. Días de trabajo. Fuera de scope.

**Implementación atómica:**
- `hold_seat` hace `UpdateItem` con ConditionExpression compuesta que acepta seats libres O con `held_by` propio (renueva) O con `hold_expires_at` ya vencido. Rechaza seats con `reserved_by` o `held_by` ajeno vigente.
- En el mismo handler, después del lock, se liberan holds previos del mismo user en el MISMO vuelo (best-effort, no atomic). Si el race genera un hold huérfano, sólo bloquea 10 min hasta TTL.
- `reserve_flight_handler` (Saga) usa la misma ConditionExpression compuesta: el hold se "consume" en la operación que pone `reserved_by`, eliminando `held_by` y `hold_expires_at` en el mismo UpdateItem. No hay ventana entre liberar y reservar.
- `_claim_random_seat` (asignación aleatoria) filtra holds ajenos vigentes del pool de candidatos.

**Aviso continuo del estado al usuario:**
- En cada turn del chat entre PASO 3 (hold) y PASO 6c (confirmar), Claude llama `check_hold_status` que retorna `still_held` / `expired_seat_free` / `expired_seat_taken`. Según el resultado, Claude notifica al user proactivamente sin esperar al intento de confirmación.

**Countdown visual en el frontend:**
- El backend devuelve `metadata.hold = {seat_id, expires_at_epoch, vuelo_numero, fecha}` en el response del chat cuando `hold_seat` tiene éxito.
- El módulo `HoldBanner` en `frontend/js/chat.js` arranca un `setInterval` que actualiza el banner cada segundo con formato `MM:SS`.
- Cuando faltan ≤60 segundos, el banner cambia a estilo rojo con pulse animation.
- Al llegar a 0, el banner muestra "0:00" y aparece un mensaje en el chat invitando al user a verificar el estado.
- El backend también emite `metadata.hold_cleared = true` cuando el user libera, confirma reserva o el check_hold_status detecta cambios — el frontend cierra el banner.

**Trade-off honesto:**
- Si el user abandona la conversación, el seat queda bloqueado 10 min sin beneficio para nadie. Aceptable — el costo de un seat bloqueado por 10 min es mucho menor que la fricción de no tener hold.
- Si el user tarda >10 min completando PASOS 4-5-6, el hold expira. El backend lo detecta y le ofrece retomar. Si en ese intervalo otro usuario tomó el mismo seat, le mostramos alternativas.
- No usamos `TransactWriteItems` para liberar previos + tomar nuevo. Atomic global agregaría costo 2× WCU y complejidad. El hold huérfano por race es bounded por TTL.

**Pregunta esperable en oral:** *"¿Y si el user holdea desde un tab e intenta confirmar desde otro?"* → el `user_id` viene del JWT de Cognito validado in-app en el contenedor, es el mismo en ambos tabs. El handler reconoce el hold como propio y lo consume. **Funciona transparente.**

---

## 27. Protección de PII frente a la API de Anthropic

**Decisión:** tokenización en línea de PII del usuario antes de enviar mensajes a `api.anthropic.com`. El módulo de tokenización de PII (copia en `app/chat-handler/`, ejecutado por el servicio Fargate del chat-handler) detecta email, DNI, teléfono, fecha de nacimiento (ISO y DD/MM/YYYY) y sexo en el texto que el user escribe en el chat, y los reemplaza por placeholders del tipo `<EMAIL_xxxxxxxxxx>`, `<DNI_xxxxxxxxxx>`, etc. Claude ve solo los placeholders. Antes de invocar los handlers de tools, los placeholders se resuelven a los valores reales (look-up en `conversations` table).

**Por qué importa:**
- Cada llamada a `messages.create()` envía el system prompt, el array `messages` (historial + nuevo mensaje) y los `tools` schemas a infraestructura de Anthropic. Los servidores de Anthropic procesan transitivamente todo lo que el user escribió.
- Aunque el contrato del plan API estipula que Anthropic no entrena modelos con esos datos, los logs internos podrían ser comprometidos.
- Tokenizar reduce drásticamente la PII expuesta sin cambiar la UX del chat.

**Implementación:**
- Tokens determinísticos por sesión vía HMAC-SHA256 (`HMAC(kind|value|session_id, secret)[:10]`). Mismo dato en la misma sesión → mismo token (Claude puede razonar sobre identidad sin ver el valor).
- Mappings guardados en `conversations` table: `PK = SESSION#<sid>`, `SK = TOKEN#<token>` con TTL 24h.
- `tokenize_text` corre sobre el `content` de cada mensaje user antes del `messages.create()`. El system prompt no se tokeniza (no contiene PII, es info de negocio).
- `detokenize_inputs` recorre recursivamente los `input` de tool_use que devuelve Claude y reemplaza placeholders por valores reales antes de invocar el handler.
- El system prompt instruye a Claude a NUNCA reformular los placeholders — debe pasarlos exactamente como llegan.

**Alternativas evaluadas:**
- (a) Masking parcial (`ju***@gmail.com`) — más simple pero no reversible y rompe la capacidad de Claude de mostrarle al user su propio dato.
- (b) AWS Comprehend — más cobertura de PII no estructurada pero +200ms latencia y costo adicional. Difícil con DNI argentino (lo confunde con número genérico).
- (c) Migrar a AWS Bedrock — elimina la fuga total porque el modelo corre dentro de AWS. Pero Haiku 4.5 no está aún en Bedrock. Queda como roadmap.

**Trade-off honesto:**
- ❌ No tokenizamos NOMBRES (regex confiable para nombres en español es difícil y los falsos positivos romperían el chat). Los nombres pasan en cleartext.
- ❌ No tokenizamos los `tool_results` (Claude necesita razonar sobre datos del vuelo; lo dejamos como evolución).
- ✅ Tokenizamos los inputs PII más estructurados que sí tienen regex confiable.
- ✅ Si Anthropic compromete sus logs, lo único directamente identificable serían nombres y partes del flujo conversacional, no DNIs ni emails ni fechas de nacimiento.

**Validación server-side asociada:** como Claude solo ve tokens, no puede validar formato de DNI/teléfono/fecha. La validación es responsabilidad del server: `_validate_passenger_input` en `app/chat-handler/chat_core.py` (servicio Fargate) valida los valores reales después de detokenizar y rechaza con error explícito si el DNI no tiene 7-8 dígitos, la fecha no es válida, el sexo no está en el enum, etc.

**Persistencia de fecha_nacimiento + sexo:** los pasos 5c y 5d del flujo de compra recolectan estos datos como exige cualquier PSS real (regulación TSA, identificación en boarding pass). Ahora se persisten en el item `PNR#<pnr>/PAX#01` junto con DNI, nombre, email, teléfono y seat. Antes se pedían pero no se guardaban — bug de coherencia que se cierra.

**Pregunta esperable en oral:** *"¿Por qué no tokenizan también los tool_results?"* → porque Claude necesita razonar sobre los datos del vuelo (origen, destino, fecha, precio, asiento) para conversar. Esos no son PII directa. Tokenizar las PII reales que aparecen en algunos tool_results (email del user, nombre en list_saved_passengers) es el paso siguiente del roadmap. Lo dejamos parcial honesto: mitigamos el vector más grande (texto libre del user) y reconocemos el residual.

---

## 28. Proactive notifications event-driven (DynamoDB Stream → Lambda emisor)

**Decisión:** habilitamos un Stream en la tabla `business` (`NEW_AND_OLD_IMAGES`) y la Lambda `stream-emitter` consume el stream con `filter_criteria` server-side. Cuando detecta un master row `FLIGHT#` con transición de estado a `CANCELADO`, publica `event_type=flight_cancelled` al SNS central `events`. El downstream lo resuelven las suscripciones directas del backbone (SNS→Lambda con filter policy): `proactive_notifications` (Query GSI `ReservationsByFlight` → emails vía SNS `notifications`) y `refund_trigger` (arranca la Refund Saga). No hay SNS `flight_events` ni SQS `proactive_notifications`: hay un único topic central `events` y el fan-out es por filter policy.

**Antes (TP4 inicial):** un script local `scripts/cancel_flight.py` hacía el `UpdateItem` y publicaba al SNS. Trigger manual, fuera del sistema. **Eliminado en TP4 final** — el único trigger ahora es el Stream.

**Alternativas evaluadas:**
- (a) Script manual (TP3 → mitad de TP4) → no escala, requiere operador con creds, no se integra con un dashboard de ops real.
- (b) **DynamoDB Stream → Lambda `stream-emitter` → SNS central `events` (elegida)** → cambia solo el origen del trigger; la Lambda traduce el cambio de estado a un evento de dominio y lo publica al backbone, donde los consumidores ya se enganchan por filter policy. Latencia <1 seg.
- (c) DynamoDB Stream → EventBridge Pipes → SNS (sin Lambda intermedia) → más declarativo, cero código de glue, pero los filter patterns de Pipes no permiten comparar `OldImage` vs `NewImage` para detectar transiciones. Habría falsos positivos cuando el ítem ya estaba CANCELADO y se modifica otro atributo. Además Pipes en LabRole no está garantizado disponible.
- (d) Stream → Lambda que invoca `proactive_notifications` directo (saltar el SNS central) → un componente menos pero perdés el fan-out: el mismo `flight_cancelled` también dispara `refund_trigger`. Publicar al backbone desacopla y deja sumar consumidores sin tocar el emisor.

**Implementación del `stream-emitter`:**
- `filter_criteria` en el event_source_mapping: solo invoca cuando `eventName=MODIFY` y `NewImage.estado_vuelo.S=CANCELADO`. Evita procesamiento de cambios en ítems PNR#, SEAT#, etc.
- Guard adicional en el handler: confirma que es master row `FLIGHT#` (no SEAT#, no PNR# que pudo matchear el filtro por coincidencia de atributo).
- Detección de transición real: `OldImage.estado_vuelo != "CANCELADO"`. Si el ítem ya estaba cancelado y solo se actualizó otro campo (ej. `cancellation_reason`), no se re-publica.
- Publica al SNS central `events` con `event_type=flight_cancelled` como MessageAttribute (`vuelo_numero`, `fecha`, `reason` en el body) — exactamente lo que `proactive_notifications.py` y `refund_trigger` ya esperan vía sus filter policies. Cero cambios downstream.

**Trade-offs:**
- ✅ Event-driven real: ahora ops cambia el estado desde cualquier interfaz (consola DynamoDB, otra Lambda, futuro dashboard) y el flujo se dispara solo.
- ✅ Latencia <1 seg desde el `UpdateItem` hasta la publicación al SNS.
- ✅ El script `cancel_flight.py` se eliminó: el Stream es ahora el único path. Para testear se hace `UpdateItem` directo desde la consola DynamoDB o CLI — más simple que mantener un script paralelo.
- ❌ Costo de Stream: ~$0.02 por 100k read requests. Despreciable en sandbox.
- ❌ Una Lambda más que mantener (el `stream-emitter`).
- ❌ El Stream emite TODOS los cambios de la tabla. El `filter_criteria` reduce las invocaciones a las que realmente importan, pero hay costo de read del Stream igualmente.

**Pregunta esperable en oral:** *"¿Por qué Stream + Lambda y no EventBridge Pipes?"* → Pipes no permite comparar OldImage vs NewImage. Habría una invocación por cada modificación de un ítem que ya tenía estado_vuelo=CANCELADO (ej. actualización de `cancellation_reason`). Con Lambda intermedia hacemos esa comparación en código y el patrón es 100% compatible con LabRole.

---

## 12. Frontend HTTP (no HTTPS)

**Decisión:** el frontend S3 sirve HTTP estático sin CloudFront.

**Alternativa:** S3 + CloudFront + ACM certificate + Route 53.

**Razón concreta:** AWS Academy limita Route 53 (no permite registrar dominios). Sin dominio propio, conseguir un cert válido es engorroso. Para el TP, HTTP es aceptable — los tokens viajan vía Cognito HTTPS y los API calls también son HTTPS.

**En producción real:** CloudFront delante con certificado ACM y dominio propio.

---

## Preguntas probables y respuestas cortas

> **¿Cómo respondiste al feedback de Faustino del 17/06?**

Faustino marcó que el core del chatbot quedaba como Lambdas sueltas fuera de la VPC, sin perímetro de red. La respuesta fue mover el core a un servicio en contenedor (Fargate) DENTRO de la VPC, en subnets privadas detrás de un ALB, con las Lambdas de negocio también en subnets privadas. Ahora el cómputo del chatbot vive en la VPC, que es exactamente lo que pedía. El bastion y RDS siguen sin existir porque no hay caso de uso (la capa de analytics es data lake S3 + Athena).

> **¿Por qué Fargate en VPC y no seguir 100% serverless?**

Porque un servicio HTTP de larga vida con auth in-app, health-checks y Auto Scaling es el patrón natural para el core conversacional, y necesita identidad de red (awsvpc) — la VPC deja de ser burocracia y pasa a ser el perímetro real del cómputo. Los servicios managed (DynamoDB, SNS, SQS, Step Functions, Firehose) se alcanzan por VPC endpoints sin salir a internet; el egress a Anthropic sale por NAT. No es "menos serverless por moda": es poner el cómputo donde corresponde según su forma.

> **¿Cómo escalan los analytics a 10x el volumen?**

S3 escala infinito. Athena escala automáticamente (es serverless). No hay crawler que pueda quedar atrás: el schema es estático y las particiones se proyectan en consulta, así que más volumen no agrega latencia de descubrimiento. El ingest al data lake lo hace `business_analytics_emitter` consumiendo el DynamoDB Stream con batching y haciendo PutRecord a Firehose, que batchea nativo a S3 — escala con el throughput del Stream sin cuello de botella.

> **¿Qué hacen los `payment-*` Lambdas si una de ellas falla con error transitorio?**

Step Functions reintenta automáticamente con backoff exponencial (configurado en cada estado). Si después de N reintentos sigue fallando, el `Catch` dispara las compensaciones del Saga.

> **¿Cómo sabe el equipo de analytics qué columnas tiene cada tabla?**

El schema está declarado en Terraform (`analytics.tf`): hay 4 tablas tipadas en el Glue Data Catalog (`reservation_events`, `flight_events`, `claim_events`, `interaction_events`), cada una con sus columnas y tipos fijos. El equipo abre DBeaver y las ve directamente — no hay descubrimiento, el catálogo ya está poblado por el `apply`. Trade-off honesto vs un crawler: si llega un `event_type` con campos nuevos, no aparecen solos — hay que agregar la columna en Terraform y re-aplicar. Es menos mágico que un crawler con `update_behavior=UPDATE_IN_DATABASE`, pero a cambio el schema es explícito, versionado en git y revisable en el PR, sin riesgo de que un crawler infiera mal un tipo o renombre una columna en producción.
