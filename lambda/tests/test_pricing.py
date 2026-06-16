"""
Tests unitarios para lambda/pricing.py.
Sin dependencias AWS — corren con: cd lambda && python -m pytest tests/
"""
import sys, os
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pricing import compute_total, validate_inputs, PricingError  # noqa: E402


def test_basic_sin_extras_1pax():
    r = compute_total(Decimal("100"), "BASIC", [], 1)
    assert r["total"] == Decimal("100.00")


def test_full_flex_multiplica_1_5():
    r = compute_total(Decimal("100"), "FULL FLEX", [], 1)
    assert r["total"] == Decimal("150.00")


def test_smart_3_pax():
    # 100 × 1.25 × 3 = 375
    r = compute_total(Decimal("100"), "SMART", [], 3)
    assert r["total"] == Decimal("375.00")


def test_mascota_se_suma_una_vez_por_reserva():
    # BASIC 100 × 2 pax + mascota $35 (no per pax) = 235
    r = compute_total(Decimal("100"), "BASIC", ["mascota"], 2)
    assert r["total"] == Decimal("235.00")


def test_extra_incluido_en_smart_no_cobra():
    # SMART incluye equipaje_bodega → $0 en el desglose, total = 100 × 1.25 = 125
    r = compute_total(Decimal("100"), "SMART", ["equipaje_bodega"], 1)
    assert r["total"] == Decimal("125.00")
    assert r["desglose"]["extras"]["equipaje_bodega"] == Decimal("0.00")


def test_tarifa_invalida_raises():
    with pytest.raises(PricingError):
        compute_total(Decimal("100"), "BUSINESS", [], 1)


def test_extra_desconocido_raises():
    with pytest.raises(PricingError):
        compute_total(Decimal("100"), "BASIC", ["caviar"], 1)


def test_pasajeros_cero_raises():
    with pytest.raises(PricingError):
        compute_total(Decimal("100"), "BASIC", [], 0)


def test_redondeo_dos_decimales():
    # 99.99 × 1.10 = 109.989 → 109.99
    r = compute_total(Decimal("99.99"), "LIGHT", [], 1)
    assert r["total"] == Decimal("109.99")


def test_validate_inputs_acepta_validos():
    validate_inputs("FULL FLEX", ["mascota", "equipaje_bodega"])  # no raise


def test_validate_inputs_rechaza_tarifa_invalida():
    with pytest.raises(PricingError):
        validate_inputs("PRIVATE_JET", [])


def test_validate_inputs_rechaza_extra_desconocido():
    with pytest.raises(PricingError):
        validate_inputs("BASIC", ["sushi"])


def test_desglose_completo():
    # SMART 2 pax + mascota + flexismart sobre $80 base
    # 80 × 1.25 = 100 por pax → 200 subtotal tarifa
    # extras: mascota 35 + flexismart 25 = 60
    # total = 260
    r = compute_total(Decimal("80"), "SMART", ["mascota", "flexismart"], 2)
    assert r["total"] == Decimal("260.00")
    assert r["desglose"]["base_por_pasajero"] == Decimal("100.00")
    assert r["desglose"]["subtotal_tarifa"] == Decimal("200.00")
    assert r["desglose"]["subtotal_extras"] == Decimal("60.00")
    assert r["desglose"]["extras"]["mascota"] == Decimal("35.00")
    assert r["desglose"]["extras"]["flexismart"] == Decimal("25.00")
