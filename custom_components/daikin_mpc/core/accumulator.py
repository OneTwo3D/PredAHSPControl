"""Aggregate validated snapshots into hourly and daily records.

Hour records hold time-weighted means, compressor min/max, counter increments (with meter-reset
handling) and flags. Day records feed the building learner and need near-complete coverage.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, tzinfo
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
    # a counter started a new baseline after its date began (first reading, re-mapping, lost state): the
    # date's energy before that point was not observed, so the date is incomplete
    counter_restart: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        for k in ("hz_min", "hz_max", "flow_min"):
            d[k] = _num_out(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HourRecord:
        start = datetime.fromisoformat(str(d["start"]))
        if start.tzinfo is None:
            raise ValueError("naive hour start")
        rec = cls(
            start=start,
            coverage=_num_in(d["coverage"], 0.0, 1.0),
            means={str(k): _num_in(v) for k, v in dict(d["means"]).items()},
            hz_min=_num_in(d.get("hz_min"), allow_nan=True),
            hz_max=_num_in(d.get("hz_max"), allow_nan=True),
            flow_min=_num_in(d.get("flow_min"), allow_nan=True),
            deltas_kwh={str(k): _num_in(v, 0.0) for k, v in dict(d["deltas_kwh"]).items()},
            defrost=bool(d["defrost"]),
            dhw=bool(d["dhw"]),
            cls=str(IntervalClass(d["cls"]).value),
            counter_gap=bool(d.get("counter_gap", False)),
            counter_stale=bool(d.get("counter_stale", False)),
            counter_restart=bool(d.get("counter_restart", False)),
        )
        if rec.cls != IntervalClass.INVALID.value and not {"ti", "to"} <= rec.means.keys():
            raise ValueError("usable hour without temperature means")
        return rec


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
    # Mean measured gains beyond the base gains (household electricity, sun, battery, tank), W.
    extra_gains_w: float = 0.0
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
    counter_restart: bool = False


def _num_out(v: float) -> float | None:
    """JSON-safe number (NaN/inf → None)."""
    return v if math.isfinite(v) else None


def _num_in(v: object, lo: float = -math.inf, hi: float = math.inf, allow_nan: bool = False) -> float:
    if v is None and allow_nan:
        return float("nan")
    x = float(v)  # type: ignore[arg-type]
    if not (math.isfinite(x) and lo <= x <= hi):
        raise ValueError(f"invalid number {v!r}")
    return x


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

    def add(self, s: ValidatedSnapshot, extras: dict[str, float] | None = None) -> list[HourRecord]:
        """``extras``: derived quantities averaged like the mean roles (e.g. ``gains_w``)."""
        done: list[HourRecord] = []
        if self._last_time is not None and elapsed_s(self._last_time, s.time) <= 0:
            # the clock went back (or a duplicate sample): the timeline is unreliable, so the current
            # date's energy balance is not used; samples are ignored until time passes the last one
            if self._acc is not None:
                self._acc.counter_restart = True
            return done
        key = hour_key(s.time)
        # Counter increments cover the interval since each counter's previous reading; they are
        # computed before the hour rolls over so the part of the interval that lies in the closing
        # hour (e.g. 23:55-00:00) is booked there, not to the next hour (or date).
        incs, gap_dropped, stale, new_baseline = self._counter_increments(s)
        # Completeness rule: a date's energy counts only if every mapped counter was observed
        # continuously from the date's start to its end. A counter starting a new baseline at this
        # sample (first reading, re-mapping, lost state) breaks the date it falls in, unless the sample
        # is exactly at local midnight, and also the previous date if we crossed midnight since the
        # last sample (that date's final interval was not measured).
        at_midnight = (s.time.hour, s.time.minute, s.time.second, s.time.microsecond) == (0, 0, 0, 0)
        old = self._acc
        if old is not None and key != old.key:
            to_old, incs = _split(incs, s.time, add_hours(old.key, 1.0))
            _add(old, to_old)
            old.counter_stale |= stale
            if new_baseline and old.start.date() != s.time.date():
                old.counter_restart = True
            done.append(self._finish(old))
            self._acc = None
        if self._acc is None:
            local = key.astimezone(s.time.tzinfo) if s.time.tzinfo is not None else key
            self._acc = _Acc(start=local, key=key)
        a = self._acc
        a.counter_restart |= new_baseline and not at_midnight
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
            values = [(role.value, s.get(role)) for role in MEAN_ROLES]
            for name, v in (*values, *(extras or {}).items()):
                if v is not None and math.isfinite(v):
                    a.sums[name] = a.sums.get(name, 0.0) + v * dt
                    a.weights[name] = a.weights.get(name, 0.0) + dt
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
    ) -> tuple[dict[str, tuple[float, datetime]], bool, bool, bool]:
        """Per counter: (increment, start of the interval it covers); whether an increment across a
        midnight gap was dropped; whether any counter could not be read (stale bridge or a mapped
        counter unavailable), so energy may still be outstanding; whether a counter started a new
        baseline here (no previous reading)."""
        incs: dict[str, tuple[float, datetime]] = {}
        dropped = new_baseline = False
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
            new_baseline |= prev is None
            inc = 0.0
            if prev is not None and gap and not same_day:
                dropped = True
            elif prev is not None:
                if v >= prev:
                    inc = v - prev
                elif v < prev - RESET_THRESHOLD_KWH:
                    # meter reset: the energy between the last reading and the reset is unknown, so it is
                    # a discontinuity like a new baseline (the date is not used for learning)
                    inc = v
                    new_baseline = True
                else:
                    v = prev  # small decrease: noise, keep the higher value
            self._last_counter[k] = v
            self.counter_time[k] = s.time
            incs[k] = (inc, seen if seen is not None else s.time)
        return incs, dropped, stale, new_baseline

    def to_dict(self) -> dict[str, Any]:
        """The in-progress hour, so a restart does not lose its samples and energy."""
        a = self._acc
        acc = None
        if a is not None:
            acc = {
                "start": a.start.isoformat(),
                "key": a.key.isoformat(),
                "covered_s": a.covered_s,
                # copies: HA serialises saved state later, in a worker thread, while polling continues
                "sums": dict(a.sums),
                "weights": dict(a.weights),
                "hz_min": _num_out(a.hz_min),
                "hz_max": _num_out(a.hz_max),
                "flow_min": _num_out(a.flow_min),
                "deltas": dict(a.deltas),
                "defrost": a.defrost,
                "dhw": a.dhw,
                "counter_gap": a.counter_gap,
                "counter_stale": a.counter_stale,
                "counter_restart": a.counter_restart,
            }
        return {"acc": acc, "last_time": self._last_time.isoformat() if self._last_time else None}

    def load_dict(self, d: dict[str, Any], tz: tzinfo | None = None) -> None:
        """Restore the in-progress hour (raises on invalid data; nothing is changed then).

        ``tz``: the installation's named zone; restored ISO timestamps only carry a fixed offset, which
        would lose 23/25-hour day lengths.
        """
        raw = d.get("acc")
        acc = None
        if raw is not None:
            start, key = datetime.fromisoformat(str(raw["start"])), datetime.fromisoformat(str(raw["key"]))
            if start.tzinfo is None or key.tzinfo is None:
                raise ValueError("naive time")
            if hour_key(start) != key or elapsed_s(key, start) != 0:
                raise ValueError("hour start and key disagree")
            if tz is not None:
                start = start.astimezone(tz)
            sums = {str(k): _num_in(v) for k, v in dict(raw["sums"]).items()}
            weights = {str(k): _num_in(v, 0.0, 3600.0) for k, v in dict(raw["weights"]).items()}
            if set(weights) != set(sums):
                raise ValueError("means: sums and weights disagree")
            hz_min, hz_max = (
                _num_in(raw.get("hz_min"), allow_nan=True),
                _num_in(raw.get("hz_max"), allow_nan=True),
            )
            flow_min = _num_in(raw.get("flow_min"), allow_nan=True)
            acc = _Acc(
                start=start,
                key=key,
                covered_s=_num_in(raw["covered_s"], 0.0, 3600.0),
                sums=sums,
                weights=weights,
                hz_min=hz_min if math.isfinite(hz_min) else math.inf,
                hz_max=hz_max if math.isfinite(hz_max) else -math.inf,
                flow_min=flow_min if math.isfinite(flow_min) else math.inf,
                deltas={str(k): _num_in(v, 0.0) for k, v in dict(raw["deltas"]).items()},
                defrost=bool(raw["defrost"]),
                dhw=bool(raw["dhw"]),
                counter_gap=bool(raw["counter_gap"]),
                counter_stale=bool(raw["counter_stale"]),
                counter_restart=bool(raw.get("counter_restart", False)),
            )
        lt = d.get("last_time")
        last_time = datetime.fromisoformat(str(lt)) if lt else None
        if last_time is not None and last_time.tzinfo is None:
            raise ValueError("naive time")
        if acc is not None and last_time is not None and not 0 <= elapsed_s(acc.key, last_time) < 3600:
            raise ValueError("last sample outside the in-progress hour")
        self._acc, self._last_time = acc, last_time

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
            a.counter_restart,
        )


class DayAggregator:
    """Collect hour records by local date; emits a DayRecord when the date changes."""

    MIN_HOURS = 22

    def __init__(self) -> None:
        self._day: str | None = None
        self._tz: object = None
        self._hours: list[HourRecord] = []
        # Without restored state the hours before the first one seen are unknown, so the first date
        # is never emitted (its energy total would be partial).
        self._first_partial = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self._day,
            "hours": [h.to_dict() for h in self._hours],
            "first_partial": self._first_partial,
        }

    def load_dict(self, d: dict[str, Any], tz: tzinfo | None = None) -> None:
        """Restore the current date's hours (raises on invalid data; nothing is changed then)."""
        day = d.get("day")
        hours = [HourRecord.from_dict(dict(x)) for x in d.get("hours") or []]
        if tz is not None:
            for h in hours:
                h.start = h.start.astimezone(tz)
        if day is not None:
            date.fromisoformat(str(day))
            if any(h.start.date().isoformat() != day for h in hours):
                raise ValueError("hour outside its date")
        self._day = None if day is None else str(day)
        self._hours = hours
        self._tz = tz  # restored offsets are fixed; the named zone comes from the caller or live hours
        # older state without the flag: assume the date may be incomplete
        self._first_partial = True if day is None else bool(d.get("first_partial", True))

    def add(self, h: HourRecord) -> DayRecord | None:
        day = h.start.date().isoformat()
        out = None
        # live hours carry the installation's named zone (needed for 23/25-hour day lengths)
        self._tz = h.start.tzinfo
        if self._day is not None and day != self._day:
            # The closing date's total is only known to be complete if the counters were readable at the
            # boundary: a stale bridge at the end of the old date or the start of the new one (or a gap
            # reaching into this hour) means energy may still be outstanding.
            last = self._hours[-1] if self._hours else None
            boundary_bad = h.counter_gap or h.counter_stale or (last is not None and last.counter_stale)
            out = None if boundary_bad or self._first_partial else self._finish()
            self._first_partial = False
            self._hours = []
        if self._day is None:
            # first hour without restored state: the date can only be complete if it is the midnight
            # hour (counters starting later in it are caught by counter_restart)
            self._first_partial = (h.start.hour, h.start.minute) != (0, 0)
        self._day = day
        self._hours.append(h)
        return out

    def _finish(self) -> DayRecord | None:
        hs = [h for h in self._hours if h.coverage >= 0.75 and h.cls != IntervalClass.INVALID.value]
        if self._day is None or any(h.counter_gap or h.counter_restart for h in self._hours):
            return None
        length_h = day_length_h(date.fromisoformat(self._day), self._tz)
        if len(hs) < self.MIN_HOURS + (length_h - 24):  # same allowance for missing hours on 23/25 h days
            return None
        ti = [h.means["ti"] for h in hs if "ti" in h.means]
        to = [h.means["to"] for h in hs if "to" in h.means]
        if len(ti) < self.MIN_HOURS + (length_h - 24) or len(to) < self.MIN_HOURS + (length_h - 24):
            return None
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
            extra_gains_w=(
                sum(g) / len(g) if (g := [h.means["gains_w"] for h in hs if "gains_w" in h.means]) else 0.0
            ),
            length_h=length_h,
        )


def hours_between(a: datetime, b: datetime) -> float:
    return (b - a) / timedelta(hours=1)
