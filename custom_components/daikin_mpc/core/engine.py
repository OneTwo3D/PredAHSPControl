"""Shadow engine: telemetry → intervals → learning → forecast → error tracking.

Pure Python; the HA coordinator calls :meth:`ShadowEngine.process` every few minutes with a validated
snapshot and an hourly outdoor-temperature forecast. The engine never issues commands.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from .accumulator import DayAggregator, DayRecord, HourAccumulator, HourRecord
from .cop_learner import CopLearner
from .cost_model import CostProvider, TariffPeriod, period_value
from .emitter_model import RadiatorParams
from .heatpump_model import CopCurve
from .learner import BuildingLearner, LearnerConfig
from .optimiser import OptimiserConfig, PlanInputs, Recommendation, recommend
from .predictor import Forecast, PlantParams, forecast
from .telemetry import Role, ValidatedSnapshot
from .thermal_model import ThermalParams
from .timeutil import add_hours, elapsed_h, elapsed_s, hour_key

ENGINE_STATE_VERSION = 1
HORIZONS_H = (1, 3, 6)
FORECAST_HOURS = 24
STEP_H = 0.25


@dataclass(frozen=True)
class EngineConfig:
    """Priors and plant description (defaults: offline fit of winter 2025/26)."""

    ua_w_per_k: float = 94.0
    gains_w: float = 440.0
    c_wh_per_k: float = 3000.0
    prior_sd: tuple[float, float, float] = (10.0, 80.0, 1500.0)
    radiator_k: float = 63.1
    radiator_n: float = 1.3
    q_min_w: float = 680.0
    q_max_w: float = 4300.0
    # COP prior: Daikin heating heat counter / (external meter − Daikin DHW electricity − standby), so it
    # includes the circulation pump but not standby — the same basis the live COP learner uses.
    # Standby is added once, separately (winter 2025/26 daily data).
    cop_centres_c: tuple[float, ...] = (1.4, 4.7, 7.6, 10.3, 13.1)
    cop_values: tuple[float, ...] = (3.06, 3.45, 3.68, 3.99, 4.82)
    # native weather-dependent curve: (outdoor °C, LWT °C) points, linear between, clamped outside
    wc_curve: tuple[tuple[float, float], ...] = ((-23.0, 47.0), (10.0, 25.0))
    min_lwt_c: float = 25.0
    standby_w: float = 19.0  # external meter, compressor off (winter 2025/26)
    learning_enabled: bool = True


@dataclass
class ErrorStats:
    n: int = 0
    abs_sum: float = 0.0
    sum: float = 0.0

    def add(self, err: float) -> None:
        self.n += 1
        self.abs_sum += abs(err)
        self.sum += err

    @property
    def mae(self) -> float | None:
        return self.abs_sum / self.n if self.n else None

    @property
    def bias(self) -> float | None:
        return self.sum / self.n if self.n else None


@dataclass
class EngineStatus:
    time: datetime
    telemetry_ok: bool
    issues: dict[str, str]
    interval_class: str | None
    forecast: Forecast | None
    forecast_source: str
    ua: float
    gains: float
    c_kwh: float
    sd: dict[str, float]
    learner_updates: int
    learner_rejected: int
    learner_last_reason: str
    hour_class_counts: dict[str, int]
    errors: dict[str, dict[str, float | int | None]]
    cop_now: float | None
    lwt_required_c: float | None
    last_day: dict[str, Any] | None = None
    heating_enabled: bool | None = None
    standby_w: float = 0.0
    cop_source: str = "prior"
    cop_learned_days: int = 0
    recommendation: Recommendation | None = None
    cost_source: str = ""


@dataclass
class _Pending:
    name: str
    due: datetime
    predicted: float


@dataclass
class ShadowEngine:
    cfg: EngineConfig = field(default_factory=EngineConfig)

    def __post_init__(self) -> None:
        c = self.cfg
        self.learner = BuildingLearner(
            ThermalParams(c.ua_w_per_k, c.c_wh_per_k, c.gains_w), c.prior_sd, LearnerConfig()
        )
        self.plant = PlantParams(
            RadiatorParams(c.radiator_k, c.radiator_n), c.q_min_w, c.q_max_w, min_lwt_c=c.min_lwt_c
        )
        self.cop_prior = CopCurve(tuple(c.cop_centres_c), tuple(c.cop_values), (), ())
        self.cop_learner = CopLearner(self.cop_prior)
        self.cop_curve = self.cop_prior
        self._cop_bins_learned = 0
        self.hours = HourAccumulator()
        self.days = DayAggregator()
        self.pending_day: DayRecord | None = None
        self.class_counts: Counter[str] = Counter()
        self.errors: dict[str, ErrorStats] = {}
        self.pending: list[_Pending] = []
        self.setpoint_profile: dict[int, float] = {}  # hour of day -> learned room setpoint
        self.lwt_offset_k = 0.0  # observed requested LWT minus WC curve (RT modulation, deviation)
        self.last_hour: HourRecord | None = None
        self.last_day: DayRecord | None = None
        self.last_forecast: Forecast | None = None
        self._last_forecast_hour: datetime | None = None

    # --- helpers ---------------------------------------------------------------------------
    def cop(self, to_c: float) -> float:
        return self.cop_curve.at(to_c)[0]

    def wc_lwt(self, to_c: float) -> float:
        xs = [p[0] for p in self.cfg.wc_curve]
        ys = [p[1] for p in self.cfg.wc_curve]
        return max(float(np.interp(to_c, xs, ys)), self.cfg.min_lwt_c)

    def _record_external(self, name: str, issue: datetime, horizon_h: float, value: float) -> None:
        self.pending.append(_Pending(name, add_hours(issue, horizon_h), value))

    # --- main entry ------------------------------------------------------------------------
    def process(
        self,
        s: ValidatedSnapshot,
        to_forecast: list[tuple[datetime, float]] | None,
        external_predictions: dict[str, tuple[float, float]] | None = None,
    ) -> EngineStatus:
        """Process one snapshot.

        ``to_forecast``: hourly (time, °C) outdoor forecast, or None.
        ``external_predictions``: other forecasters to score, ``name -> (horizon_h, predicted Ti)``,
        e.g. Predheat's 1 h and 8 h room-temperature forecasts. Issued once per hour.
        """
        t = s.time
        ti, to = s.get(Role.TI), s.get(Role.TO)
        heating_enabled = s.get(Role.HEATING_ENABLED)
        hz = s.get(Role.HZ)

        # 1. aggregation and learning
        for h in self.hours.add(s):
            self.last_hour = h
            self.class_counts[h.cls] += 1
            day = self.days.add(h)
            if day is not None:
                self._on_day(day)

        # 2. learn the setpoint schedule and the LWT offset from the native curve
        sp = s.get(Role.ROOM_SET)
        if sp is not None:
            hod = t.hour
            prev = self.setpoint_profile.get(hod)
            self.setpoint_profile[hod] = sp if prev is None else 0.8 * prev + 0.2 * sp
        lwt_set = s.get(Role.LWT_SET)
        if (
            s.complete
            and lwt_set is not None
            and to is not None
            and hz is not None
            and hz > 0
            and not s.get(Role.DHW_ACTIVE)
            and not s.get(Role.DEFROST)
        ):
            # space heating only: DHW and defrost request very different LWTs
            # remove the modelled RT modulation so the offset captures curve shift / deviation only
            mod = 0.0
            if ti is not None and sp is not None:
                mod = max(
                    -self.plant.rt_modulation_max_k,
                    min(self.plant.rt_modulation_max_k, self.plant.rt_modulation_gain_k_per_k * (sp - ti)),
                )
            self.lwt_offset_k = 0.95 * self.lwt_offset_k + 0.05 * (lwt_set - mod - self.wc_lwt(to))

        # 3. score matured predictions
        if ti is not None:
            keep = []
            for p in self.pending:
                late_s = elapsed_s(p.due, t)
                if late_s >= 0:
                    if late_s <= 15 * 60:
                        self.errors.setdefault(p.name, ErrorStats()).add(p.predicted - ti)
                else:
                    keep.append(p)
            self.pending = keep[-200:]

        # 4. forecast (every step) and issue tracked predictions once per hour
        fc, source = None, "none"
        if s.complete and ti is not None and to is not None:
            to_steps, source = self._to_steps(t, to, to_forecast)
            sps = [self._setpoint_at(add_hours(t, STEP_H * i), sp) for i in range(len(to_steps))]
            lwts = [self.wc_lwt(x) + self.lwt_offset_k for x in to_steps]
            fc = forecast(
                ti,
                self._calling(s, hz),
                to_steps,
                sps,
                lwts,
                bool(heating_enabled if heating_enabled is not None else True),
                self.learner.params,
                self.plant,
                self.cop,
                STEP_H,
            )
            self.last_forecast = fc
            hour = hour_key(t)
            if self._last_forecast_hour != hour:
                self._last_forecast_hour = hour
                for hz_h in HORIZONS_H:
                    self._record_external(f"mpc_{hz_h}h", t, hz_h, fc.ti_at(hz_h))
                for name, (h_h, val) in (external_predictions or {}).items():
                    if math.isfinite(val):
                        self._record_external(name, t, h_h, val)

        lwt_req = None
        if ti is not None and to is not None:
            bp = self.learner.params
            q_need = max(0.0, bp.ua_w_per_k * (ti - to) - bp.gains_w)
            lwt_req = max(
                self.plant.radiator.required_mwt_c(q_need, ti) + self.plant.flow_return_dt_k / 2,
                self.cfg.min_lwt_c,
            )

        sd = self.learner.sd
        return EngineStatus(
            time=t,
            telemetry_ok=s.complete,
            issues={k.value: v for k, v in s.issues.items()},
            interval_class=self.last_hour.cls if self.last_hour else None,
            forecast=fc,
            forecast_source=source,
            ua=self.learner.params.ua_w_per_k,
            gains=self.learner.params.gains_w,
            c_kwh=self.learner.params.c_wh_per_k / 1000,
            sd={"ua": sd["ua"], "gains": sd["gains"], "c_kwh": sd["c"] / 1000},
            learner_updates=self.learner.updates,
            learner_rejected=self.learner.rejected,
            learner_last_reason=self.learner.last_reason,
            hour_class_counts=dict(self.class_counts),
            errors={k: {"n": v.n, "mae": v.mae, "bias": v.bias} for k, v in sorted(self.errors.items())},
            cop_now=self.cop(to) if to is not None else None,
            lwt_required_c=lwt_req,
            last_day=self.last_day.to_dict() if self.last_day else None,
            heating_enabled=bool(heating_enabled) if heating_enabled is not None else None,
            standby_w=self.standby_w,
            cop_source=f"learned ({self._cop_bins_learned} bins)" if self._cop_bins_learned else "prior",
            cop_learned_days=self.cop_learner.days,
        )

    @property
    def standby_w(self) -> float:
        sb = self.cop_learner.standby_w
        return sb if sb is not None else self.cfg.standby_w

    def _on_day(self, day: DayRecord) -> None:
        # The energy balance needs the next day's mean temperature (ΔTi), so update one day late.
        prev, self.pending_day, self.last_day = self.pending_day, day, day
        if not self.cfg.learning_enabled:
            return
        # ΔTi is only meaningful between consecutive dates; a gap (rejected day, outage) breaks the pair
        if prev is not None and date.fromisoformat(day.day) - date.fromisoformat(prev.day) == timedelta(
            days=1
        ):
            self.learner.update_day(prev.ti, prev.to, prev.heat_kwh, day.ti - prev.ti, prev.length_h)
        if day.heating_ext_kwh is not None and self.cop_learner.update_day(
            day.to, day.heat_kwh, day.heating_ext_kwh, day.standby_w, day.length_h, self.cfg.standby_w
        ):
            self.cop_curve, self._cop_bins_learned = self.cop_learner.curve()
        elif day.standby_w is not None:
            self.cop_learner.update_day(day.to, 0.0, 0.0, day.standby_w)  # standby only

    @staticmethod
    def _calling(s: ValidatedSnapshot, hz: float | None) -> bool:
        """Thermostat state at the start of the forecast: demand signal if mapped, else compressor."""
        demand = s.get(Role.HEATING_DEMAND)
        if demand is not None:
            return demand > 0
        return bool(hz and hz > 0)

    def _setpoint_at(self, when: datetime, current: float | None) -> float:
        v = self.setpoint_profile.get(when.hour)
        if v is not None:
            return round(v * 2) / 2  # Daikin setpoints are in 0.5 K steps
        return current if current is not None else 20.0

    def recommend(
        self,
        s: ValidatedSnapshot,
        to_forecast: list[tuple[datetime, float]] | None,
        cost: CostProvider,
        cfg: OptimiserConfig,
        comfort_periods: tuple[TariffPeriod, ...] = (),
    ) -> Recommendation | None:
        """Shadow recommendation: cheapest hourly setpoint plan within the comfort range.

        Pure computation on the current learned models; it never changes the engine's state and never
        issues commands. Returns None when telemetry is incomplete.
        """
        ti, to = s.get(Role.TI), s.get(Role.TO)
        if not s.complete or ti is None or to is None:
            return None
        t = s.time
        to_steps, _ = self._to_steps(t, to, to_forecast)
        per_h = round(1 / STEP_H)
        hours = min(cfg.horizon_h, len(to_steps) // per_h)
        if hours < 1:
            return None
        sp_now = s.get(Role.ROOM_SET)
        baseline = [self._setpoint_at(add_hours(t, h), sp_now) for h in range(hours)]
        enabled = s.get(Role.HEATING_ENABLED)
        inp = PlanInputs(
            start=t,
            ti0_c=ti,
            running0=self._calling(s, s.get(Role.HZ)),
            current_setpoint_c=sp_now if sp_now is not None else baseline[0],
            heating_enabled=bool(enabled if enabled is not None else True),
            to_c=to_steps,
            lwt_set_c=[self.wc_lwt(x) + self.lwt_offset_k for x in to_steps],
            rate_p=[cost.marginal_rate(add_hours(t, STEP_H * (i + 0.5))) for i in range(len(to_steps))],
            baseline_setpoint_c=baseline,
            target_c=comfort_targets(t, len(to_steps), STEP_H, comfort_periods),
        )
        return recommend(inp, self.learner.params, self.plant, self.cop, cfg)

    @staticmethod
    def _to_steps(
        t: datetime, to_now: float, fc: list[tuple[datetime, float]] | None
    ) -> tuple[list[float], str]:
        n = int(FORECAST_HOURS / STEP_H)
        q = [STEP_H * (i + 0.5) for i in range(n)]  # elapsed hours from now
        # only future, finite, plausible points in strictly increasing order; past points would break
        # the interpolation and override the measured value now
        pts: dict[float, float] = {}
        for ft, v in fc or ():
            x = elapsed_h(t, ft)
            if x > 0 and math.isfinite(v) and -40.0 <= v <= 50.0:
                pts[x] = float(v)
        if not pts:
            return [to_now] * n, "persistence"
        xs = sorted(pts)
        # anchor at the measured value now so the first steps are continuous
        return [float(v) for v in np.interp(q, [0.0, *xs], [to_now, *(pts[x] for x in xs)])], "weather"

    # --- persistence -----------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": ENGINE_STATE_VERSION,
            "learner": self.learner.to_dict(),
            "class_counts": dict(self.class_counts),
            "errors": {k: [v.n, v.abs_sum, v.sum] for k, v in self.errors.items()},
            "setpoint_profile": {str(k): v for k, v in self.setpoint_profile.items()},
            "lwt_offset_k": self.lwt_offset_k,
            "pending_day": self.pending_day.to_dict() if self.pending_day else None,
            "last_counters": dict(self.hours._last_counter),
            "counter_time": {k: v.isoformat() for k, v in self.hours.counter_time.items()},
            "cop_learner": self.cop_learner.to_dict(),
        }

    def load_dict(self, d: dict[str, Any]) -> list[str]:
        """Restore persisted state. Invalid parts are skipped; returns a list of warnings."""
        warn: list[str] = []
        if not isinstance(d, dict) or d.get("version") != ENGINE_STATE_VERSION:
            return ["unsupported or missing state version; starting from priors"]
        # Each section is parsed completely before it is applied; a corrupt section keeps its priors and
        # never prevents start-up.
        try:
            ok = self.learner.load_dict(d.get("learner") or {})
        except Exception:
            ok = False
        if not ok:
            warn.append("learner state invalid; using priors")
        sections: list[tuple[str, Any]] = [
            ("class counts", lambda: self._load_counts(d)),
            ("error statistics", lambda: self._load_errors(d)),
            ("setpoint profile", lambda: self._load_profile(d)),
            ("pending day", lambda: self._load_pending_day(d)),
            ("meter counters", lambda: self._load_counters(d)),
        ]
        for name, fn in sections:
            try:
                fn()
            except Exception as e:
                warn.append(f"{name} ignored: {e!r}")
        if "cop_learner" in d:
            try:
                ok = self.cop_learner.load_dict(d["cop_learner"])
            except Exception:
                ok = False
            if ok:
                self.cop_curve, self._cop_bins_learned = self.cop_learner.curve()
            else:
                self.cop_learner = CopLearner(self.cop_prior)
                warn.append("COP learner state invalid or from an older version; using prior curve")
        return warn

    def _load_counts(self, d: dict[str, Any]) -> None:
        counts = Counter({str(k): int(v) for k, v in dict(d.get("class_counts") or {}).items()})
        if any(v < 0 for v in counts.values()):
            raise ValueError("negative count")
        self.class_counts = counts

    def _load_errors(self, d: dict[str, Any]) -> None:
        errors: dict[str, ErrorStats] = {}
        for k, v in dict(d.get("errors") or {}).items():
            n, a, b = int(v[0]), float(v[1]), float(v[2])
            if n < 0 or not (math.isfinite(a) and math.isfinite(b)):
                raise ValueError(f"invalid error statistics for {k}")
            errors[str(k)] = ErrorStats(n, a, b)
        self.errors = errors

    def _load_profile(self, d: dict[str, Any]) -> None:
        prof = {int(k): float(v) for k, v in dict(d.get("setpoint_profile") or {}).items()}
        off = float(d.get("lwt_offset_k", 0.0))
        self.setpoint_profile = {k: v for k, v in prof.items() if 0 <= k < 24 and 5 <= v <= 35}
        self.lwt_offset_k = off if math.isfinite(off) and abs(off) <= 15 else 0.0

    def _load_pending_day(self, d: dict[str, Any]) -> None:
        pdict = d.get("pending_day")
        if not pdict:
            self.pending_day = None
            return
        raw = dict(pdict)

        def num(key: str, lo: float, hi: float, default: float | None = None) -> float:
            v = float(raw[key]) if key in raw else default
            if v is None or not (math.isfinite(v) and lo <= v <= hi):
                raise ValueError(f"pending day {key} invalid")
            return v

        def opt(key: str, lo: float, hi: float) -> float | None:
            return None if raw.get(key) is None else num(key, lo, hi)

        # normalised and typed, so a stored string can never reach the learner's arithmetic
        rec = DayRecord(
            day=date.fromisoformat(str(raw["day"])).isoformat(),
            hours=int(num("hours", 0, 25)),
            ti=num("ti", 5, 35),
            to=num("to", -40, 50),
            heat_kwh=num("heat_kwh", 0, 500),
            elec_kwh=num("elec_kwh", 0, 500),
            dhw_hours=int(num("dhw_hours", 0, 25)),
            defrost_hours=int(num("defrost_hours", 0, 25)),
            ext_kwh=opt("ext_kwh", 0, 500),
            heating_ext_kwh=opt("heating_ext_kwh", 0, 500),
            standby_w=opt("standby_w", 0, 200),
            length_h=num("length_h", 23, 25, 24.0),
        )
        self.pending_day = rec

    def _load_counters(self, d: dict[str, Any]) -> None:
        last = {str(k): float(v) for k, v in dict(d.get("last_counters") or {}).items()}
        times = {str(k): datetime.fromisoformat(str(v)) for k, v in dict(d.get("counter_time") or {}).items()}
        if not all(math.isfinite(v) for v in last.values()) or any(t.tzinfo is None for t in times.values()):
            raise ValueError("invalid counter baselines")
        self.hours._last_counter, self.hours.counter_time = last, times


def comfort_targets(
    start: datetime, steps: int, step_h: float, periods: tuple[TariffPeriod, ...]
) -> list[float | None]:
    """Soft comfort target per step (local time) from ``"HH:MM-HH:MM=°C"`` periods."""
    out: list[float | None] = []
    for i in range(steps):
        t = add_hours(start, step_h * (i + 1))  # temperature at the end of the step
        out.append(period_value(periods, t.hour * 60 + t.minute))
    return out
