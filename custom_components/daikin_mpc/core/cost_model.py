"""Electricity price per time slot.

Two bases:

* ``tariff``: the raw import tariff (pence/kWh) for each time.
* ``battery``: battery- and export-aware approximation. An extra kWh used outside the cheap window is
  normally supplied by the battery (charged at the cheap rate) or by surplus PV, so its marginal cost is
  not the peak import rate (plan §22) but the larger of

  - the storage cost: cheapest import rate / round-trip efficiency, and
  - the opportunity cost: the export rate the same energy could have earned,

  capped at the import rate (importing is always possible)::

      marginal = min(import, max(cheapest_import / efficiency, export))

  Assumption (confirmed by the owner for the reference installation): the heat pump can always run
  from the battery, or directly from the grid in the cheap night slots, i.e. battery capacity and
  power never limit it. On a day when the battery does run out the import rate would apply; M4 can
  refine this with Predbat's plan.

Rates come from Predbat (``predbat.rates`` / ``predbat.rates_export`` ``results`` attributes: change
points, each holding until the next, including events such as Axle/saving sessions); a fixed tariff is
only the fallback outside Predbat's horizon or while Predbat is not running. The battery round-trip
efficiency is derived from Predbat's loss settings (:func:`efficiency_from_predbat`).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .timeutil import add_hours, elapsed_s

DEFAULT_TARIFF = "00:00-05:00=7.6, 05:00-24:00=34.87"
# 12p fixed export with Predbat's -10p night override in apps.yaml (00:00-05:00)
DEFAULT_EXPORT_TARIFF = "00:00-05:00=2.0, 05:00-24:00=12.0"


@dataclass(frozen=True)
class TariffPeriod:
    start_min: int  # minutes after local midnight, inclusive
    end_min: int  # exclusive, 1440 = midnight
    rate_p: float


def _minutes(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")
    v = int(h) * 60 + int(m)
    if not 0 <= v <= 1440:
        raise ValueError(f"time out of range: {hhmm}")
    return v


def parse_periods(text: str) -> tuple[TariffPeriod, ...]:
    """Parse ``"HH:MM-HH:MM=value, ..."`` into periods (may wrap midnight; gaps allowed)."""
    periods: list[TariffPeriod] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        span, value = part.split("=")
        a, b = span.split("-")
        start, end, v = _minutes(a), _minutes(b), float(value)
        if not math.isfinite(v):
            raise ValueError("value must be finite")
        if end <= start:
            periods += [TariffPeriod(start, 1440, v), TariffPeriod(0, end, v)]
        else:
            periods.append(TariffPeriod(start, end, v))
    return tuple(periods)


def period_value(periods: tuple[TariffPeriod, ...], minute_of_day: int) -> float | None:
    for p in periods:
        if p.start_min <= minute_of_day < p.end_min:
            return p.rate_p
    return None


def parse_tariff(text: str) -> tuple[TariffPeriod, ...]:
    """Parse ``"HH:MM-HH:MM=rate, ..."``; periods may wrap midnight. Must cover the whole day."""
    periods: list[TariffPeriod] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        span, rate = part.split("=")
        a, b = span.split("-")
        start, end, r = _minutes(a), _minutes(b), float(rate)
        if not math.isfinite(r):
            raise ValueError("rate must be finite")
        if end <= start:  # wraps midnight
            periods += [TariffPeriod(start, 1440, r), TariffPeriod(0, end, r)]
        else:
            periods.append(TariffPeriod(start, end, r))
    covered = sorted((p.start_min, p.end_min) for p in periods)
    pos = 0
    for lo_min, hi_min in covered:
        if lo_min > pos:
            raise ValueError(f"tariff does not cover {pos // 60:02d}:{pos % 60:02d}")
        pos = max(pos, hi_min)
    if pos < 1440:
        raise ValueError("tariff does not cover the whole day")
    return tuple(periods)


@dataclass
class CostProvider:
    """Marginal electricity cost in pence/kWh for a local time."""

    tariff: tuple[TariffPeriod, ...]
    export_tariff: tuple[TariffPeriod, ...] | None = None
    basis: str = "battery"  # "battery" or "tariff"
    round_trip_efficiency: float = 0.9
    series: list[tuple[datetime, float]] | None = None  # import (slot start, rate), sorted
    export_series: list[tuple[datetime, float]] | None = None
    series_slot: timedelta = timedelta(minutes=30)

    def __post_init__(self) -> None:
        if self.basis not in ("battery", "tariff"):
            raise ValueError("basis must be 'battery' or 'tariff'")
        self._times = [x for x, _ in self.series] if self.series else []
        self._export_times = [x for x, _ in self.export_series] if self.export_series else []

    def _lookup(
        self,
        t: datetime,
        series: list[tuple[datetime, float]] | None,
        fixed: tuple[TariffPeriod, ...],
        times: list[datetime],
    ) -> float:
        if series:
            # Predbat publishes change points: each value holds until the next point; the last point is
            # the start of the final slot of its horizon.
            i = bisect.bisect_right(times, t) - 1
            if i >= 0 and (i < len(series) - 1 or t - series[i][0] < self.series_slot):
                return series[i][1]
        m = t.hour * 60 + t.minute
        for p in fixed:
            if p.start_min <= m < p.end_min:
                return p.rate_p
        return max(p.rate_p for p in fixed)

    def tariff_rate(self, t: datetime) -> float:
        """Raw import rate at ``t`` (Predbat series first, fixed tariff otherwise)."""
        return self._lookup(t, self.series, self.tariff, self._times)

    def export_rate(self, t: datetime) -> float:
        """Export rate at ``t``; 0 when no export tariff is known."""
        if self.export_tariff is None and not self.export_series:
            return 0.0
        return self._lookup(
            t, self.export_series, self.export_tariff or (TariffPeriod(0, 1440, 0.0),), self._export_times
        )

    def charge_rate(self, t: datetime) -> float:
        """Cheapest import rate in force at any time in the 24 h before ``t`` (when the battery was charged).

        Evaluated at the window start and at every rate change inside it, so short slots are never
        missed. Each instant uses the rate that applies then: Predbat's where its series covers it, the
        fixed tariff otherwise, so an expired series cannot set the price and the fallback's cheap rate is
        not used where Predbat's is known. Window membership is decided in elapsed (UTC) time, and local
        tariff boundaries are taken in both folds of a repeated autumn hour and skipped when they do not
        exist (spring gap).
        """
        start = add_hours(t, -24.0)

        def inside(x: datetime) -> bool:
            return elapsed_s(start, x) >= 0 and elapsed_s(x, t) > 0

        instants = [start]
        if self.series:
            instants += [x for x in self._times if inside(x)]
            end = add_hours(self._times[-1], self.series_slot.total_seconds() / 3600)
            if inside(end):
                instants.append(end)  # the fixed tariff takes over where the series ends
        tz = t.tzinfo
        if tz is not None:
            # just after each UTC-offset change (a spring change can cut a cheap period short without
            # removing it, e.g. 01:30-02:30 keeps 02:00-02:30); changes fall on 15-minute UTC boundaries
            u = start.astimezone(UTC)
            u = u.replace(minute=u.minute - u.minute % 15, second=0, microsecond=0)
            prev = u.astimezone(tz).utcoffset()
            while elapsed_s(u, t) > 0:
                u += timedelta(minutes=15)
                off = u.astimezone(tz).utcoffset()
                if off != prev and inside(u.astimezone(tz)):
                    instants.append(u.astimezone(tz))
                prev = off
        d = start.date() - timedelta(days=1)
        while d <= t.date():
            for p in self.tariff:
                if p.start_min >= 1440:
                    continue
                hh, mm = divmod(p.start_min, 60)
                for fold in (0, 1):
                    x = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz, fold=fold)
                    if tz is not None:
                        back = x.astimezone(UTC).astimezone(tz)
                        if (back.hour, back.minute) != (hh, mm):
                            continue  # does not exist (clocks went forward)
                        x = back
                    if inside(x):
                        instants.append(x)
            d += timedelta(days=1)
        return min(self.tariff_rate(x) for x in instants)

    def marginal_rate(self, t: datetime) -> float:
        raw = self.tariff_rate(t)
        if self.basis == "tariff":
            return raw
        storage = self.charge_rate(t) / self.round_trip_efficiency
        return min(raw, max(storage, self.export_rate(t)))

    @property
    def source(self) -> str:
        base = "Predbat rates" if self.series else "fixed tariff"
        return f"{base}, {'battery/export-aware' if self.basis == 'battery' else 'raw import tariff'}"


def parse_rate_series(results: object) -> list[tuple[datetime, float]] | None:
    """Convert Predbat's ``results`` mapping (ISO time -> rate) into a sorted series.

    Anything that is not such a mapping (external data) gives None, i.e. the fixed tariff is used.
    """
    if not isinstance(results, dict) or not results:
        return None
    out: list[tuple[datetime, float]] = []
    for k, v in results.items():
        try:
            t = datetime.fromisoformat(str(k))
            r = float(v)
        except (TypeError, ValueError):
            continue
        if t.tzinfo is not None and math.isfinite(r):
            out.append((t, r))
    return sorted(out) or None


def efficiency_from_predbat(
    battery_loss: float, battery_loss_discharge: float, inverter_loss: float
) -> float:
    """Round-trip efficiency grid → battery → house from Predbat's loss fractions.

    Charging passes the inverter and the battery charge loss; discharging the battery discharge loss and
    the inverter again.
    """
    eff = (1 - battery_loss) * (1 - battery_loss_discharge) * (1 - inverter_loss) ** 2
    if not 0.5 <= eff <= 1.0:
        raise ValueError(f"implausible round-trip efficiency {eff:.3f}")
    return eff
