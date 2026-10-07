"""Sensors exposing the shadow engine's state, learned parameters and forecasts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower, UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DaikinMpcCoordinator
from .core.engine import EngineStatus
from .core.timeutil import add_hours

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import DaikinMpcConfigEntry


@dataclass(frozen=True, kw_only=True)
class MpcSensorDescription(SensorEntityDescription):
    value_fn: Callable[[EngineStatus], Any]
    attrs_fn: Callable[[EngineStatus], dict[str, Any]] | None = None


def _ti(h: int) -> Callable[[EngineStatus], float | None]:
    return lambda s: round(s.forecast.ti_at(h), 2) if s.forecast else None


def _err(name: str) -> Callable[[EngineStatus], float | None]:
    def f(s: EngineStatus) -> float | None:
        mae = s.errors.get(name, {}).get("mae")
        return round(float(mae), 3) if mae is not None else None

    return f


def _err_attrs(*names: str) -> Callable[[EngineStatus], dict[str, Any]]:
    return lambda s: {n: s.errors.get(n) for n in names}


def _energy_attrs(s: EngineStatus) -> dict[str, Any]:
    if not s.forecast:
        return {}
    fc = s.forecast
    per_hour = round(1 / fc.step_h)
    start = s.time.replace(second=0, microsecond=0)
    sb_kwh_h = s.standby_w / 1000
    hourly = [
        {
            "start": add_hours(start, k).isoformat(),
            "elec_kwh": round(sum(fc.elec_wh[i : i + per_hour]) / 1000 + sb_kwh_h, 3),
            "heat_kwh": round(sum(fc.heat_wh[i : i + per_hour]) / 1000, 3),
        }
        for k, i in enumerate(range(0, len(fc.elec_wh), per_hour))
    ]
    heat, elec = fc.energy_kwh(24)
    return {
        "heat_kwh_24h": round(heat, 2),
        "space_heating_kwh_24h": round(elec, 3),
        "standby_w": round(s.standby_w, 1),
        "standby_kwh_24h": round(sb_kwh_h * 24, 3),
        "cop_source": s.cop_source,
        "source": s.forecast_source,
        "hourly": hourly,
    }


def _plan_attrs(s: EngineStatus) -> dict[str, Any]:
    r = s.recommendation
    if r is None:
        return {"cost_source": s.cost_source}
    per_h = 4
    # times are relative to when the plan was made, not to the latest poll
    start = (r.start or s.time).replace(second=0, microsecond=0)
    hourly = [
        {
            "start": add_hours(start, h).isoformat(),
            "setpoint": sp,
            "baseline_setpoint": r.baseline.setpoints_c[h] if h < len(r.baseline.setpoints_c) else None,
            "room_c": round(r.plan.ti_c[min((h + 1) * per_h, len(r.plan.ti_c) - 1)], 2),
            "elec_kwh": round(sum(r.plan.elec_kwh[h * per_h : (h + 1) * per_h]), 3),
        }
        for h, sp in enumerate(r.plan.setpoints_c)
    ]
    return {
        "reason": r.reason,
        "schedule": r.details.get("blocks"),
        "feasible": r.plan.feasible,
        "room_min_c": round(r.plan.min_ti_c, 2),
        "room_max_c": round(r.plan.max_ti_c, 2),
        "cost_source": s.cost_source,
        "mode": "shadow (not applied)",
        "plan": hourly,
    }


def _saving_attrs(s: EngineStatus) -> dict[str, Any]:
    r = s.recommendation
    if r is None:
        return {}
    return {
        "plan_cost_p": round(r.plan.cost_p, 1),
        "baseline_cost_p": round(r.baseline.cost_p, 1),
        "plan_kwh": round(r.plan.energy_kwh(), 2),
        "baseline_kwh": round(r.baseline.energy_kwh(), 2),
        "plan_compressor_starts": r.plan.starts,
        "baseline_compressor_starts": r.baseline.starts,
        "baseline_room_min_c": r.details.get("baseline_min_c"),
        "note": "model estimate for the next 24 h versus the current schedule; not measured savings",
    }


def _total_elec(s: EngineStatus) -> float | None:
    if not s.forecast:
        return None
    return round(s.forecast.energy_kwh(24)[1] + s.standby_w * 24 / 1000, 3)


SENSORS: tuple[MpcSensorDescription, ...] = (
    MpcSensorDescription(
        key="recommended_setpoint",
        name="Recommended room setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.recommendation.setpoint_now_c if s.recommendation else None,
        attrs_fn=_plan_attrs,
    ),
    MpcSensorDescription(
        key="expected_saving_24h",
        name="Expected saving 24h",
        native_unit_of_measurement="p",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: round(s.recommendation.saving_p, 1) if s.recommendation else None,
        attrs_fn=_saving_attrs,
    ),
    MpcSensorDescription(
        key="recommendation_reason",
        name="Recommendation",
        value_fn=lambda s: s.recommendation.reason[:255] if s.recommendation else None,
    ),
    MpcSensorDescription(
        key="status",
        name="Status",
        value_fn=lambda s: "ok" if s.telemetry_ok else "telemetry_incomplete",
        attrs_fn=lambda s: {"issues": s.issues, "mode": "shadow", "heating_enabled": s.heating_enabled},
    ),
    *(
        MpcSensorDescription(
            key=f"temperature_{h}h",
            name=f"Predicted room temperature {h}h",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=_ti(h),
        )
        for h in (1, 3, 6)
    ),
    MpcSensorDescription(
        key="energy_24h",
        name="Predicted heat pump electricity 24h",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_total_elec,
        attrs_fn=_energy_attrs,
    ),
    MpcSensorDescription(
        key="heat_loss_coefficient",
        name="Heat loss coefficient",
        native_unit_of_measurement="W/K",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: round(s.ua, 2),
        attrs_fn=lambda s: {"std_dev": round(s.sd["ua"], 2)},
    ),
    MpcSensorDescription(
        key="thermal_capacity",
        name="Thermal capacity",
        native_unit_of_measurement="kWh/K",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: round(s.c_kwh, 3),
        attrs_fn=lambda s: {"std_dev": round(s.sd["c_kwh"], 3)},
    ),
    MpcSensorDescription(
        key="internal_gain",
        name="Internal gains",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: round(s.gains),
        attrs_fn=lambda s: {"std_dev": round(s.sd["gains"], 1)},
    ),
    MpcSensorDescription(
        key="estimated_cop",
        name="Estimated COP",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: round(s.cop_now, 2) if s.cop_now is not None else None,
        attrs_fn=lambda s: {
            "source": s.cop_source,
            "learned_days": s.cop_learned_days,
            "basis": "Daikin heat counter / (external meter − DHW − standby); standby added separately",
        },
    ),
    MpcSensorDescription(
        key="standby_power",
        name="Standby power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda s: round(s.standby_w, 1),
    ),
    MpcSensorDescription(
        key="required_flow_temperature",
        name="Required flow temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: round(s.lwt_required_c, 1) if s.lwt_required_c is not None else None,
        attrs_fn=lambda s: {"note": "steady-state LWT to hold the current room temperature; floor 25 °C"},
    ),
    *(
        MpcSensorDescription(
            key=f"prediction_error_{h}h",
            name=f"Prediction error {h}h",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            suggested_display_precision=2,
            value_fn=_err(f"mpc_{h}h"),
            attrs_fn=_err_attrs(
                f"mpc_{h}h", *(("predheat_1h",) if h == 1 else ("predheat_8h",) if h == 6 else ())
            ),
        )
        for h in (1, 3, 6)
    ),
    MpcSensorDescription(
        key="training_days",
        name="Training days",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.learner_updates,
        attrs_fn=lambda s: {
            "rejected_days": s.learner_rejected,
            "last_result": s.learner_last_reason,
            "hours_by_class": s.hour_class_counts,
            "last_day": s.last_day,
        },
    ),
    MpcSensorDescription(
        key="interval_class",
        name="Last hour class",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.interval_class,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: DaikinMpcConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(MpcSensor(coordinator, d) for d in SENSORS)


class MpcSensor(CoordinatorEntity[DaikinMpcCoordinator], SensorEntity):
    _attr_has_entity_name = True
    # Large or fast-changing attributes are kept out of the recorder database.
    _unrecorded_attributes = frozenset(
        {"hourly", "last_day", "hours_by_class", "issues", "plan", "schedule", "reason"}
    )
    entity_description: MpcSensorDescription

    def __init__(self, coordinator: DaikinMpcCoordinator, description: MpcSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry_id = coordinator.config_entry.entry_id if coordinator.config_entry else "default"
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Daikin MPC",
            manufacturer="PredAHSPControl",
            model="Shadow controller",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data) if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn and self.coordinator.data else None
