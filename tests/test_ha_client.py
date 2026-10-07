import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ha_client


def test_write_commands_are_refused():
    with pytest.raises(PermissionError):
        ha_client.ws_calls([{"type": "call_service", "domain": "climate", "service": "set_temperature"}])


def test_rest_path_allowlist():
    with pytest.raises(PermissionError):
        ha_client.rest_get("/auth/token")
