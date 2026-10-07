import math

from custom_components.daikin_mpc.core.intervals import IntervalClass, IntervalSummary, classify

BASE = dict(
    compressor_hz_min=20,
    compressor_hz_max=30,
    dhw_heat_kwh=0,
    dhw_run_h=0,
    backup_kwh=0,
    flow_l_min_min=7,
    ti_c=20.5,
    to_c=5,
)


def c(**kw):
    return classify(IntervalSummary(**{**BASE, **kw}))


def test_classes():
    assert c() is IntervalClass.HEATING_FULL
    assert c(compressor_hz_min=0) is IntervalClass.HEATING_PARTIAL
    assert c(compressor_hz_min=0, compressor_hz_max=0) is IntervalClass.OFF
    assert c(dhw_heat_kwh=1) is IntervalClass.DHW
    assert c(backup_kwh=0.5) is IntervalClass.BACKUP_HEATER


def test_invalid_and_missing_are_conservative():
    assert c(ti_c=-127.996) is IntervalClass.INVALID
    assert c(compressor_hz_max=math.nan) is IntervalClass.INVALID
    assert c(dhw_heat_kwh=math.nan) is IntervalClass.DHW
