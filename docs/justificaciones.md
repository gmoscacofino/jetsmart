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
- (a) El usuario consulta periódicamente (polling) → mala UX, carga DynamoDB.
- (b) Polling desde el sistema PSS hacia DynamoDB → carga GSI innecesariamente.
- (c) **Event-driven push (elegida)** → el sistema PSS publica un evento, los suscriptores se enteran.

**Por qué GSI2 ReservationsByFlight:** sin él, encontrar los pasajeros afectados requiere Scan de toda la business table — O(n) lineal. Con GSI2, una sola Query devuelve la lista — O(log n). Es **el habilitador técnico** del feature.

**Demo offline:** en producción el evento vendría del sistema interno de ops cuando marca un vuelo como cancelado. En el TP el trigger es un script CLI (`scripts/cancel_flight.py`); el flujo se prueba antes del demo y se muestra el resultado en CloudWatch logs durante la presentación, sin disparar en vivo.

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
