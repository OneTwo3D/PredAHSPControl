"""Daikin MPC: self-learning supervisory controller for Daikin Altherma (shadow mode).

This version observes, learns and forecasts only. It contains no code path that writes to the
heat pump or any other device.

Home Assistant imports are deferred so that ``custom_components.daikin_mpc.core`` stays importable
(and testable) without Home Assistant installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import DaikinMpcCoordinator

    type DaikinMpcConfigEntry = ConfigEntry[DaikinMpcCoordinator]

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: DaikinMpcConfigEntry) -> bool:
    """Set up Daikin MPC from a config entry."""
    from .coordinator import DaikinMpcCoordinator

    coordinator = DaikinMpcCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DaikinMpcConfigEntry) -> bool:
    """Unload a config entry, saving learned state first."""
    await entry.runtime_data.async_save_now()
    return bool(await hass.config_entries.async_unload_platforms(entry, PLATFORMS))
