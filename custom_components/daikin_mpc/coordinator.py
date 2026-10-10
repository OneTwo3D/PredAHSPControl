"""Coordinator: reads HA states, runs the pure-Python shadow engine, persists learned state.

Read-only towards devices: the only service it calls is ``weather.get_forecasts`` (a read action).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_C,
    CONF_COMFORT_PERIODS,
    CONF_COST_BASIS,
    CONF_GAIN_BATTERY,
    CONF_GAIN_HOUSEHOLD,
    CONF_GAIN_SOLAR,
    CONF_GAIN_TANK,
    CONF_GAINS,
    CONF_LEARNING,
    CONF_PREDHEAT_H1,
    CONF_PREDHEAT_H8,
    CONF_ROOM_MAX,
    CONF_ROOM_MIN,
    CONF_SOLCAST,
    CONF_TARIFF_EXPORT,
    CONF_TARIFF_IMPORT,
    CONF_UA,
    CONF_WEATHER,
    DEFAULT_GAINS,
    DEFAULT_OPTIMISER,
    DEFAULT_PRIORS,
    DOMAIN,
    OPTIMISE_INTERVAL,
    PREDBAT_LOSSES,
    PREDBAT_RATES,
    PREDBAT_RATES_EXPORT,
    SAVE_DELAY_S,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
    WEATHER_REFRESH,
)
from .core.cost_model import (
    CostProvider,
    efficiency_from_predbat,
    parse_periods,
    parse_rate_series,
    parse_tariff,
)
from .core.engine import EngineConfig, EngineStatus, ShadowEngine
from .core.gains import GainsConfig, parse_solcast
from .core.optimiser import OptimiserConfig, Recommendation
from .core.telemetry import BINARY_ROLES, COUNTER_ROLES, Reading, Role, Snapshot, validate
from .core.timeutil import elapsed_s
from .core.units import temperature_c, to_core_unit

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)
EXTERNAL_MAX_AGE_S = 30 * 60


class DaikinMpcCoordinator(DataUpdateCoordinator[EngineStatus]):
    """Polls mapped entities every few minutes and feeds the shadow engine."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=UPDATE_INTERVAL)
        opts = {**DEFAULT_PRIORS, **DEFAULT_OPTIMISER, **DEFAULT_GAINS, **entry.options}
        self.engine = ShadowEngine(
            EngineConfig(
                ua_w_per_k=float(opts[CONF_UA]),
                gains_w=float(opts[CONF_GAINS]),
                c_wh_per_k=float(opts[CONF_C]) * 1000.0,
                learning_enabled=bool(opts.get(CONF_LEARNING, True)),
                gains=GainsConfig(
                    household_factor=float(opts[CONF_GAIN_HOUSEHOLD]),
                    solar_factor=float(opts[CONF_GAIN_SOLAR]),
                    battery_loss_fraction=float(opts[CONF_GAIN_BATTERY]),
                    tank_ua_w_per_k=float(opts[CONF_GAIN_TANK]),
                ),
            )
        )
        self.mapping: dict[str, str] = dict(entry.data)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._weather: list[tuple[datetime, float]] | None = None
        self._weather_fetched: datetime | None = None
        self.weather_error: str | None = None
        self.load_warnings: list[str] = []
        self.opt_cfg = OptimiserConfig(
            room_min_c=float(opts[CONF_ROOM_MIN]),
            room_max_c=float(opts[CONF_ROOM_MAX]),
            setpoints=_setpoint_grid(float(opts[CONF_ROOM_MIN]), float(opts[CONF_ROOM_MAX])),
        )
        self.cost_basis = str(opts[CONF_COST_BASIS])
        self.fallback_import = parse_tariff(str(opts[CONF_TARIFF_IMPORT]))
        self.fallback_export = parse_tariff(str(opts[CONF_TARIFF_EXPORT]))
        self.comfort_periods = parse_periods(str(opts[CONF_COMFORT_PERIODS]))
        self.recommendation: Recommendation | None = None
        self.cost_source = ""
        self.optimiser_error: str | None = None
        self._last_optimise: datetime | None = None

    async def _async_setup(self) -> None:
        stored = await self._store.async_load()
        if stored:
            stored = self._drop_remapped_counters(dict(stored))
            self.load_warnings = self.engine.load_dict(stored)
            for w in self.load_warnings:
                _LOGGER.warning("Daikin MPC state: %s", w)

    def _state(self) -> dict[str, Any]:
        """Engine state plus the entity behind each counter, to detect remapping on the next load."""
        return {**self.engine.to_dict(), "counter_entities": self._counter_entities()}

    def _counter_entities(self) -> dict[str, str]:
        return {r.value: self.mapping[r.value] for r in COUNTER_ROLES if self.mapping.get(r.value)}

    def _drop_remapped_counters(self, stored: dict[str, Any]) -> dict[str, Any]:
        """Forget counter baselines whose role now points to another entity (or whose entity is unknown):
        comparing two different meters would book their difference as energy."""
        old = stored.get("counter_entities")
        old = old if isinstance(old, dict) else {}
        now = self._counter_entities()
        keep = {k for k, e in now.items() if old.get(k) == e}
        for key in ("last_counters", "counter_time"):
            v = stored.get(key)
            if isinstance(v, dict):
                stored[key] = {k: x for k, x in v.items() if k in keep}
        return stored

    async def async_save_now(self) -> None:
        await self._store.async_save(self._state())

    # ------------------------------------------------------------------------------------------
    def _reading(self, entity_id: str | None, role: Role | None) -> Reading | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return Reading(None)
        last = getattr(st, "last_reported", None) or st.last_updated
        age = (dt_util.utcnow() - last).total_seconds()
        if role is Role.HEARTBEAT:
            return Reading(1.0, age)
        if role in BINARY_ROLES:
            return Reading(1.0 if st.state == STATE_ON else 0.0, age)
        try:
            value = float(st.state)
        except ValueError:
            return Reading(None, age)
        if role is None:
            return Reading(value, age)
        try:
            return Reading(to_core_unit(role, value, st.attributes.get("unit_of_measurement")), age)
        except ValueError as err:
            return Reading(None, age, str(err))

    def _snapshot(self, now: datetime) -> Snapshot:
        readings: dict[Role, Reading] = {}
        for role in Role:
            rd = self._reading(self.mapping.get(role.value), role)
            if rd is not None:
                readings[role] = rd
        return Snapshot(now, readings)

    async def _async_weather(self, now: datetime) -> None:
        entity = self.mapping.get(CONF_WEATHER)
        if not entity:
            return
        if self._weather_fetched and elapsed_s(self._weather_fetched, now) < WEATHER_REFRESH.total_seconds():
            return
        try:
            resp = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                target={"entity_id": entity},
                blocking=True,
                return_response=True,
            )
            st = self.hass.states.get(entity)
            unit = st.attributes.get("temperature_unit") if st is not None else None
            fc = _parse_forecast(resp, entity, unit)
            self._weather = sorted(fc) or None
            self._weather_fetched = now
            self.weather_error = None if fc else "empty forecast"
        except Exception as err:
            self.weather_error = str(err) or type(err).__name__
            # keep the last forecast while it still covers the future; the engine falls back to
            # persistence when it is missing
            if self._weather and self._weather[-1][0] < now:
                self._weather = None

    def _solcast(self) -> list[tuple[datetime, float]] | None:
        """Solcast PV forecast (period start, W) from the configured forecast entities' attributes."""
        ids = [e.strip() for e in str(self.mapping.get(CONF_SOLCAST) or "").split(",") if e.strip()]
        lists = []
        for eid in ids:
            st = self.hass.states.get(eid)
            if st is not None:
                lists.append(st.attributes.get("detailedForecast"))
        return parse_solcast(lists) or None

    def _external(self) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for key, name, horizon in (
            (CONF_PREDHEAT_H1, "predheat_1h", 1.0),
            (CONF_PREDHEAT_H8, "predheat_8h", 8.0),
        ):
            rd = self._reading(self.mapping.get(key), Role.TI)  # a room temperature: same units/range
            # Predheat refreshes every few minutes; an old value means it is disabled or stopped, and
            # scoring that stale forecast would distort the comparison.
            if (
                rd is not None
                and rd.value is not None
                and math.isfinite(rd.value)
                and 5.0 <= rd.value <= 35.0
                and rd.age_s <= EXTERNAL_MAX_AGE_S
            ):
                out[name] = (horizon, rd.value)
        return out

    async def _async_update_data(self) -> EngineStatus:
        now = dt_util.now()
        await self._async_weather(now)
        snap = validate(self._snapshot(now))
        try:
            status = self.engine.process(snap, self._weather, self._external(), self._solcast())
        except Exception as err:  # never let a model error take HA down; surface it instead
            raise UpdateFailed(f"engine error: {err}") from err
        await self._async_optimise(now, snap, status.telemetry_ok)
        status.recommendation = self.recommendation
        status.cost_source = self.cost_source
        self._store.async_delay_save(self._state, SAVE_DELAY_S)
        return status

    def _cost_provider(self) -> CostProvider:
        """Prices from Predbat when available; the configured tariff only as fallback."""

        def series(entity_id: str) -> list[tuple[datetime, float]] | None:
            st = self.hass.states.get(entity_id)
            return parse_rate_series(st.attributes.get("results")) if st is not None else None

        eff = 0.9
        try:
            losses = [float(self.hass.states.get(e).state) for e in PREDBAT_LOSSES]  # type: ignore[union-attr]
            eff = efficiency_from_predbat(*losses)
        except (AttributeError, TypeError, ValueError):
            pass  # Predbat not running or settings missing: nominal efficiency
        return CostProvider(
            tariff=self.fallback_import,
            export_tariff=self.fallback_export,
            basis=self.cost_basis,
            round_trip_efficiency=eff,
            series=series(PREDBAT_RATES),
            export_series=series(PREDBAT_RATES_EXPORT),
        )

    async def _async_optimise(self, now: datetime, snap: Any, telemetry_ok: bool) -> None:
        if not telemetry_ok:
            # never show a plan built on data that is no longer valid; retry on the next poll
            self.recommendation = None
            self._last_optimise = None
            return
        if (
            self.recommendation is not None
            and self._last_optimise is not None
            and elapsed_s(self._last_optimise, now) < OPTIMISE_INTERVAL.total_seconds()
        ):
            return
        self._last_optimise = now
        try:
            cost = self._cost_provider()  # Predbat attributes are external data: keep inside the guard
            self.cost_source = cost.source
            self.recommendation = await self.hass.async_add_executor_job(
                self.engine.recommend, snap, self._weather, cost, self.opt_cfg, self.comfort_periods
            )
            self.optimiser_error = None
        except Exception as err:  # the optimiser is advisory; never break telemetry/learning
            _LOGGER.warning("Daikin MPC optimiser failed: %s", err)
            self.optimiser_error = str(err)
            self.recommendation = None


def _setpoint_grid(lo: float, hi: float) -> tuple[float, ...]:
    """Daikin setpoints in 0.5 K steps within [lo, hi]."""
    start = round(lo * 2 + 0.499) / 2
    out: list[float] = []
    v = start
    while v <= hi + 1e-9:
        out.append(round(v, 1))
        v += 0.5
    return tuple(out) or (round(lo * 2) / 2,)


def _parse_forecast(resp: object, entity: str, unit: str | None) -> list[tuple[datetime, float]]:
    """Hourly (local time, °C) points from a ``weather.get_forecasts`` response; malformed items skipped."""
    body = resp.get(entity) if isinstance(resp, dict) else None
    items = body.get("forecast") if isinstance(body, dict) else None
    fc: list[tuple[datetime, float]] = []
    for it in items if isinstance(items, list) else ():
        if not isinstance(it, dict):
            continue
        t = dt_util.parse_datetime(str(it.get("datetime")))
        temp = it.get("temperature")
        if t is None or temp is None:
            continue
        try:
            v = temperature_c(float(temp), unit)
        except (TypeError, ValueError):
            continue
        # NaN or implausible values would propagate through the forecast and the optimiser
        if math.isfinite(v) and -40.0 <= v <= 50.0:
            fc.append((dt_util.as_local(t), v))
    return sorted(fc)
