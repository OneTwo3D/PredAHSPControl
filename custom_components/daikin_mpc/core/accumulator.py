"""Aggregate validated snapshots into hourly and daily records.

Hour records hold time-weighted means, compressor min/max, counter increments (with meter-reset
handling) and flags. Day records feed the building learner and need near-complete coverage.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .intervals import IntervalClass, IntervalSummary, classify
from .telemetry import Role, ValidatedSnapshot

MEAN_ROLES = (
    Role.TI,
    Role.TO,
    Role.LWT,
    Role.RWT,
    Role.FLOW,
    Role.HEAT_W,
    Role.ELEC_W,
    Role.LWT_SET,
    Role.ROOM_SET,
    Role.EXT_W,
)
COUNTERS = (Role.HEAT_KWH, Role.ELEC_KWH, Role.DHW_HEAT_KWH, Role.BUH_KWH, Role.EXT_KWH, Role.DHW_ELEC_KWH)
# A counter decrease larger than this is a meter reset; a smaller one is noise and ignored.
RESET_THRESHOLD_KWH = 1.0
MAX_GAP_S = 20 * 60.0  # samples further apart than this do not contribute time


@dataclass
class HourRecord:
    start: datetime
    coverage: float  # fraction of the hour covered by valid samples
    means: dict[str, float]
    hz_min: float
    hz_max: float
    flow_min: float
    deltas_kwh: dict[str, float]
    defrost: bool
    dhw: bool
    cls: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        return d


@dataclass
class DayRecord:
    day: str  # local ISO date
    hours: int
    ti: float
    to: float
    heat_kwh: float
    elec_kwh: float
    dhw_hours: int
    defrost_hours: int
    # External meter (None when not configured): whole heat pump incl. DHW and standby.
    ext_kwh: float | None = None
    # External meter minus Daikin DHW electricity: space heating incl. standby and pump.
    heating_ext_kwh: float | None = None
    # Mean external power in hours with the compressor off all hour (standby), W.
    standby_w: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Acc:
    start: datetime
    covered_s: float = 0.0
    sums: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    hz_min: float = math.inf
    hz_max: float = -math.inf
    flow_min: float = math.inf
    deltas: dict[str, float] = field(default_factory=dict)
    defrost: bool = False
    dhw: bool = False


class HourAccumulator:
    """Feed validated snapshots in time order; completed hours are returned by :meth:`add`."""

    def __init__(self) -> None:
        self._acc: _Acc | None = None
        self._last_time: datetime | None = None
        self._last_counter: dict[str, float] = {}

    @staticmethod
    def _hour(t: datetime) -> datetime:
        return t.replace(minute=0, second=0, microsecond=0)

    def add(self, s: ValidatedSnapshot) -> list[HourRecord]:
        done: list[HourRecord] = []
        hour = self._hour(s.time)
        if self._acc is not None and hour != self._acc.start:
            done.append(self._finish(self._acc))
            self._acc = None
        if self._acc is None:
            self._acc = _Acc(start=hour)
        a = self._acc
        dt = 0.0
        if self._last_time is not None:
            dt = (s.time - self._last_time).total_seconds()
            dt = dt if 0 < dt <= MAX_GAP_S else 0.0
            dt = min(dt, (s.time - a.start).total_seconds())  # only time inside this hour
        self._last_time = s.time
        if s.complete and dt > 0:
            a.covered_s += dt
            for role in MEAN_ROLES:
                v = s.get(role)
                if v is not None:
                    a.sums[role.value] = a.sums.get(role.value, 0.0) + v * dt
                    a.weights[role.value] = a.weights.get(role.value, 0.0) + dt
        hz = s.get(Role.HZ)
        if hz is not None:
            a.hz_min, a.hz_max = min(a.hz_min, hz), max(a.hz_max, hz)
        flow = s.get(Role.FLOW)
        if flow is not None:
            a.flow_min = min(a.flow_min, flow)
        a.defrost |= bool(s.get(Role.DEFROST))
        a.dhw |= bool(s.get(Role.DHW_ACTIVE))
        for role in COUNTERS:
            v = s.get(role)
            if v is None:
                continue
            k = role.value
            prev = self._last_counter.get(k)
            inc = 0.0
            if prev is not None:
                if v >= prev:
                    inc = v - prev
                elif v < prev - RESET_THRESHOLD_KWH:
                    inc = v  # meter reset: energy counted since the reset
                else:
                    v = prev  # small decrease: noise, keep the higher value
            self._last_counter[k] = v
            a.deltas[k] = a.deltas.get(k, 0.0) + inc
        return done

    def _finish(self, a: _Acc) -> HourRecord:
        means = {k: a.sums[k] / w for k, w in a.weights.items() if w > 0}
        deltas = dict(a.deltas)
        nan = float("nan")
        hz_min = a.hz_min if math.isfinite(a.hz_min) else nan
        hz_max = a.hz_max if math.isfinite(a.hz_max) else nan
        flow_min = a.flow_min if math.isfinite(a.flow_min) else nan
        dhw_kwh = deltas.get(Role.DHW_HEAT_KWH.value, nan)
        cls = classify(
            IntervalSummary(
                compressor_hz_min=hz_min,
                compressor_hz_max=hz_max,
                dhw_heat_kwh=1.0 if a.dhw else dhw_kwh,
                dhw_run_h=nan,
                backup_kwh=deltas.get(Role.BUH_KWH.value, nan),
                flow_l_min_min=flow_min,
                ti_c=means.get(Role.TI.value, nan),
                to_c=means.get(Role.TO.value, nan),
            )
        )
        if a.covered_s < 0.5 * 3600:
            cls = IntervalClass.INVALID
        return HourRecord(
            a.start,
            min(1.0, a.covered_s / 3600),
            means,
            hz_min,
            hz_max,
            flow_min,
            deltas,
            a.defrost,
            a.dhw,
            cls.value,
        )


class DayAggregator:
    """Collect hour records by local date; emits a DayRecord when the date changes."""

    MIN_HOURS = 22

    def __init__(self) -> None:
        self._day: str | None = None
        self._hours: list[HourRecord] = []

    def add(self, h: HourRecord) -> DayRecord | None:
        day = h.start.date().isoformat()
        out = None
        if self._day is not None and day != self._day:
            out = self._finish()
            self._hours = []
        self._day = day
        self._hours.append(h)
        return out

    def _finish(self) -> DayRecord | None:
        hs = [h for h in self._hours if h.coverage >= 0.75 and h.cls != IntervalClass.INVALID.value]
        if len(hs) < self.MIN_HOURS or self._day is None:
            return None
        ti = [h.means["ti"] for h in hs if "ti" in h.means]
        to = [h.means["to"] for h in hs if "to" in h.means]
        # Counter deltas over all hours (energy is not lost in low-coverage hours).
        heat = sum(h.deltas_kwh.get("heat_kwh", 0.0) for h in self._hours)
        elec = sum(h.deltas_kwh.get("elec_kwh", 0.0) for h in self._hours)
        ext = heating_ext = standby = None
        if all("ext_kwh" in h.deltas_kwh for h in hs):
            ext = sum(h.deltas_kwh.get("ext_kwh", 0.0) for h in self._hours)
            if all("dhw_elec_kwh" in h.deltas_kwh for h in hs):
                heating_ext = max(0.0, ext - sum(h.deltas_kwh.get("dhw_elec_kwh", 0.0) for h in self._hours))
            off = [h.means["ext_w"] for h in hs if h.cls == IntervalClass.OFF.value and "ext_w" in h.means]
            if len(off) >= 3:
                standby = sum(off) / len(off)
        return DayRecord(
            day=self._day,
            hours=len(hs),
            ti=sum(ti) / len(ti),
            to=sum(to) / len(to),
            heat_kwh=heat,
            elec_kwh=elec,
            dhw_hours=sum(h.dhw for h in self._hours),
            defrost_hours=sum(h.defrost for h in self._hours),
            ext_kwh=ext,
            heating_ext_kwh=heating_ext,
            standby_w=standby,
        )


def hours_between(a: datetime, b: datetime) -> float:
    return (b - a) / timedelta(hours=1)
