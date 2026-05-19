#!/usr/bin/env python3
"""
Carga vuelos de ejemplo en DynamoDB para el chatbot JetSmart.
Uso: python3 scripts/seed_flights.py <TABLE_NAME>

Genera vuelos los próximos 75 días (lunes, miércoles y viernes)
para las rutas que opera JetSmart en Sudamérica.
"""
import sys
from datetime import date, timedelta
from decimal import Decimal

import boto3

if len(sys.argv) < 2:
    print("Uso: python3 scripts/seed_flights.py <TABLE_NAME>")
    sys.exit(1)

TABLE_NAME = sys.argv[1]
dynamodb   = boto3.resource("dynamodb", region_name="us-east-1")
table      = dynamodb.Table(TABLE_NAME)

# (origen, destino, vuelo, hora_salida, hora_llegada, duracion_min, precio_usd)
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

def fechas_proximos_dias(dias=75):
    hoy = date.today()
    return [
        (hoy + timedelta(days=i)).isoformat()
        for i in range(1, dias + 1)
        if (hoy + timedelta(days=i)).weekday() in (0, 2, 4)  # lun, mié, vie
    ]

def duracion_str(minutos):
    return f"{minutos // 60}h {minutos % 60:02d}m"

FECHAS = fechas_proximos_dias()

items = []
for origen, destino, vuelo, hora_salida, hora_llegada, duracion_min, precio_base in RUTAS:
    for fecha in FECHAS:
        d = date.fromisoformat(fecha)
        # viernes ~15% más caro
        precio   = round(precio_base * (1.15 if d.weekday() == 4 else 1.0), 2)
        asientos = 120 if precio_base < 60 else (100 if precio_base < 100 else 80)
        gate_num  = (abs(hash(vuelo)) % 20) + 1

        items.append({
            "PK":                  f"FLIGHT#{origen}#{destino}",
            "SK":                  f"DATE#{fecha}",
            "vuelo_numero":        vuelo,
            "fecha":               fecha,
            "origen":              origen,
            "destino":             destino,
            "hora_salida":         hora_salida,
            "hora_llegada":        hora_llegada,
            "duracion":            duracion_str(duracion_min),
            "precio":              Decimal(str(precio)),
            "asientos_disponibles": asientos,
            "aerolinea":           "JetSmart",
            "estado_vuelo":        "EN_HORARIO",
            "horario_salida_real": hora_salida,
            "puerta":              f"{gate_num:02d}",
            "demora_minutos":      0,
        })

print(f"Cargando {len(items)} vuelos en {TABLE_NAME}...")

with table.batch_writer() as batch:
    for item in items:
        batch.put_item(Item=item)

print(f"OK: {len(items)} vuelos cargados ({len(RUTAS)} rutas x {len(FECHAS)} fechas).")
