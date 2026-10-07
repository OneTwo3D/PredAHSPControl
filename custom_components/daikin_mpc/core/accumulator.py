"""Aggregate validated snapshots into hourly and daily records.

Hour records hold time-weighted means, compressor min/max, counter increments (with meter-reset
handling) and flags. Day records feed the building learner and need near-complete coverage.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .intervals import IntervalClass, IntervalSummary, classify
from .telemetry import Role, ValidatedSnapshot
from .timeutil import add_hours, day_length_h, elapsed_s, hour_key

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
    counter_gap: bool = False  # counter increments across a midnight-spanning gap were dropped
    counter_stale: bool = False  # counters not readable (bridge heartbeat stale) during this hour

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
    # Length of the local day (23 or 25 on daylight-saving change days).
    length_h: float = 24.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Acc:
    start: datetime  # local start of the hour
    key: datetime  # UTC start of the hour (unique across daylight-saving changes)
    covered_s: float = 0.0
    sums: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    hz_min: float = math.inf
    hz_max: float = -math.inf
    flow_min: float = math.inf
    deltas: dict[str, float] = field(default_factory=dict)
    defrost: bool = False
    dhw: bool = False
    counter_gap: bool = False
    counter_stale: bool = False


def _local_date(t: datetime, ref: datetime) -> date:
    return (t.astimezone(ref.tzinfo) if ref.tzinfo is not None and t.tzinfo is not None else t).date()


def _split(
    incs: dict[str, tuple[float, datetime]], now: datetime, boundary: datetime
) -> tuple[dict[str, float], dict[str, tuple[float, datetime]]]:
    """Split each increment (covering ``since``→``now``) at ``boundary`` by elapsed time."""
    before: dict[str, float] = {}
    after: dict[str, tuple[float, datetime]] = {}
    for k, (inc, since) in incs.items():
        total = elapsed_s(since, now)
        frac = min(1.0, max(0.0, elapsed_s(since, boundary) / total)) if total > 0 else 0.0
        before[k] = inc * frac
        after[k] = (inc - before[k], boundary)
    return before, after


def _add(a: _Acc, incs: dict[str, float]) -> None:
    for k, v in incs.items():
        a.deltas[k] = a.deltas.get(k, 0.0) + v  # key presence also marks the counter as seen


class HourAccumulator:
    """Feed validated snapshots in time order; completed hours are returned by :meth:`add`."""

    def __init__(self) -> None:
        self._acc: _Acc | None = None
        self._last_time: datetime | None = None
        self._last_counter: dict[str, float] = {}
        # When the counter baselines were last updated (persisted with them); increments spanning a
        # longer gap (HA restart, bridge outage) cannot be placed in time and are not counted.
        self.counter_time: dict[str, datetime] = {}

    def add(self, s: ValidatedSnapshot) -> list[HourRecord]:
        done: list[HourRecord] = []
        key = hour_key(s.time)
        # Counter increments cover the interval since each counter's previous reading; they are
        # computed before the hour rolls over so the part of the interval that lies in the closing
        # hour (e.g. 23:55-00:00) is booked there, not to the next hour (or date).
        incs, gap_dropped, stale = self._counter_increments(s)
        old = self._acc
        if old is not None and key != old.key:
            to_old, incs = _split(incs, s.time, add_hours(old.key, 1.0))
            _add(old, to_old)
            old.counter_stale |= stale
            done.append(self._finish(old))
            self._acc = None
        if self._acc is None:
            local = key.astimezone(s.time.tzinfo) if s.time.tzinfo is not None else key
            self._acc = _Acc(start=local, key=key)
        a = self._acc
        _add(a, {k: inc for k, (inc, _) in incs.items()})
        a.counter_stale |= stale
        a.counter_gap |= gap_dropped
        dt = 0.0
        if self._last_time is not None:
            dt = elapsed_s(self._last_time, s.time)
            dt = dt if 0 < dt <= MAX_GAP_S else 0.0
            dt = min(dt, elapsed_s(a.key, s.time))  # only time inside this hour
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
        return done

    def _counter_increments(
        self, s: ValidatedSnapshot
    ) -> tuple[dict[str, tuple[float, datetime]], bool, bool]:
        """Per counter: (increment, start of the interval it covers); whether an increment across a
        midnight gap was dropped; whether any counter could not be read (stale bridge or a mapped
        counter unavailable), so energy may still be outstanding."""
        incs: dict[str, tuple[float, datetime]] = {}
        dropped = False
        # Counters only from a live bridge: with a stale heartbeat HA still holds the last values, and
        # refreshing the baselines from them would hide the outage from the gap handling below.
        live = Role.HEARTBEAT not in s.issues
        stale = not live or any(role in s.issues for role in COUNTERS)
        for role in COUNTERS if live else ():
            v = s.get(role)
            if v is None:
                continue
            k = role.value
            prev = self._last_counter.get(k)
            seen = self.counter_time.get(k)
            gap = seen is None or not 0 <= elapsed_s(seen, s.time) <= MAX_GAP_S
            # After a gap (HA restart, bridge outage) the increment cannot be placed in time. Within
            # one local day it is kept, so daily totals stay right; across midnight it is dropped and
            # the day is marked so it is not used for learning.
            same_day = seen is not None and _local_date(seen, s.time) == s.time.date()
            inc = 0.0
            if prev is not None and gap and not same_day:
                dropped = True
            elif prev is not None:
                if v >= prev:
                    inc = v - prev
                elif v < prev - RESET_THRESHOLD_KWH:
                    inc = v  # meter reset: energy counted since the reset
                else:
                    v = prev  # small decrease: noise, keep the higher value
            self._last_counter[k] = v
            self.counter_time[k] = s.time
            incs[k] = (inc, seen if seen is not None else s.time)
        return incs, dropped, stale

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
            a.counter_gap,
            a.counter_stale,
        )


class DayAggregator:
    """Collect hour records by local date; emits a DayRecord when the date changes."""

    MIN_HOURS = 22

    def __init__(self) -> None:
        self._day: str | None = None
        self._tz: object = None
        self._hours: list[HourRecord] = []

    def add(self, h: HourRecord) -> DayRecord | None:
        day = h.start.date().isoformat()
        out = None
        if self._day is not None and day != self._day:
            # The closing date's total is only known to be complete if the counters were readable at the
            # boundary: a stale bridge at the end of the old date or the start of the new one (or a gap
            # reaching into this hour) means energy may still be outstanding.
            last = self._hours[-1] if self._hours else None
            boundary_bad = h.counter_gap or h.counter_stale or (last is not None and last.counter_stale)
            out = None if boundary_bad else self._finish()
            self._hours = []
        self._day = day
        self._tz = h.start.tzinfo
        self._hours.append(h)
        return out

    def _finish(self) -> DayRecord | None:
        hs = [h for h in self._hours if h.coverage >= 0.75 and h.cls != IntervalClass.INVALID.value]
        if self._day is None or any(h.counter_gap for h in self._hours):
            return None
        length_h = day_length_h(date.fromisoformat(self._day), self._tz)
        if len(hs) < self.MIN_HOURS + (length_h - 24):  # same allowance for missing hours on 23/25 h days
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
            length_h=length_h,
        )


def hours_between(a: datetime, b: datetime) -> float:
    return (b - a) / timedelta(hours=1)
