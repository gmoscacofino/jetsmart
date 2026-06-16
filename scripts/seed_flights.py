#!/usr/bin/env python3
"""
Carga vuelos de ejemplo en DynamoDB para el chatbot JetSmart.
Uso: python3 scripts/seed_flights.py <TABLE_NAME>

Genera vuelos los próximos 30 días (lunes, miércoles y viernes) para las rutas
que opera JetSmart en Sudamérica. Por cada vuelo crea:
  - 1 ítem "master row" con precio, horarios y datos del vuelo
    SK = DATE#YYYY-MM-DD#FLIGHT#JAXX
  - 120 ítems SEAT# (20 filas × 6 letras A-F)
    SK = DATE#YYYY-MM-DD#FLIGHT#JAXX#SEAT#<row><letter>

Los SEAT# se crean SIN reserved_by (libres). La Saga los reserva atómicamente
con ConditionExpression al confirmar la compra (ver payment_processor.py).
"""
import sys, random
from datetime import date, timedelta
from decimal import Decimal

import boto3

if len(sys.argv) < 2:
    print("Uso: python3 scripts/seed_flights.py <TABLE_NAME>")
    sys.exit(1)

TABLE_NAME = sys.argv[1]
dynamodb   = boto3.resource("dynamodb", region_name="us-east-1")
table      = dynamodb.Table(TABLE_NAME)

# (origen, destino, vuelo, hora_salida, hora_llegada, duracion_min, precio_base_usd)
RUTAS = [
    ("AEP", "SCL", "JA401",  "08:15", "10:30", 135,  89.0),
    ("SCL", "AEP", "JA402",  "11:30", "13:45", 135,  89.0),
    ("AEP", "SCL", "JA403",  "18:00", "20:15", 135, 105.0),
    ("SCL", "AEP", "JA404",  "21:00", "23:15", 135, 105.0),
    ("AEP", "MDZ", "JA201",  "07:30", "08:45",  75,  49.0),
    ("MDZ", "AEP", "JA202",  "09:30", "10:45",  75,  49.0),
    ("AEP", "MDZ", "JA203",  "17:00", "18:15",  75,  59.0),
    ("MDZ", "AEP", "JA204",  "19:00", "20:15",  75,  59.0),
    ("AEP", "COR", "JA101",  "06:45", "07:55",  70,  39.0),
    ("COR", "AEP", "JA102",  "09:00", "10:10",  70,  39.0),
    ("AEP", "COR", "JA103",  "20:00", "21:10",  70,  45.0),
    ("COR", "AEP", "JA104",  "22:00", "23:10",  70,  45.0),
    ("SCL", "IGR", "JA601",  "09:00", "12:30", 210, 119.0),
    ("IGR", "SCL", "JA602",  "13:30", "17:00", 210, 119.0),
    ("SCL", "ANF", "JA501",  "07:00", "08:30",  90,  55.0),
    ("ANF", "SCL", "JA502",  "09:30", "11:00",  90,  55.0),
    ("SCL", "COR", "JA701",  "10:00", "13:30", 210, 129.0),
    ("COR", "SCL", "JA702",  "14:30", "18:00", 210, 129.0),
    ("AEP", "IGR", "JA301",  "07:00", "08:45", 105,  69.0),
    ("IGR", "AEP", "JA302",  "09:30", "11:15", 105,  69.0),
]

# ── Layout de cabina ──────────────────────────────────────────────────────────
# 20 filas × 6 letras (A-F) = 120 asientos por vuelo.
# Categorías por fila — refleja layout real de cabina single-aisle.
SEAT_LETTERS = ("A", "B", "C", "D", "E", "F")
ROWS = list(range(1, 21))


def _seat_type(row: int) -> str:
    if row == 1:
        return "primera_fila"
    if 6 <= row <= 10:
        return "salida_rapida"
    if row in (14, 15):
        return "salida_emergencia"
    return "estandar"


def fechas_proximos_dias(dias=30):
    hoy = date.today()
    return [
        (hoy + timedelta(days=i)).isoformat()
        for i in range(1, dias + 1)
        if (hoy + timedelta(days=i)).weekday() in (0, 2, 4)  # lun, mié, vie
    ]


def duracion_str(minutos):
    return f"{minutos // 60}h {minutos % 60:02d}m"


def _precio(precio_base: float, vuelo: str, fecha: str) -> float:
    """
    Precio dinámico por vuelo+fecha:
    - Demanda por proximidad (revenue management básico)
    - Recargo de viernes
    - Ruido pequeño determinístico para evitar precios idénticos
    """
    d = date.fromisoformat(fecha)
    days_out = (d - date.today()).days

    if days_out <= 3:
        demand = 1.50
    elif days_out <= 7:
        demand = 1.30
    elif days_out <= 14:
        demand = 1.13
    elif days_out <= 30:
        demand = 1.03
    else:
        demand = 0.92   # tarifas early-bird

    dow_factor = 1.18 if d.weekday() == 4 else 1.00  # viernes +18 %

    rng = random.Random(hash(f"p|{vuelo}|{fecha}"))
    noise = rng.uniform(0.96, 1.04)

    return round(precio_base * demand * dow_factor * noise, 2)


FECHAS = fechas_proximos_dias()

items = []
for origen, destino, vuelo, hora_salida, hora_llegada, duracion_min, precio_base in RUTAS:
    gate_num = (abs(hash(vuelo)) % 20) + 1

    for fecha in FECHAS:
        precio = _precio(precio_base, vuelo, fecha)

        # Master row del vuelo (sin #SEAT# en el SK) — datos comerciales y horarios
        items.append({
            "PK":                   f"FLIGHT#{origen}#{destino}",
            "SK":                   f"DATE#{fecha}#FLIGHT#{vuelo}",
            "vuelo_numero":         vuelo,
            "fecha":                fecha,
            "origen":               origen,
            "destino":              destino,
            "hora_salida":          hora_salida,
            "hora_llegada":         hora_llegada,
            "duracion":             duracion_str(duracion_min),
            "precio":               Decimal(str(precio)),
            "estado_vuelo":         "EN_HORARIO",
            "horario_salida_real":  hora_salida,
            "puerta":               f"{gate_num:02d}",
            "demora_minutos":       0,
        })

        # Ítems SEAT# — 120 por vuelo. Atributo reserved_by ausente = libre.
        for row in ROWS:
            for letter in SEAT_LETTERS:
                seat_id = f"{row}{letter}"
                items.append({
                    "PK":           f"FLIGHT#{origen}#{destino}",
                    "SK":           f"DATE#{fecha}#FLIGHT#{vuelo}#SEAT#{seat_id}",
                    "seat_id":      seat_id,
                    "row":          row,
                    "letter":       letter,
                    "seat_type":    _seat_type(row),
                    "vuelo_numero": vuelo,
                    "fecha":        fecha,
                })

print(f"Cargando {len(items)} items en {TABLE_NAME} ({len(RUTAS)} rutas × {len(FECHAS)} fechas × ~121 items/vuelo)...")

with table.batch_writer() as batch:
    for item in items:
        batch.put_item(Item=item)

print(f"OK: {len(items)} items cargados.")
