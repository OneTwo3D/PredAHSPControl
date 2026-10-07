"""Config and options flow: map telemetry entities, set model priors.

No writable entity is requested here: this version is observation-only.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers import selector

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
    SUGGESTED,
)
from .core.telemetry import REQUIRED_ROLES, Role

_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_BINARY = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=["binary_sensor", "switch", "input_boolean"])
)
_WEATHER = selector.EntitySelector(selector.EntitySelectorConfig(domain="weather"))
_ANY = selector.EntitySelector(selector.EntitySelectorConfig())

_BINARY_KEYS = {Role.DEFROST.value, Role.DHW_ACTIVE.value, Role.HEATING_ENABLED.value}


def _schema() -> vol.Schema:
    fields: dict[Any, Any] = {}
    required = {r.value for r in REQUIRED_ROLES}
    for role in Role:
        key = role.value
        marker = vol.Required if key in required else vol.Optional
        fields[marker(key)] = (
            _BINARY if key in _BINARY_KEYS else _ANY if key == Role.HEARTBEAT.value else _SENSOR
        )
    fields[vol.Optional(CONF_WEATHER)] = _WEATHER
    fields[vol.Optional(CONF_PREDHEAT_H1)] = _ANY
    fields[vol.Optional(CONF_PREDHEAT_H8)] = _ANY
    return vol.Schema(fields)


class DaikinMpcConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            for key, entity_id in user_input.items():
                if entity_id and self.hass.states.get(entity_id) is None:
                    errors[key] = "entity_not_found"
            if not errors:
                return self.async_create_entry(title="Daikin MPC", data=user_input)
        suggested = {k: v for k, v in SUGGESTED.items() if self.hass.states.get(v) is not None}
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_schema(), user_input or suggested),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> DaikinMpcOptionsFlow:
        return DaikinMpcOptionsFlow()


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LEARNING): selector.BooleanSelector(),
        vol.Required(CONF_UA): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=20, max=500, step=1, unit_of_measurement="W/K", mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(CONF_GAINS): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-500, max=2000, step=10, unit_of_measurement="W", mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(CONF_C): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.5, max=30, step=0.1, unit_of_measurement="kWh/K", mode=selector.NumberSelectorMode.BOX
            )
        ),
    }
)


class DaikinMpcOptionsFlow(OptionsFlowWithReload):
    """Priors only apply until learned state exists (reset by removing the integration)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {CONF_LEARNING: True, **DEFAULT_PRIORS, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, current)
        )
