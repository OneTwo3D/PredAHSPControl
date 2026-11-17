"""A date's energy total is complete when live counter readings bracket midnight closely (<= MAX_GAP_S).

Found 15 Nov 2026: a 3-minute MQTT/network blip at 00:51 made one 5-minute sample "counters stale"; the
old rule rejected the previous date for any stale sample in the new date's first hour, so 14 Nov (a
complete day) was never learned. Short outages and HA restarts away from the boundary, or bracketing it
within MAX_GAP_S, must not cost a training day; a long outage across midnight still must.
"""

import json
from datetime import UTC, datetime, timedelta

from custom_components.daikin_mpc.core.accumulator import DayAggregator, HourAccumulator, HourRecord
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.telemetry import Reading, Role, Snapshot, validate

from .test_live_core import snap

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=5)


def _stale(s):
    """The same sample with the bridge heartbeat unavailable (as during an MQTT/network blip)."""
    readings = {r: Reading(v) for r, v in s.values.items()}
    readings[Role.HEARTBEAT] = Reading(None)
    return validate(Snapshot(s.time, readings))


def _feed(times, is_stale, acc=None, days=None):
    """1 kWh of heat per hour; returns (emitted day records, accumulator, aggregator)."""
    acc, days = acc or HourAccumulator(), days or DayAggregator()
    out = []
    for t in times:
        heat = 100.0 + (t - T0).total_seconds() / 3600.0
        s = snap(t, heat_kwh=heat)
        if is_stale(t):
            s = _stale(s)
        for h in acc.add(s):
            r = days.add(h)
            if r:
                out.append(r)
    return out, acc, days


def _times(start, end, skip=lambda t: False):
    t, out = start, []
    while t < end:
        if not skip(t):
            out.append(t)
        t += STEP
    return out


def test_short_blip_in_the_first_hour_keeps_the_previous_day():
    blip = (datetime(2026, 11, 2, 0, 50, tzinfo=UTC), datetime(2026, 11, 2, 0, 55, tzinfo=UTC))
    recs, _, _ = _feed(_times(T0, T0 + timedelta(hours=26)), lambda t: blip[0] <= t < blip[1])
    days = {r.day: r for r in recs}
    assert "2026-11-01" in days
    assert abs(days["2026-11-01"].heat_kwh - 24.0) < 0.05


def test_short_outage_across_midnight_keeps_both_sides_when_bracketed():
    # an HA restart 23:55-00:10: no samples, then counters unreadable for the first sample after start
    gone = (datetime(2026, 11, 1, 23, 55, tzinfo=UTC), datetime(2026, 11, 2, 0, 10, tzinfo=UTC))
    first_after = gone[1]
    recs, _, _ = _feed(
        _times(T0, T0 + timedelta(hours=26), skip=lambda t: gone[0] <= t < gone[1]),
        lambda t: t == first_after,
    )
    days = {r.day: r for r in recs}
    # last live reading 23:50, next 00:15: 25 min apart > MAX_GAP_S (20 min) -> must still be dropped
    assert "2026-11-01" not in days


def test_restart_bracketing_midnight_within_max_gap_keeps_the_day():
    # no samples 23:55-00:05, first sample after start unreadable, live again 00:10: 20 min bracket
    gone = (datetime(2026, 11, 1, 23, 55, tzinfo=UTC), datetime(2026, 11, 2, 0, 5, tzinfo=UTC))
    recs, _, _ = _feed(
        _times(T0, T0 + timedelta(hours=26), skip=lambda t: gone[0] <= t < gone[1]),
        lambda t: t == gone[1],
    )
    days = {r.day: r for r in recs}
    assert "2026-11-01" in days
    assert abs(days["2026-11-01"].heat_kwh - 24.0) < 0.05


def test_first_hour_without_any_live_counter_reading_drops_the_previous_day():
    stale = (datetime(2026, 11, 1, 23, 50, tzinfo=UTC), datetime(2026, 11, 2, 1, 5, tzinfo=UTC))
    recs, _, _ = _feed(_times(T0, T0 + timedelta(hours=26)), lambda t: stale[0] <= t < stale[1])
    assert "2026-11-01" not in {r.day for r in recs}


def test_ha_restart_mid_day_via_saved_state_keeps_the_day():
    """Engine-level: save/restore around a 5-minute restart at 17:33, blip at 00:51 the next day."""
    eng = ShadowEngine(EngineConfig())
    restart = (datetime(2026, 11, 1, 17, 33, tzinfo=UTC), datetime(2026, 11, 1, 17, 38, 27, tzinfo=UTC))
    blip = (datetime(2026, 11, 2, 0, 51, tzinfo=UTC), datetime(2026, 11, 2, 0, 54, tzinfo=UTC))
    # start the evening before: the first counter reading is a new baseline, which (correctly) makes the
    # date it falls in incomplete
    t, end, done = T0 - timedelta(hours=2) + timedelta(seconds=23), T0 + timedelta(hours=26), False
    while t < end:
        if not done and t >= restart[0]:
            state = json.loads(json.dumps(eng.to_dict()))
            eng = ShadowEngine(EngineConfig())
            assert eng.load_dict(state) == []
            t, done = restart[1], True
        s = snap(t, heat_kwh=100.0 + (t - T0).total_seconds() / 3600.0)
        if blip[0] <= t < blip[1]:
            s = _stale(s)
        eng.process(s, None, {}, None)
        t += STEP
    assert eng.last_day is not None and eng.last_day.day == "2026-11-01"


def test_hour_record_bracket_round_trip_and_legacy_default():
    _, acc, days = _feed(_times(T0, T0 + timedelta(hours=2)), lambda t: False)
    h = days._hours[0]
    back = HourRecord.from_dict(json.loads(json.dumps(h.to_dict())))
    assert back.counter_bracket == h.counter_bracket
    legacy = h.to_dict()
    legacy.pop("counter_bracket")
    assert HourRecord.from_dict(legacy).counter_bracket is None
    # the in-progress hour carries it through a save/restore as well
    restored = HourAccumulator()
    restored.load_dict(json.loads(json.dumps(acc.to_dict())))
    assert restored._acc.bracket == acc._acc.bracket
