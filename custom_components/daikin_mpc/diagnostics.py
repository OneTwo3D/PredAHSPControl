"""Diagnostics download: mappings, learned state and latest status (no credentials involved)."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import DaikinMpcConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DaikinMpcConfigEntry
) -> dict[str, Any]:
    coord = entry.runtime_data
    status = coord.data
    st: dict[str, Any] | None = None
    if status is not None:
        st = asdict(status)
        st["forecast"] = (
            None
            if status.forecast is None
            else {
                "ti_1h": status.forecast.ti_at(1),
                "ti_6h": status.forecast.ti_at(6),
                "energy_24h_kwh": status.forecast.energy_kwh(24),
            }
        )
        st["time"] = status.time.isoformat()
    return {
        "mapping": dict(entry.data),
        "options": dict(entry.options),
        "engine_state": coord.engine.to_dict(),
        "load_warnings": coord.load_warnings,
        "weather_error": coord.weather_error,
        "status": st,
    }
