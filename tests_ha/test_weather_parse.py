"""Malformed weather responses never raise."""

from custom_components.daikin_mpc.coordinator import _parse_forecast


def test_malformed_weather_responses():
    e = "weather.home"
    assert _parse_forecast(None, e, None) == []
    assert _parse_forecast({e: None}, e, None) == []
    assert _parse_forecast({e: {"forecast": [None, "x", {"datetime": "bad"}]}}, e, None) == []
    ok = _parse_forecast(
        {
            e: {
                "forecast": [
                    {"datetime": "2026-11-02T10:00:00+00:00", "temperature": 41},
                    {"datetime": "2026-11-02T11:00:00+00:00", "temperature": "nan"},
                ]
            }
        },
        e,
        "°F",
    )
    assert len(ok) == 1 and abs(ok[0][1] - 5.0) < 1e-9


def test_remapped_counter_baselines_are_dropped():
    from types import SimpleNamespace

    from custom_components.daikin_mpc.coordinator import DaikinMpcCoordinator

    c = SimpleNamespace(mapping={"ext_kwh": "sensor.new_meter", "heat_kwh": "sensor.heat"})
    c._counter_entities = lambda: DaikinMpcCoordinator._counter_entities(c)
    stored = {
        "counter_entities": {"ext_kwh": "sensor.old_meter", "heat_kwh": "sensor.heat"},
        "last_counters": {"ext_kwh": 1000.0, "heat_kwh": 5.0},
        "counter_time": {"ext_kwh": "2026-11-02T12:00:00+00:00", "heat_kwh": "2026-11-02T12:00:00+00:00"},
    }
    out = DaikinMpcCoordinator._drop_remapped_counters(c, stored)
    assert out["last_counters"] == {"heat_kwh": 5.0} and list(out["counter_time"]) == ["heat_kwh"]
