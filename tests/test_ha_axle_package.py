"""HA package docs/ha/packages/ashp_axle.yaml: the heat pump's response to Axle grid events.

The decision logic lives in one template sensor (sensor.ashp_axle_decision) so it can be tested here
without Home Assistant: its Jinja template is rendered with stand-ins for HA's template helpers.
The automations only act on that sensor's state; their structure is checked below.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

PKG = Path(__file__).resolve().parents[1] / "docs" / "ha" / "packages" / "ashp_axle.yaml"
NOW = datetime(2026, 11, 26, 19, 30, tzinfo=UTC)


def _pkg() -> dict[str, Any]:
    return yaml.safe_load(PKG.read_text())


def _template_sensor(unique_id: str) -> dict[str, Any]:
    for block in _pkg()["template"]:
        for s in block.get("sensor", []):
            if s.get("unique_id") == unique_id:
                return s
    raise KeyError(unique_id)


def _render(template: str, states: dict[str, Any], now: datetime = NOW) -> str:
    def _st(eid: str) -> Any:
        v = states.get(eid, "unknown")
        return v[0] if isinstance(v, tuple) else v

    def _attr(eid: str, name: str) -> Any:
        v = states.get(eid)
        return v[1].get(name) if isinstance(v, tuple) else None

    def as_timestamp(value: Any, default: Any = None) -> Any:
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except (TypeError, ValueError):
            return default

    env = jinja2.Environment(extensions=["jinja2.ext.loopcontrols"])
    env.globals.update(
        states=lambda e: str(_st(e)),
        is_state=lambda e, v: str(_st(e)) == v,
        state_attr=_attr,
        as_timestamp=as_timestamp,
        now=lambda: now,
    )
    return env.from_string(template).render().strip()


def _base(**over: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "input_boolean.ashp_axle_response": "on",
        "input_boolean.ashp_axle_simulate": "off",
        "input_select.ashp_axle_simulate_type": "export",
        "binary_sensor.predbat_axle_event": ("off", {"event_current": []}),
        "input_text.ashp_axle_active": "",
        "input_datetime.ashp_axle_started": "2026-11-26 18:59:00",  # previous response: before this event
        "sensor.bridge0_sensors_temperature_room": "20.2",
        "binary_sensor.bridge0_mode_defrost_active": "off",
        "binary_sensor.bridge0_dhw_dhw_demand": "off",
        "sensor.bridge0_dhw_temperature_r5t_dhw_tank": "41.0",
        "switch.bridge0_mode_altherma_on": "on",
        "climate.bridge0_dhw_dhw_setpoint": ("heat", {"temperature": 45.0}),
        "switch.bridge0_dhw_dhw_boost": "off",
    }
    s.update(over)
    return s


def _event(kind: str, on: bool = True, pence: float = 100.0) -> tuple[str, dict[str, Any]]:
    ev = {
        "start_time": "2026-11-26T19:00:00+0000",
        "end_time": "2026-11-26T20:00:00+0000",
        "import_export": kind,
        "pence_per_kwh": pence,
    }
    return ("on" if on else "off", {"event_current": [ev]})


def decide(**over: Any) -> str:
    return _render(_template_sensor("ashp_axle_decision")["state"], _base(**over))


# --- idle ---------------------------------------------------------------------------------------


def test_idle_without_event_or_when_disabled():
    assert decide() == "idle"
    assert (
        decide(**{"binary_sensor.predbat_axle_event": _event("export", on=False)}) == "idle"
    )  # announced only
    assert (
        decide(
            **{
                "binary_sensor.predbat_axle_event": _event("export"),
                "input_boolean.ashp_axle_response": "off",
            }
        )
        == "idle"
    )


def test_unknown_event_type_is_ignored():
    assert decide(**{"binary_sensor.predbat_axle_event": _event("both")}) == "idle"


# --- export -------------------------------------------------------------------------------------


def test_export_event_starts_export():
    assert decide(**{"binary_sensor.predbat_axle_event": _event("export")}) == "start_export"


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"sensor.bridge0_sensors_temperature_room": "18.4"}, "skip:room"),
        ({"sensor.bridge0_sensors_temperature_room": "unavailable"}, "skip:room"),
        ({"binary_sensor.bridge0_mode_defrost_active": "on"}, "wait:defrost"),
        ({"binary_sensor.bridge0_dhw_dhw_demand": "on"}, "wait:dhw_run"),
    ],
)
def test_export_guards(over, expected):
    assert decide(**{"binary_sensor.predbat_axle_event": _event("export"), **over}) == expected


def test_active_export_holds_then_ends():
    active = {"input_text.ashp_axle_active": "export", "binary_sensor.predbat_axle_event": _event("export")}
    assert decide(**active) == "hold"
    assert decide(**{**active, "binary_sensor.predbat_axle_event": _event("export", on=False)}) == "end"
    assert decide(**{**active, "sensor.bridge0_sensors_temperature_room": "17.9"}) == "end"
    assert decide(**{**active, "input_boolean.ashp_axle_response": "off"}) == "end"  # manual override
    assert decide(**{**active, "input_datetime.ashp_axle_started": "2026-11-26 16:20:00"}) == "end"  # > 3 h


def test_an_event_already_handled_is_not_started_again():
    # 4 h event: the 3 h cap ended the response at 22:00; the event is still on -> no restart
    ev = (
        "on",
        {
            "event_current": [
                {
                    "start_time": "2026-11-26T19:00:00+0000",
                    "end_time": "2026-11-26T23:00:00+0000",
                    "import_export": "export",
                    "pence_per_kwh": 100.0,
                }
            ]
        },
    )
    over = {"binary_sensor.predbat_axle_event": ev, "input_datetime.ashp_axle_started": "2026-11-26 19:00:30"}
    assert decide(**over) == "skip:done"
    # a new event after an older response does start
    assert decide(**{**over, "input_datetime.ashp_axle_started": "2026-11-25 19:00:30"}) == "start_export"


def test_active_export_ends_if_the_event_turns_into_import():
    assert (
        decide(
            **{"input_text.ashp_axle_active": "export", "binary_sensor.predbat_axle_event": _event("import")}
        )
        == "end"
    )


# --- import -------------------------------------------------------------------------------------


def test_import_event_starts_dhw_heat_up():
    assert decide(**{"binary_sensor.predbat_axle_event": _event("import")}) == "start_import"


def test_import_skipped_when_tank_already_near_target():
    ev = {"binary_sensor.predbat_axle_event": _event("import")}
    assert decide(**ev, **{"sensor.bridge0_dhw_temperature_r5t_dhw_tank": "53.2"}) == "skip:tank"
    assert decide(**ev, **{"sensor.bridge0_dhw_temperature_r5t_dhw_tank": "unavailable"}) == "skip:tank"
    assert decide(**ev, **{"binary_sensor.bridge0_mode_defrost_active": "on"}) == "wait:defrost"


def test_active_import_holds_until_tank_target_or_event_end():
    active = {"input_text.ashp_axle_active": "import", "binary_sensor.predbat_axle_event": _event("import")}
    assert decide(**active) == "hold"
    assert decide(**{**active, "sensor.bridge0_dhw_temperature_r5t_dhw_tank": "54.6"}) == "end"
    assert decide(**{**active, "binary_sensor.predbat_axle_event": _event("import", on=False)}) == "end"
    assert decide(**{**active, "input_boolean.ashp_axle_response": "off"}) == "end"


# --- simulation and restart -----------------------------------------------------------------------


def test_simulation_drives_the_same_logic():
    sim = {"input_boolean.ashp_axle_simulate": "on"}
    assert decide(**sim) == "start_export"
    assert decide(**sim, **{"input_select.ashp_axle_simulate_type": "import"}) == "start_import"
    # a simulated event ending restores like a real one
    assert decide(**{"input_text.ashp_axle_active": "import"}) == "end"


@pytest.mark.parametrize("raw", ["unknown", "unavailable", "", "idle"])
def test_unset_active_marker_counts_as_idle(raw):
    assert decide(**{"input_text.ashp_axle_active": raw}) == "idle"


def test_after_a_restart_mid_event_the_persisted_marker_still_ends_it():
    # HA restarted after the event finished: the restored marker says export, no event any more -> restore
    assert decide(**{"input_text.ashp_axle_active": "export"}) == "end"


# --- automation structure -----------------------------------------------------------------------


def _automation(alias_part: str) -> dict[str, Any]:
    for a in _pkg()["automation"]:
        if alias_part in a["alias"]:
            return a
    raise KeyError(alias_part)


def _dump(obj: Any) -> str:
    return json.dumps(obj)


def test_start_and_end_share_one_queued_automation():
    a = _automation("Axle event start/end")
    assert a["mode"] == "queued"
    triggers = _dump(a["triggers"])
    assert "sensor.ashp_axle_decision" in triggers and "start_export" in triggers and '"end"' in triggers
    assert "time_pattern" in triggers  # periodic retry of an unconfirmed restore
    branches = a["actions"][0]["choose"]
    assert "script.ashp_axle_" in _dump(branches[0]["sequence"])
    assert _dump(branches[1]["sequence"]) == '[{"action": "script.ashp_axle_restore"}]'
    assert not any(
        x["alias"] in ("ASHP Axle event start", "ASHP Axle event end") for x in _pkg()["automation"]
    )


def test_start_retries_stop_when_the_event_ends():
    for name in ("ashp_axle_start_export", "ashp_axle_start_import"):
        loop = next(step["repeat"] for step in _pkg()["script"][name]["sequence"] if "repeat" in step)
        assert "is_state('sensor.ashp_axle_decision', 'end')" in loop["until"]


@pytest.mark.parametrize("kind", ["export", "import"])
@pytest.mark.parametrize(
    "eid",
    ["switch.bridge0_mode_altherma_on", "climate.bridge0_dhw_dhw_setpoint", "switch.bridge0_dhw_dhw_boost"],
)
def test_waits_for_valid_bridge_states_before_saving_them(kind, eid):
    over = {"binary_sensor.predbat_axle_event": _event(kind), eid: "unavailable"}
    assert decide(**over) == "wait:bridge"


@pytest.mark.parametrize(
    "eid", ["binary_sensor.bridge0_mode_defrost_active", "binary_sensor.bridge0_dhw_dhw_demand"]
)
@pytest.mark.parametrize("raw", ["unknown", "unavailable"])
def test_export_waits_when_a_guard_sensor_is_not_known(eid, raw):
    over = {"binary_sensor.predbat_axle_event": _event("export"), eid: raw}
    assert decide(**over).startswith("wait:")


@pytest.mark.parametrize("kind", ["export", "import"])
@pytest.mark.parametrize("attrs", [{}, {"temperature": None}, {"temperature": 0}, {"temperature": 75}])
def test_waits_for_a_valid_dhw_target_before_saving_it(kind, attrs):
    over = {
        "binary_sensor.predbat_axle_event": _event(kind),
        "climate.bridge0_dhw_dhw_setpoint": ("heat", attrs),
    }
    assert decide(**over) == "wait:bridge"


def test_restore_falls_back_to_heating_on_for_an_invalid_saved_state():
    v = _pkg()["script"]["ashp_axle_restore"]["sequence"][0]["variables"]["want_on"]
    tpl = "{% set saved = {'on': 'unavailable'} %}{% set mode = 'export' %}" + v
    assert _render(tpl, _base()) == "on"
    tpl = "{% set saved = {'on': 'off'} %}{% set mode = 'export' %}" + v
    assert _render(tpl, _base()) == "off"


def test_starts_record_state_first_and_set_the_marker_last():
    mark = _dump(_pkg()["script"]["ashp_axle_mark_start"]["sequence"])
    assert mark.index("input_text.ashp_axle_saved") < mark.rindex("input_text.ashp_axle_active")
    for name in ("ashp_axle_start_export", "ashp_axle_start_import"):
        seq = _pkg()["script"][name]["sequence"]
        assert seq[0]["action"] == "script.ashp_axle_mark_start"  # before any heat-pump write


def test_export_start_switches_space_heating_and_hot_water_off():
    text = _dump(_pkg()["script"]["ashp_axle_start_export"]["sequence"])
    assert "switch.turn_off" in text and "switch.bridge0_mode_altherma_on" in text
    assert '"hvac_mode": "off"' in text and "climate.bridge0_dhw_dhw_setpoint" in text


def test_import_start_raises_dhw_target_to_55_and_boosts():
    text = _dump(_pkg()["script"]["ashp_axle_start_import"]["sequence"])
    assert '"temperature": 55' in text and "switch.bridge0_dhw_dhw_boost" in text
    assert '"hvac_mode": "heat"' in text  # hot water switched on if it was off


def _seq(name: str) -> list[dict[str, Any]]:
    return _pkg()["script"][name]["sequence"]


def test_starts_verify_writes_and_roll_back_on_failure():
    for name, expect in (
        ("ashp_axle_start_export", "is_state('switch.bridge0_mode_altherma_on', 'off')"),
        ("ashp_axle_start_import", "is_state('switch.bridge0_dhw_dhw_boost', 'on')"),
    ):
        seq = _seq(name)
        loop = next(step["repeat"] for step in seq if "repeat" in step)
        assert "repeat.index >= 2" in loop["until"]  # at most two attempts
        waits = [st for st in loop["sequence"] if "wait_template" in st]
        assert waits and expect in waits[0]["wait_template"] and waits[0]["continue_on_timeout"] is True
        writes = [st for st in _dump(loop["sequence"]).split('"action"')[1:]]
        assert all('"continue_on_error": true' in w for w in writes)  # a failed write never aborts the script
        rollback = _dump(seq[-1])
        assert "not wait.completed" in rollback and "script.ashp_axle_restore" in rollback


def test_restore_clears_marker_before_reporting_and_only_when_confirmed():
    seq = _seq("ashp_axle_restore")
    text = _dump(seq)
    clear = text.index('{"value": ""}')
    assert text.index("wait_template") < clear  # restoration confirmed first
    assert clear < text.index("logbook.log") and clear < text.rindex("notify.mobile_app_jan_mobile")
    # unconfirmed restore: notify, keep the marker (stop before clearing) so the 5-minute tick retries
    unconfirmed = next(st for st in seq if "if" in st and "not wait.completed" in _dump(st["if"]))
    assert "stop" in _dump(unconfirmed["then"])
    # reporting cannot fail the restore
    for st in seq:
        if st.get("action") in ("logbook.log", "notify.mobile_app_jan_mobile"):
            assert st.get("continue_on_error") is True


def test_restore_puts_boost_back_to_its_saved_state():
    variables = _seq("ashp_axle_restore")[0]["variables"]
    assert "saved.boost" in variables["want_boost"]
    text = _dump(_seq("ashp_axle_restore"))
    assert "switch.turn_{{ want_boost }}" in text
    assert "switch.turn_off" not in text  # no unconditional boost-off any more


def test_restore_targets_come_from_saved_state():
    v = _seq("ashp_axle_restore")[0]["variables"]
    assert "saved.on" in v["want_on"] and "saved.dhw" in v["want_dhw"] and "saved.t" in v["want_t"]


def test_saved_state_fits_an_input_text():
    # input_text max is 255; the saved JSON is {"mode":"export","on":"on","dhw":"heat","t":45.0,"boost":"off"}
    sample = json.dumps({"mode": "export", "on": "on", "dhw": "heat", "t": 45.0, "boost": "off"})
    assert len(sample) < 255
    assert _pkg()["input_text"]["ashp_axle_saved"]["max"] == 255


def test_decision_sensor_refreshes_each_minute_for_time_limits():
    tpl = _template_sensor("ashp_axle_decision")["state"]
    assert "now()" in tpl


def test_room_end_threshold_is_below_start_threshold():
    tpl = _template_sensor("ashp_axle_decision")["state"]
    assert "18.5" in tpl and "18.0" in tpl


def test_three_hour_cap():
    tpl = _template_sensor("ashp_axle_decision")["state"]
    assert str(3 * 3600) in tpl or "10800" in tpl


def test_started_timestamp_parses_like_ha_input_datetime():
    # input_datetime state format "YYYY-MM-DD HH:MM:SS" must parse in the time-cap check
    s = _base(
        **{"input_text.ashp_axle_active": "export", "binary_sensor.predbat_axle_event": _event("export")}
    )
    s["input_datetime.ashp_axle_started"] = (NOW - timedelta(hours=2, minutes=59)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert _render(_template_sensor("ashp_axle_decision")["state"], s) == "hold"
