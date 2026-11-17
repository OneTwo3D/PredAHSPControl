"""Outdoor temperature from a change-only bridge may stay unchanged for hours on a calm night.

11 Nov 2026: the Daikin outdoor sensor (0.5 K steps) did not change 03:40-07:10, so with the fixed 3 h
age limit every snapshot was incomplete for >3 h and the (otherwise complete) day was lost. While the
bridge heartbeat is live an unchanged value is steady, not stale, up to TO_MAX_AGE_LIVE_BRIDGE_S.
"""

from datetime import UTC, datetime

from custom_components.daikin_mpc.core.telemetry import (
    TO_MAX_AGE_LIVE_BRIDGE_S,
    Reading,
    Role,
    Snapshot,
    validate,
)

T0 = datetime(2026, 11, 11, 5, 30, tzinfo=UTC)
BASE = {
    Role.TI: Reading(20.5),
    Role.LWT: Reading(30.0),
    Role.RWT: Reading(28.0),
    Role.HZ: Reading(0.0),
    Role.HEAT_KWH: Reading(100.0),
    Role.ELEC_KWH: Reading(30.0),
}


def _snap(to_age_h, heartbeat_age_s, to_on_bridge=True):
    rd = {**BASE, Role.TO: Reading(4.5, age_s=to_age_h * 3600)}
    if heartbeat_age_s is not None:
        rd[Role.HEARTBEAT] = Reading(1.0, age_s=heartbeat_age_s)
    return validate(Snapshot(T0, rd), to_on_bridge=to_on_bridge)


def test_unchanged_outdoor_temperature_with_live_bridge_is_steady():
    s = _snap(6, 30)
    assert Role.TO not in s.issues and s.complete


def test_outdoor_temperature_still_goes_stale_eventually():
    s = _snap(TO_MAX_AGE_LIVE_BRIDGE_S / 3600 + 1, 30)
    assert s.issues[Role.TO].startswith("stale")


def test_without_live_bridge_the_short_limit_applies():
    assert _snap(4, 3600).issues[Role.TO].startswith("stale")  # heartbeat itself stale
    assert _snap(4, None).issues[Role.TO].startswith("stale")  # no heartbeat mapped/read


def test_live_bridge_does_not_vouch_for_an_outdoor_sensor_elsewhere():
    """Codex review PR #1 round 9: with the outdoor temperature from another device (e.g. a weather
    station), the bridge heartbeat says nothing about it: the 3 h limit applies."""
    assert _snap(4, 30, to_on_bridge=False).issues[Role.TO].startswith("stale")
    assert Role.TO not in _snap(2, 30, to_on_bridge=False).issues
