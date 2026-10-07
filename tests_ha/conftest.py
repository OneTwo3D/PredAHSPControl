from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.util import dt as dt_util

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def weather_calls(hass: HomeAssistant):
    calls = []

    async def handler(call) -> ServiceResponse:
        calls.append(call)
        now = dt_util.utcnow()
        return {
            "weather.forecast_home": {
                "forecast": [
                    {"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": 5.0 - 0.1 * h}
                    for h in range(1, 49)
                ]
            }
        }

    hass.services.async_register("weather", "get_forecasts", handler, supports_response=SupportsResponse.ONLY)
    return calls
