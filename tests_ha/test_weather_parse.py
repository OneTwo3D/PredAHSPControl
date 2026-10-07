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
