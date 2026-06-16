# Justificaciones — TP4 (cheat sheet de presentación)

Cada decisión arquitectónica con: qué se hizo, alternativas consideradas, trade-off explícito y razón final. Pensado para tener abierto durante la presentación.

---

## 1. Sin VPC

**Decisión:** la arquitectura no tiene VPC propia.

**Alternativas:**
- (a) Mantener VPC con todas las Lambdas adentro + VPC Endpoints. → Over-engineering sin recursos persistentes.
- (b) VPC con sólo `analytics-processor` adentro (como TP3). → Marcado como inconsistente por Faustino.
- (c) **Sin VPC (elegida).** → Coherente con la realidad: no hay recursos para aislar.

**Trade-off:** se pierde VPC Flow Logs (visibilidad L3/L4 de red). Se gana: cold start mínimo, costo cero de NAT/endpoints, menos código Terraform, sin SGs.

**Por qué es la decisión correcta:** las VPCs sirven para aislar recursos con identidad de red persistente (EC2, RDS, ElastiCache). En una arquitectura 100% Lambda + servicios managed regionales no hay nada para aislar. CloudTrail cubre la auditoría que en otro escenario haría falta de Flow Logs.

---

## 2. Sin RDS — Data Lake S3 + Athena

**Decisión:** la capa de analytics es S3 (eventos crudos en JSON Lines) + Glue Crawler + Athena.

**Alternativas:**
- (a) RDS PostgreSQL como TP3 → carga el OLTP con queries de OLAP; no escala por costo; mala práctica.
- (b) Redshift → over-engineered para el volumen.
- (c) Athena Federated Query a RDS → costo dual.
- (d) **S3 + Athena (elegida).** → Estándar de data lake 2026.

**Trade-off:** Athena tiene latencia de 1-5s por query (vs ms en RDS); no es real-time. Para business analytics (reportes diarios, semanales) esto es irrelevante.

**Costo:** Athena cobra ~5 USD por TB escaneado. Con el volumen del chatbot el costo es despreciable. S3 storage es ~0.023 USD/GB vs ~0.10 USD/GB de RDS.

**Frescura de datos:** Glue Crawler corre cada hora. Para demo se invoca manualmente con `aws glue start-crawler`.

---

## 3. Sin bastion

**Decisión:** el bastion EC2 del TP3 se eliminó.

**Alternativas:**
- (a) Mantener bastion en subnet pública → señalado por Faustino.
- (b) Mover bastion a subnet privada + VPC Endpoints SSM → requiere VPC para nada más.
- (c) **Eliminar bastion (elegida).** → Sin RDS, no hay caso de uso.

**Trade-off:** ninguno relevante. El bastion solo servía para que un DBA hiciera port-forwarding a RDS via SSM — sin RDS no hay nada que forwardear.

**Si tuviéramos que dar acceso DBA en producción real:** Lambda one-shot con permisos limitados invocada por el DBA via AWS CLI, logueada por CloudTrail.

---

## 4. Cognito Authorizer (no validación manual)

**Decisión:** el JWT lo valida API Gateway con un Cognito Authorizer, no la Lambda.

**Alternativas:**
- (a) Validación manual con `python-jose` dentro de la Lambda (TP3).
- (b) Lambda Authorizer custom → más código.
- (c) **Cognito Authorizer (elegida).** → Patrón canónico de AWS.

**Trade-off:** dependencia más fuerte en API Gateway (si el servicio se cae, no se puede validar). Pero API GW es serverless administrado por AWS — alta disponibilidad por diseño.

**Ganancias:**
- Sin token válido → `401` en el perímetro, sin gastar invocación de Lambda.
- Cero código de validación que mantener.
- Layer `python-jose` eliminado.
- Patrón estándar reconocible por cualquier arquitecto AWS.

**Excepción documentada:** el API GW de `auth-callback` queda con `authorization = "NONE"` por el workaround Cognito (ver `teoria/notas-de-clase/workaround-cognito.md`).

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

## 8. chat-handler regional (no en VPC)

**Decisión:** la Lambda `chat-handler` no está en VPC.

**Alternativa eliminada:** ponerla en VPC con NAT Gateway para alcanzar Anthropic API.

**Razón:** ponerla en VPC requiere NAT Gateway (USD/hora) y agrega cold start de 500ms-2s. Sin VPC no hay perímetro de red pero la seguridad está en:
- Cognito Authorizer rechaza requests sin JWT.
- LabRole limita las acciones AWS.
- API key Anthropic en Secrets Manager (no en código).
- CloudTrail audita todas las API calls.

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

**Decisión:** eliminamos el layer.

**Razón:** era usado por `chat-handler` para validar JWT manualmente. Con Cognito Authorizer la validación ya no está en la Lambda → `python-jose` se eliminó.

**Beneficio colateral:** menos peso del cold start, menos superficie de seguridad (el código de `python-jose` ya no se ejecuta).

---

## 13. Dos tablas DynamoDB en TP4 (bounded contexts)

**Decisión:** la única tabla del TP3 se partió en dos single-design: `jetsmart-prod-conversations` (estado del chatbot) + `jetsmart-prod-business` (PSS-like).

**Alternativas:**
- (a) Mantener single-table con todo (TP3) → mezcla conceptos, dificulta retention y reemplazo del canal.
- (b) Una tabla por entidad (USERS, FLIGHTS, RESERVATIONS, ...) → rompe single-table design, multiplica RCU/WCU.
- (c) **Dos tablas, una por bounded context (elegida)** → bounded contexts del DDD, manteniendo single-table dentro de cada uno.

**Trade-off:** dos conexiones de cliente DynamoDB en `chat_handler`, una operación más en `terraform apply`, dos backups diarios. Ganancia: separación clara de responsabilidades, failure isolation, retention policies independientes, reemplazabilidad del canal sin tocar el negocio.

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
- "Encontrar PNR de Juan Pérez" → Query GSI3 ReservationsByPassenger.
- Pasajero CRM separado (`PASSENGER#{dni}`) con back-refs históricos.

---

## 15. Derivación a humano vía SQS (no llamada directa al call center)

**Decisión:** la tool `escalate_to_human` del chatbot publica a SQS `human-handoff`; la Lambda `human_handoff_processor` consume y simula el POST al call center.

**Alternativas:**
- (a) `chat_handler` llama directo a la API del call center → acopla disponibilidad y latencia.
- (b) **SQS intermediario (elegida)** → desacople + reintentos + DLQ.

**Trade-off:** un componente más en el path (SQS). Ganancia: si el call center está caído, el pedido queda esperando 14 días en la cola; reintentos automáticos con DLQ para alarma; trazabilidad de todos los handoffs en conversations table.

**Por qué SQS y no SNS:** un pedido de handoff tiene un único consumer lógico (el sistema del call center). SNS sería over-engineering. Si en el futuro queremos fan-out (analytics + call center + Slack del equipo de soporte), agregamos un SNS por delante; hoy no hay necesidad.

---

## 16. Notificaciones proactivas vs polling

**Decisión:** las cancelaciones de vuelo se notifican proactivamente vía SNS `flight-events` → SQS `proactive-notifications` → Lambda → SNS `notifications` (emails).

**Alternativas:**
- (a) El usuario consulta periódicamente (polling) → mala UX, carga la tabla.
- (b) Una Lambda cron que escanee la tabla buscando vuelos cancelados → carga el GSI innecesariamente y agrega latencia (los pasajeros se enteran cuando corre la cron, no cuando se canceló).
- (c) **Event-driven push (elegida)** → el módulo de ops de la aerolínea publica un evento, los suscriptores se enteran al instante.

**Por qué GSI2 ReservationsByFlight:** sin él, encontrar los pasajeros afectados requiere Scan de toda la business table — O(n) lineal. Con GSI2, una sola Query devuelve la lista — O(log n). Es **el habilitador técnico** del feature.

**Demo offline:** el evento de `flight-events` lo publica el módulo de operaciones de la aerolínea cuando marca un vuelo como cancelado. En el TP, ese rol lo cumple el script CLI `scripts/cancel_flight.py` (mismo payload SNS, mismo `UpdateItem` sobre la tabla `business`). El flujo se prueba antes del demo y se muestra el resultado en CloudWatch logs durante la presentación, sin disparar en vivo.

---

## 17. Boarding pass async vía SQS

**Decisión:** el Saga PostBookingActions ya no invoca la Lambda de boarding pass directamente — publica un mensaje a SQS `boarding-pass-generation` y la Lambda `boarding_pass_async` la consume.

**Alternativas:**
- (a) Mantener sync en el Saga (TP3) → un error en BP frena la confirmación post-pago.
- (b) `.waitForTaskToken` pattern → el Saga espera al BP. Más complejo en ASL.
- (c) **Fire-and-forget vía SQS (elegida)** → simple, desacopla, retry automático con DLQ.

**Trade-off:** el BP no está inmediatamente disponible. El usuario consulta y, si todavía no se generó, recibe "tu boarding pass se está generando, intentá en unos segundos". En la práctica el BP está listo en <2 segundos.

**Por qué es correcto:** la reserva confirmada es lo crítico — no debe esperar al BP. Si la generación fallara, hoy queda en DLQ con alarma; antes hubiera dejado el Saga incompleto. Demuestra el patrón de **decoupling fire-and-forget desde Step Functions**.

---

## 18. CloudTrail multi-region como capa de auditoría

**Decisión:** trail multi-region con management events + global service events, log file validation activada, sink en bucket S3 dedicado con lifecycle de 90 días. **Sin** Glue catalog ni Athena workgroup — consulta de logs ad-hoc vía CLI.

**Alternativas:**
- (a) Sin CloudTrail → no hay traza de quién hizo qué en la cuenta (failure de gobernanza).
- (b) Trail single-region → pierde IAM/STS (servicios globales) y actividad en otras regiones.
- (c) Trail + CloudWatch Logs → bloqueado por AWS Academy (no se puede habilitar el sink a CloudWatch).
- (d) Trail + Glue Catalog + Athena → probado y descartado: el JSON classifier default de Glue no infiere bien la estructura `{"Records":[...]}` de CloudTrail (queda una columna `records` de tipo array que rompe las queries útiles). Habría requerido custom classifier o tabla manual con `CloudTrailSerde`.
- (e) **Trail multi-region + S3 con consulta ad-hoc (elegida)** → para la frecuencia de auditoría del TP, la diferencia entre Athena y CLI no justifica el overhead de mantener Glue.

**Trade-off:** sin Athena, queries SQL no son directas. Para investigar un evento puntual: `aws s3 cp s3://...-cloudtrail/AWSLogs/<acc>/CloudTrail/<region>/<año>/<mes>/<día>/*.json.gz . && zcat *.gz | jq '.Records[]'`. En producción real se montaría un Lake Formation blueprint de CloudTrail o un SIEM (Datadog, Splunk, OpenSearch).

**Por qué es correcto:**
- Compensa la pérdida de VPC Flow Logs (decisión #1) en el plano de management.
- Captura *fuera* de cuenta no auditable: si alguien rota la API key de Anthropic vía consola, queda registrado.
- `enable_log_file_validation = true` produce digest SHA-256 firmados → detección de tampering ex-post.
- Lifecycle a 90 días: costo ~0 en sandbox con uso bajo.
- Multi-region significa que un atacante no puede "esquivar" la auditoría operando en `us-west-2`.

**Por qué no data events (S3/DynamoDB):** cuestan ~$0.10 por 100k events y para los criterios del TP4 alcanza con management events. Si en producción quisiéramos auditar quién descarga cada boarding pass, se prende `data_resource` sobre el bucket `boarding_passes` agregando ~5 líneas al recurso `aws_cloudtrail`.

**Pregunta esperable en oral:** *"¿Cómo consultás los logs sin CloudWatch?"* → vía CLI con `aws s3 cp` + `jq` para investigaciones puntuales. Por qué no Athena: el JSON classifier default no parsea bien la estructura wrapped de CloudTrail, y montar un classifier custom o un schema manual para un consumo de baja frecuencia es sobreingeniería para este TP — en producción iría un SIEM o Lake Formation.

---

## 19. Email del JWT, no preguntárselo al usuario en el chat

**Decisión:** la tool `create_reservation` ignora el campo `email_contacto` del input y usa el claim `email` del JWT de Cognito. El system prompt instruye explícitamente *"NUNCA preguntar el email al usuario"*.

**Razón:** el usuario ya se autenticó vía Cognito Hosted UI — su email está validado por el IdP, llega firmado en el JWT y la API Gateway lo expone vía `event.requestContext.authorizer.claims.email`. Preguntárselo de nuevo en el chat es:
1. **Mala UX** — el usuario lo escribió hace 10 segundos en el login.
2. **Riesgo de tipos** — un email mal tipeado en el chat dispara mails fallidos sin que el sistema se entere.
3. **Inconsistente con el design de Cognito Authorizer** (decisión #4): si confiamos en el JWT para autenticación, también confiamos en sus claims para identidad.

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

**Decisión de atomicidad:** el PNR se genera en `chat_handler.py` con SHA-256 del payment_id ANTES de iniciar el Saga y se pasa en el input del state machine. ReserveFlight escribe `reserved_by = "PNR#XXXXXX"` desde el primer paso. Esto permite que la compensación `ReleaseFlight` libere exactamente el seat de este PNR (sin riesgo de tocar uno ajeno) y que el flujo sea idempotente bajo retry.

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

**Decisión:** se eliminó la columna `aerolinea: "JetSmart"` del seed y de las lecturas en `chat_handler`. Era un valor constante en toda la tabla.

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

**Pregunta esperable en oral:** *"¿Y si el user holdea desde un tab e intenta confirmar desde otro?"* → el `user_id` viene del JWT del Cognito Authorizer, es el mismo en ambos tabs. El handler reconoce el hold como propio y lo consume. **Funciona transparente.**

---

## 12. Frontend HTTP (no HTTPS)

**Decisión:** el frontend S3 sirve HTTP estático sin CloudFront.

**Alternativa:** S3 + CloudFront + ACM certificate + Route 53.

**Razón concreta:** AWS Academy limita Route 53 (no permite registrar dominios). Sin dominio propio, conseguir un cert válido es engorroso. Para el TP, HTTP es aceptable — los tokens viajan vía Cognito HTTPS y los API calls también son HTTPS.

**En producción real:** CloudFront delante con certificado ACM y dominio propio.

---

## Preguntas probables y respuestas cortas

> **¿Por qué eliminaste todo en lugar de "arreglar" lo que Faustino marcó?**

Porque al analizarlo en profundidad descubrimos que los 3 puntos tenían una raíz común: la VPC no tenía razón de ser sin RDS, y RDS no tenía razón de ser sin un equipo que la consumiera bien. Cambiar al patrón data lake resolvió los 3 puntos de raíz en lugar de parchearlos.

> **¿No es una arquitectura "menos serial" que la de TP3?**

Es menos componentes — y eso es justamente la fortaleza. YAGNI también aplica a arquitectura. Una VPC sin recursos persistentes es burocracia de red.

> **¿Cómo escalan los analytics a 10x el volumen?**

S3 escala infinito. Athena escala automáticamente (es serverless). Glue Crawler tarda más pero no bloquea queries. La única cosa que escalaría problemática es la velocidad de invocación de analytics-processor — para eso ya tenemos SQS con batching.

> **¿Qué hacen los `payment-*` Lambdas si una de ellas falla con error transitorio?**

Step Functions reintenta automáticamente con backoff exponencial (configurado en cada estado). Si después de N reintentos sigue fallando, el `Catch` dispara las compensaciones del Saga.

> **¿Cómo sabe el equipo de analytics qué columnas tiene `events`?**

Glue Crawler las descubre y las publica en el Glue Data Catalog. El equipo abre DBeaver y la tabla aparece con todas sus columnas y tipos. Si llega un nuevo `event_type` con campos nuevos, el crawler lo agrega en su próxima corrida (configurado con `update_behavior = "UPDATE_IN_DATABASE"`).
