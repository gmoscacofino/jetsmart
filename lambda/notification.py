"""
Notifica al usuario el resultado de su reserva via SNS email.
Invocada directamente por Step Functions en dos puntos del workflow:
  - PostBookingActions (Parallel branch) con event_type=booking_confirmed
  - NotifyBookingFailed con event_type=booking_failed
"""
import os, json, logging

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION           = os.environ["AWS_REGION_VAR"]
NOTIFICATION_ARN = os.environ.get("SNS_NOTIFICATION_ARN", "")

sns = boto3.client("sns", region_name=REGION)


def handler(event, context):
    event_type = event.get("event_type", "booking_unknown")
    data       = event.get("data", {})

    reservation_id = data.get("reservation_id", "—")
    flight_info    = data.get("flight_info", {})
    reservation    = data.get("reservation", {})
    total          = data.get("total_pagado", 0)
    email          = reservation.get("email_contacto", "—")

    if event_type == "booking_confirmed":
        subject = f"Reserva confirmada — {flight_info.get('ruta', reservation_id)}"
        message = (
            f"Tu reserva fue confirmada exitosamente.\n\n"
            f"Código de reserva: {reservation_id}\n"
            f"Ruta:              {flight_info.get('ruta', '—')}\n"
            f"Vuelo:             {flight_info.get('vuelo_numero', '—')}\n"
            f"Fecha:             {flight_info.get('fecha', '—')}\n"
            f"Pasajeros:         {reservation.get('pasajeros', '—')}\n"
            f"Tarifa:            {reservation.get('tarifa', '—')}\n"
            f"Total pagado:      ${total:.2f}\n\n"
            f"Podés ver tu reserva en 'Mis Reservas' dentro del chatbot.\n"
            f"Para hacer check-in, escribí al asistente con tu código de reserva."
        )
    else:
        subject = "No se pudo completar tu reserva"
        message = (
            f"Hubo un problema al procesar tu reserva.\n\n"
            f"Por favor intentá de nuevo o contactá soporte con el código: "
            f"{data.get('payment_id', '—')}"
        )

    log.info("Notificando %s — reserva: %s — email: %s", event_type, reservation_id, email)

    if not NOTIFICATION_ARN:
        log.warning("SNS_NOTIFICATION_ARN no configurado, notificación omitida")
        return {"notified": False, "reason": "no_topic_configured"}

    try:
        sns.publish(
            TopicArn=NOTIFICATION_ARN,
            Subject=subject,
            Message=message,
        )
        log.info("Notificación SNS enviada para reserva %s", reservation_id)
    except Exception as e:
        log.error("Error enviando notificación SNS: %s", e)

    return {"notified": True, "event_type": event_type}
