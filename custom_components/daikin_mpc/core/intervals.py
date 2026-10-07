"""Classification of aggregated (e.g. hourly) intervals for training-data filtering."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

# P1P2 reports missing temperature sensors as about -128 °C.
INVALID_TEMPERATURE_BELOW_C = -100.0


class IntervalClass(StrEnum):
    HEATING_FULL = "heating_full"  # compressor ran for the whole interval, no DHW
    HEATING_PARTIAL = "heating_partial"  # compressor started and/or stopped within the interval
    OFF = "off"  # compressor off for the whole interval
    DHW = "dhw"  # any DHW production in the interval
    BACKUP_HEATER = "backup_heater"
    INVALID = "invalid"


@dataclass(frozen=True)
class IntervalSummary:
    """Aggregates for one interval. Any field may be NaN when unavailable."""

    compressor_hz_min: float
    compressor_hz_max: float
    dhw_heat_kwh: float
    dhw_run_h: float
    backup_kwh: float
    flow_l_min_min: float
    ti_c: float
    to_c: float


def _valid_temp(t: float) -> bool:
    return math.isfinite(t) and t > INVALID_TEMPERATURE_BELOW_C


def classify(s: IntervalSummary, min_flow_l_min: float = 3.0) -> IntervalClass:
    """Classify one interval. DHW and backup-heater take precedence over heating states."""
    if not (_valid_temp(s.ti_c) and _valid_temp(s.to_c)):
        return IntervalClass.INVALID
    if not (math.isfinite(s.compressor_hz_min) and math.isfinite(s.compressor_hz_max)):
        return IntervalClass.INVALID
    if math.isfinite(s.backup_kwh) and s.backup_kwh > 0:
        return IntervalClass.BACKUP_HEATER
    # Missing DHW information is treated as "possibly DHW" (conservative).
    if (
        not math.isfinite(s.dhw_heat_kwh)
        or s.dhw_heat_kwh > 0
        or (math.isfinite(s.dhw_run_h) and s.dhw_run_h > 0)
    ):
        return IntervalClass.DHW
    if s.compressor_hz_max <= 0:
        return IntervalClass.OFF
    if s.compressor_hz_min > 0 and math.isfinite(s.flow_l_min_min) and s.flow_l_min_min >= min_flow_l_min:
        return IntervalClass.HEATING_FULL
    return IntervalClass.HEATING_PARTIAL
