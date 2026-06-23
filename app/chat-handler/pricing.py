"""
Pricing server-side para JetSmart.

Single source of truth — chat_handler valida inputs, payment_processor.reserve_booking
y collect_payment computan total con esta función. El LLM (Claude) NUNCA calcula el
total; sólo presenta los componentes al usuario.

Diseño:
- Tarifas (BASIC/LIGHT/SMART/FULL FLEX) son MULTIPLICADORES sobre el precio base.
  Refleja el costo marginal real: un servicio premium escala con el costo del vuelo.
- Extras son MONTO FIJO en USD por reserva (no por pasajero). Refleja el costo
  operativo real: un kilo de bodega, una jaula de mascota, etc., no escala con
  el precio del ticket.
- Algunos extras vienen INCLUIDOS en tarifas superiores (ej. equipaje_mano en LIGHT).
  En ese caso se cobra $0 — visible en el desglose para auditoría.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable


TARIFA_MULTIPLIERS: dict[str, Decimal] = {
    "BASIC":     Decimal("1.00"),
    "LIGHT":     Decimal("1.10"),
    "SMART":     Decimal("1.25"),
    "FULL FLEX": Decimal("1.50"),
}

EXTRAS_FIJOS: dict[str, Decimal] = {
    "mascota":                   Decimal("35"),
    "asiento_estandar":          Decimal("8"),
    "asiento_salida_rapida":     Decimal("12"),
    "asiento_salida_emergencia": Decimal("15"),
    "asiento_primera_fila":      Decimal("20"),
    "flexismart":                Decimal("25"),
    "tarjeta_embarque":          Decimal("8"),
    "embarque_prioritario":      Decimal("10"),
    "equipaje_mano":             Decimal("15"),
    "equipaje_bodega":           Decimal("35"),
}

EXTRAS_INCLUIDOS_EN_TARIFA: dict[str, set[str]] = {
    "BASIC":     set(),
    "LIGHT":     {"equipaje_mano"},
    "SMART":     {"equipaje_mano", "equipaje_bodega", "asiento_estandar"},
    "FULL FLEX": {"equipaje_mano", "equipaje_bodega", "asiento_estandar",
                  "embarque_prioritario", "flexismart"},
}


class PricingError(ValueError):
    """Error de validación de pricing — input inválido del cliente."""
    pass


def _q(d: Decimal) -> Decimal:
    """Redondeo bancario a 2 decimales."""
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def validate_inputs(tarifa: str, extras: Iterable[str]) -> None:
    """
    Valida sólo nombres de tarifa y extras. No requiere precio_base.
    Sirve al chat_handler para rechazar inputs alucinados antes de iniciar el Saga.
    """
    if tarifa not in TARIFA_MULTIPLIERS:
        raise PricingError(f"Tarifa inválida: {tarifa}")
    for e in extras:
        if e not in EXTRAS_FIJOS:
            raise PricingError(f"Extra desconocido: {e}")


def compute_total(precio_base: Decimal, tarifa: str, extras: Iterable[str], pasajeros: int) -> dict:
    """
    Calcula total y desglose.

    Args:
        precio_base: precio del vuelo en DynamoDB (Decimal o convertible).
        tarifa: una de TARIFA_MULTIPLIERS.
        extras: iterable de keys de EXTRAS_FIJOS.
        pasajeros: int >= 1.

    Returns:
        {
            "total": Decimal (redondeado a 2),
            "desglose": {
                "base_por_pasajero": Decimal,
                "tarifa": str,
                "multiplicador": Decimal,
                "pasajeros": int,
                "subtotal_tarifa": Decimal,  # base_por_pax * pasajeros
                "extras": {extra_type: Decimal, ...},  # con $0 si están incluidos
                "subtotal_extras": Decimal,
                "total": Decimal,
            },
        }

    Raises:
        PricingError: tarifa inválida, extra desconocido, o pasajeros < 1.
    """
    if tarifa not in TARIFA_MULTIPLIERS:
        raise PricingError(f"Tarifa inválida: {tarifa}")
    if pasajeros < 1:
        raise PricingError(f"Pasajeros inválidos: {pasajeros}")

    base = Decimal(str(precio_base))
    mult = TARIFA_MULTIPLIERS[tarifa]
    incluidos = EXTRAS_INCLUIDOS_EN_TARIFA[tarifa]

    base_por_pax = _q(base * mult)
    subtotal_tarifa = _q(base_por_pax * pasajeros)

    extras_breakdown: dict[str, Decimal] = {}
    for e in extras:
        if e not in EXTRAS_FIJOS:
            raise PricingError(f"Extra desconocido: {e}")
        extras_breakdown[e] = Decimal("0.00") if e in incluidos else _q(EXTRAS_FIJOS[e])

    subtotal_extras = _q(sum(extras_breakdown.values(), Decimal("0")))
    total = _q(subtotal_tarifa + subtotal_extras)

    return {
        "total": total,
        "desglose": {
            "base_por_pasajero": base_por_pax,
            "tarifa":            tarifa,
            "multiplicador":     mult,
            "pasajeros":         pasajeros,
            "subtotal_tarifa":   subtotal_tarifa,
            "extras":            extras_breakdown,
            "subtotal_extras":   subtotal_extras,
            "total":             total,
        },
    }
