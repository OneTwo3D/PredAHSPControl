"""Convert readings to the core's units (°C, W, kWh, L/min, Hz).

Home Assistant entities carry ``unit_of_measurement``; a mapped entity in Wh, kW or °F would otherwise
be read as kWh, W or °C and could still pass the plausibility ranges. Readings without a unit are taken
as already in core units (template sensors often have none); a unit that cannot be converted is an
error rather than a silent misreading.
"""

from __future__ import annotations

from collections.abc import Callable

from .telemetry import Role

_TEMP: dict[str, Callable[[float], float]] = {
    "°C": lambda v: v,
    "°F": lambda v: (v - 32.0) / 1.8,
    "K": lambda v: v - 273.15,
}
_POWER: dict[str, Callable[[float], float]] = {
    "W": lambda v: v,
    "kW": lambda v: v * 1000.0,
    "MW": lambda v: v * 1e6,
}
_ENERGY: dict[str, Callable[[float], float]] = {
    "Wh": lambda v: v / 1000.0,
    "kWh": lambda v: v,
    "MWh": lambda v: v * 1000.0,
}
_FLOW: dict[str, Callable[[float], float]] = {
    "L/min": lambda v: v,
    "l/min": lambda v: v,
    "L/h": lambda v: v / 60.0,
    "l/h": lambda v: v / 60.0,
    "m³/h": lambda v: v * 1000.0 / 60.0,
}
_FREQ: dict[str, Callable[[float], float]] = {"Hz": lambda v: v}

ROLE_UNITS: dict[Role, dict[str, Callable[[float], float]]] = {
    Role.TI: _TEMP,
    Role.TO: _TEMP,
    Role.LWT: _TEMP,
    Role.RWT: _TEMP,
    Role.LWT_SET: _TEMP,
    Role.ROOM_SET: _TEMP,
    Role.HEAT_W: _POWER,
    Role.ELEC_W: _POWER,
    Role.EXT_W: _POWER,
    Role.HEAT_KWH: _ENERGY,
    Role.ELEC_KWH: _ENERGY,
    Role.DHW_HEAT_KWH: _ENERGY,
    Role.BUH_KWH: _ENERGY,
    Role.EXT_KWH: _ENERGY,
    Role.DHW_ELEC_KWH: _ENERGY,
    Role.FLOW: _FLOW,
    Role.HZ: _FREQ,
}


def to_core_unit(role: Role, value: float, unit: str | None) -> float:
    """``value`` in ``unit`` converted to the core unit of ``role``; raises ValueError if impossible."""
    table = ROLE_UNITS.get(role)
    if table is None or not unit:
        return value
    conv = table.get(unit.strip())
    if conv is None:
        raise ValueError(f"unsupported unit {unit!r}")
    return conv(value)


def temperature_c(value: float, unit: str | None) -> float:
    """Temperature in °C from ``unit`` (None = °C)."""
    if not unit:
        return value
    conv = _TEMP.get(unit.strip())
    if conv is None:
        raise ValueError(f"unsupported temperature unit {unit!r}")
    return conv(value)
