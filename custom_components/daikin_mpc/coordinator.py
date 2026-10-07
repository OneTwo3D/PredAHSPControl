"""Coordinator: reads HA states, runs the pure-Python shadow engine, persists learned state.

Read-only towards devices: the only service it calls is ``weather.get_forecasts`` (a read action).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_C,
    CONF_GAINS,
    CONF_LEARNING,
    CONF_PREDHEAT_H1,
    CONF_PREDHEAT_H8,
    CONF_UA,
    CONF_WEATHER,
    DEFAULT_PRIORS,
    DOMAIN,
    SAVE_DELAY_S,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
    WEATHER_REFRESH,
)
from .core.engine import EngineConfig, EngineStatus, ShadowEngine
from .core.telemetry import BINARY_ROLES, Reading, Role, Snapshot, validate

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


class DaikinMpcCoordinator(DataUpdateCoordinator[EngineStatus]):
    """Polls mapped entities every few minutes and feeds the shadow engine."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=UPDATE_INTERVAL)
        opts = {**DEFAULT_PRIORS, **entry.options}
        self.engine = ShadowEngine(
            EngineConfig(
                ua_w_per_k=float(opts[CONF_UA]),
                gains_w=float(opts[CONF_GAINS]),
                c_wh_per_k=float(opts[CONF_C]) * 1000.0,
                learning_enabled=bool(opts.get(CONF_LEARNING, True)),
            )
        )
        self.mapping: dict[str, str] = dict(entry.data)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._weather: list[tuple[datetime, float]] | None = None
        self._weather_fetched: datetime | None = None
        self.weather_error: str | None = None
        self.load_warnings: list[str] = []

    async def _async_setup(self) -> None:
        stored = await self._store.async_load()
        if stored:
            self.load_warnings = self.engine.load_dict(stored)
            for w in self.load_warnings:
                _LOGGER.warning("Daikin MPC state: %s", w)

    async def async_save_now(self) -> None:
        await self._store.async_save(self.engine.to_dict())

    # ------------------------------------------------------------------------------------------
    def _reading(self, entity_id: str | None, binary: bool, heartbeat: bool = False) -> Reading | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return Reading(None)
        last = getattr(st, "last_reported", None) or st.last_updated
        age = (dt_util.utcnow() - last).total_seconds()
        if heartbeat:
            return Reading(1.0, age)
        if binary:
            return Reading(1.0 if st.state == STATE_ON else 0.0, age)
        try:
            return Reading(float(st.state), age)
        except ValueError:
            return Reading(None, age)

    def _snapshot(self, now: datetime) -> Snapshot:
        readings: dict[Role, Reading] = {}
        for role in Role:
            rd = self._reading(self.mapping.get(role.value), role in BINARY_ROLES, role is Role.HEARTBEAT)
            if rd is not None:
                readings[role] = rd
        return Snapshot(now, readings)

    async def _async_weather(self, now: datetime) -> None:
        entity = self.mapping.get(CONF_WEATHER)
        if not entity:
            return
        if self._weather_fetched and now - self._weather_fetched < WEATHER_REFRESH:
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
            items = (resp or {}).get(entity, {}).get("forecast", [])  # type: ignore[union-attr]
            fc: list[tuple[datetime, float]] = []
            for it in items:
                t = dt_util.parse_datetime(str(it.get("datetime")))
                temp = it.get("temperature")
                if t is not None and temp is not None:
                    fc.append((dt_util.as_local(t), float(temp)))
            self._weather = sorted(fc) or None
            self._weather_fetched = now
            self.weather_error = None if fc else "empty forecast"
        except (HomeAssistantError, ValueError, TypeError) as err:
            self.weather_error = str(err)
            # keep the last forecast while it still covers the future; the engine falls back to
            # persistence when it is missing
            if self._weather and self._weather[-1][0] < now:
                self._weather = None

    def _external(self) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for key, name, horizon in (
            (CONF_PREDHEAT_H1, "predheat_1h", 1.0),
            (CONF_PREDHEAT_H8, "predheat_8h", 8.0),
        ):
            rd = self._reading(self.mapping.get(key), False)
            if rd is not None and rd.value is not None:
                out[name] = (horizon, rd.value)
        return out

    async def _async_update_data(self) -> EngineStatus:
        now = dt_util.now()
        await self._async_weather(now)
        snap = validate(self._snapshot(now))
        try:
            status = self.engine.process(snap, self._weather, self._external())
        except Exception as err:  # never let a model error take HA down; surface it instead
            raise UpdateFailed(f"engine error: {err}") from err
        self._store.async_delay_save(self.engine.to_dict, SAVE_DELAY_S)
        return status
