"""Telemetry snapshot and validation (pure Python).

The integration layer converts Home Assistant states into a :class:`Snapshot`; the core never sees
HA objects. Each role has a plausible range; values outside it, P1P2's −128 °C sentinel, stale or
unavailable readings are reported as issues and replaced by ``None``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Role(StrEnum):
    """Telemetry roles. Values are the config keys used by the integration."""

    TI = "ti"  # room temperature at the thermostat, °C
    TO = "to"  # outdoor temperature, °C
    LWT = "lwt"  # leaving water, °C
    RWT = "rwt"  # return water, °C
    FLOW = "flow"  # L/min
    HEAT_W = "heat_w"  # thermal output, W
    ELEC_W = "elec_w"  # electrical input, W
    HZ = "hz"  # compressor frequency, Hz
    LWT_SET = "lwt_set"  # requested LWT, °C
    ROOM_SET = "room_set"  # room heating setpoint, °C
    HEAT_KWH = "heat_kwh"  # cumulative heating heat, kWh
    ELEC_KWH = "elec_kwh"  # cumulative heating electricity, kWh
    DHW_HEAT_KWH = "dhw_heat_kwh"  # cumulative DHW heat, kWh
    BUH_KWH = "buh_kwh"  # cumulative backup-heater electricity, kWh
    DEFROST = "defrost"  # binary
    DHW_ACTIVE = "dhw_active"  # binary
    HEATING_ENABLED = "heating_enabled"  # binary: space heating switched on
    HEATING_DEMAND = "heating_demand"  # binary: room thermostat calls for heat (main-zone valve)
    EXT_W = "ext_w"  # external electricity meter power (heat pump incl. DHW and standby), W
    EXT_KWH = "ext_kwh"  # external electricity meter cumulative energy, kWh
    DHW_ELEC_KWH = "dhw_elec_kwh"  # Daikin cumulative DHW electricity, kWh (to split the external meter)
    HEARTBEAT = "heartbeat"  # any bridge entity that changes every minute (e.g. Daikin clock)


REQUIRED_ROLES: tuple[Role, ...] = (
    Role.TI,
    Role.TO,
    Role.LWT,
    Role.RWT,
    Role.HZ,
    Role.HEAT_KWH,
    Role.ELEC_KWH,
)
BINARY_ROLES: frozenset[Role] = frozenset(
    {Role.DEFROST, Role.DHW_ACTIVE, Role.HEATING_ENABLED, Role.HEATING_DEMAND}
)
COUNTER_ROLES: frozenset[Role] = frozenset(
    {Role.HEAT_KWH, Role.ELEC_KWH, Role.DHW_HEAT_KWH, Role.BUH_KWH, Role.EXT_KWH, Role.DHW_ELEC_KWH}
)

# Plausible physical ranges (inclusive).
RANGES: dict[Role, tuple[float, float]] = {
    Role.TI: (5.0, 35.0),
    Role.TO: (-30.0, 45.0),
    Role.LWT: (0.0, 80.0),
    Role.RWT: (0.0, 80.0),
    Role.FLOW: (0.0, 100.0),
    Role.HEAT_W: (-2000.0, 20000.0),
    Role.ELEC_W: (0.0, 10000.0),
    Role.HZ: (0.0, 200.0),
    Role.LWT_SET: (0.0, 80.0),
    Role.ROOM_SET: (5.0, 35.0),
    Role.HEAT_KWH: (0.0, 1e9),
    Role.ELEC_KWH: (0.0, 1e9),
    Role.DHW_HEAT_KWH: (0.0, 1e9),
    Role.BUH_KWH: (0.0, 1e9),
    Role.EXT_W: (0.0, 15000.0),
    Role.EXT_KWH: (0.0, 1e9),
    Role.DHW_ELEC_KWH: (0.0, 1e9),
}

# P1P2MQTT publishes on change only, so an unchanged value (idle compressor, counters, setpoints) is
# not stale. Liveness of the bridge is checked through the heartbeat role instead; only the room and
# outdoor temperatures, which drift continuously, get their own age limit.
DEFAULT_MAX_AGE_S: dict[Role, float] = {
    Role.HEARTBEAT: 10 * 60.0,
    Role.TI: 3 * 3600.0,
    Role.TO: 3 * 3600.0,
}
FALLBACK_MAX_AGE_S = math.inf


@dataclass(frozen=True)
class Reading:
    """One raw reading. ``value`` is None for unavailable/unknown; ``age_s`` is seconds since last report."""

    value: float | None
    age_s: float = 0.0


@dataclass(frozen=True)
class Snapshot:
    time: datetime
    readings: dict[Role, Reading]


@dataclass(frozen=True)
class ValidatedSnapshot:
    time: datetime
    values: dict[Role, float]  # only valid readings
    issues: dict[Role, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return all(r in self.values for r in REQUIRED_ROLES) and Role.HEARTBEAT not in self.issues

    def get(self, role: Role) -> float | None:
        return self.values.get(role)


def validate(s: Snapshot, max_age_s: dict[Role, float] | None = None) -> ValidatedSnapshot:
    """Validate a snapshot. Missing optional roles are not issues; missing required roles are."""
    ages = {**DEFAULT_MAX_AGE_S, **(max_age_s or {})}
    values: dict[Role, float] = {}
    issues: dict[Role, str] = {}
    for role, rd in s.readings.items():
        v = rd.value
        if role is Role.HEARTBEAT:
            # any state counts as alive; only its age matters
            if v is None:
                issues[role] = "bridge heartbeat unavailable"
            elif rd.age_s > ages[role]:
                issues[role] = f"bridge heartbeat stale ({rd.age_s / 60:.0f} min)"
            continue
        if v is None or not math.isfinite(v):
            issues[role] = "unavailable"
            continue
        if rd.age_s > ages.get(role, FALLBACK_MAX_AGE_S):
            issues[role] = f"stale ({rd.age_s / 60:.0f} min)"
            continue
        if role in BINARY_ROLES:
            values[role] = 1.0 if v else 0.0
            continue
        lo, hi = RANGES.get(role, (-math.inf, math.inf))
        if not lo <= v <= hi:
            issues[role] = f"out of range ({v:g})"
            continue
        values[role] = float(v)
    for role in REQUIRED_ROLES:
        if role not in s.readings:
            issues[role] = "not configured"
    return ValidatedSnapshot(s.time, values, issues)
