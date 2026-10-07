"""Shadow optimiser: choose an hourly room-setpoint schedule for the next hours.

Decision: one Daikin room setpoint per hour from ``setpoints`` (0.5 K steps). The house, thermostat,
RT modulation, radiators and COP are simulated with :func:`predictor.plant_step` on 15-minute steps.

Objective (pence)::

    J = Σ electricity_kWh · marginal_rate
      + w_target · (K·h below the comfort-period target) + w_target2 · (K²·h below it)
      + w_start · compressor starts (thermostat off→on)
      + w_move  · setpoint changes (per 0.5 K)

The comfort limits [room_min, room_max] are hard: plans are ranked lexicographically by
(K·h outside the limits, J), so no saving at any price can buy a violation. Only when every plan
violates them is the least-violating one returned (reported as infeasible, best effort). Comfort-period
targets (e.g. 21 °C 07–09 and 18–24) are soft: missing them costs a substantial but finite penalty. There
is no fixed night setback: lower night setpoints are chosen only when they reduce J.

Search: dynamic programming over hours with state merging on (setpoint, room temperature bin of
``ti_bin_k``, thermostat state), keeping the best path per state. Deterministic and bounded, but an
approximation: two paths in one bin differ by up to ``ti_bin_k``, and the discarded one could turn out
cheaper later. Tests compare it with exhaustive search on short horizons.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .predictor import PlantParams, plant_step
from .thermal_model import ThermalParams
from .timeutil import add_hours

_VIOL_EPS = 1e-6  # K·h; below this a plan counts as within the limits (float noise)


@dataclass(frozen=True)
class OptimiserConfig:
    room_min_c: float = 20.0
    room_max_c: float = 22.0
    setpoints: tuple[float, ...] = (20.0, 20.5, 21.0, 21.5, 22.0)
    horizon_h: int = 24
    step_h: float = 0.25
    ti_bin_k: float = 0.1
    # Only used to report a scalar objective for infeasible plans; the ranking is lexicographic.
    w_bound_p_per_kh: float = 2000.0
    w_bound2_p_per_k2h: float = 20000.0
    w_target_p_per_kh: float = 30.0
    w_target2_p_per_k2h: float = 60.0
    w_start_p: float = 1.0
    w_move_p_per_half_k: float = 0.3

    def __post_init__(self) -> None:
        if not self.room_min_c < self.room_max_c:
            raise ValueError("room_min must be below room_max")
        if not self.setpoints or any(
            not self.room_min_c - 1e-9 <= s <= self.room_max_c + 1e-9 for s in self.setpoints
        ):
            raise ValueError("setpoints must lie within [room_min, room_max]")


@dataclass(frozen=True)
class PlanInputs:
    start: datetime
    ti0_c: float
    running0: bool
    current_setpoint_c: float
    heating_enabled: bool
    to_c: Sequence[float]  # per step
    lwt_set_c: Sequence[float]  # per step, weather-curve LWT
    rate_p: Sequence[float]  # per step, marginal pence/kWh
    baseline_setpoint_c: Sequence[float]  # per hour, what the native schedule would do
    target_c: Sequence[float | None] | None = None  # per step, soft comfort target (None = none)


@dataclass
class PlanResult:
    setpoints_c: list[float]  # per hour
    ti_c: list[float]  # per step (+1)
    elec_kwh: list[float]  # per step
    cost_p: float  # energy cost only
    objective_p: float
    starts: int
    min_ti_c: float
    max_ti_c: float
    feasible: bool
    nodes: int = 0
    violation_kh: float = 0.0  # K·h outside [room_min, room_max]

    def energy_kwh(self) -> float:
        return sum(self.elec_kwh)


@dataclass
class Recommendation:
    plan: PlanResult
    baseline: PlanResult
    setpoint_now_c: float
    saving_p: float
    reason: str
    details: dict[str, object] = field(default_factory=dict)
    start: datetime | None = None  # plan start; the hourly schedule is relative to this time


@dataclass
class _Node:
    viol: float  # K·h outside the hard limits so far (ranked first)
    cost: float  # objective so far
    energy_cost: float
    ti: float
    running: bool
    sp: float
    starts: int
    min_ti: float
    max_ti: float
    path: tuple[float, ...]


def _simulate_hour(
    ti: float,
    running: bool,
    sp: float,
    k0: int,
    n: int,
    inp: PlanInputs,
    building: ThermalParams,
    plant: PlantParams,
    cop: Callable[[float], float],
    cfg: OptimiserConfig,
) -> tuple[float, bool, float, float, float, int, float, float, list[float], list[float]]:
    """Simulate ``n`` steps from step ``k0``; returns state, costs and per-step traces."""
    obj = energy = viol = 0.0
    starts = 0
    lo, hi = math.inf, -math.inf
    tis: list[float] = []
    elec: list[float] = []
    dt = cfg.step_h
    for k in range(k0, k0 + n):
        was = running
        ti, running, q = plant_step(
            ti, running, inp.to_c[k], sp, inp.lwt_set_c[k], inp.heating_enabled, building, plant, dt
        )
        e_kwh = q * dt / max(cop(inp.to_c[k]), 1.0) / 1000 if q > 0 else 0.0
        c = e_kwh * inp.rate_p[k]
        energy += c
        obj += c
        if running and not was:
            starts += 1
            obj += cfg.w_start_p
        tgt = inp.target_c[k] if inp.target_c is not None else None
        if tgt is not None and ti < tgt:
            d = tgt - ti
            obj += (cfg.w_target_p_per_kh * d + cfg.w_target2_p_per_k2h * d * d) * dt
        below = max(0.0, cfg.room_min_c - ti)
        above = max(0.0, ti - cfg.room_max_c)
        viol += (below + above) * dt
        lo, hi = min(lo, ti), max(hi, ti)
        tis.append(ti)
        elec.append(e_kwh)
    return ti, running, obj, energy, viol, starts, lo, hi, tis, elec


def _check_inputs(inp: PlanInputs, cfg: OptimiserConfig) -> int:
    """Validate sequence lengths; returns the number of whole hours that can be planned."""
    per_h = round(1 / cfg.step_h)
    if per_h < 1 or abs(per_h * cfg.step_h - 1.0) > 1e-9:
        raise ValueError("step_h must divide one hour")
    steps = min(len(inp.to_c), len(inp.lwt_set_c), len(inp.rate_p))
    if inp.target_c is not None:
        steps = min(steps, len(inp.target_c))
    hours = min(cfg.horizon_h, steps // per_h)
    if hours < 1:
        raise ValueError("inputs cover less than one hour")
    return hours


def evaluate(
    setpoints_per_hour: Sequence[float],
    inp: PlanInputs,
    building: ThermalParams,
    plant: PlantParams,
    cop: Callable[[float], float],
    cfg: OptimiserConfig,
) -> PlanResult:
    """Simulate a given hourly setpoint schedule."""
    per_h = round(1 / cfg.step_h)
    if len(setpoints_per_hour) > _check_inputs(inp, cfg):
        raise ValueError("schedule longer than the inputs")
    ti, running = inp.ti0_c, inp.running0 and inp.heating_enabled
    obj = energy = viol = 0.0
    starts = 0
    lo, hi = ti, ti
    tis, elec = [ti], []
    prev = inp.current_setpoint_c
    for h, sp in enumerate(setpoints_per_hour):
        obj += cfg.w_move_p_per_half_k * abs(sp - prev) / 0.5
        prev = sp
        ti, running, o, e, v, s, a, b, t_tr, e_tr = _simulate_hour(
            ti, running, sp, h * per_h, per_h, inp, building, plant, cop, cfg
        )
        obj += o
        energy += e
        viol += v
        starts += s
        lo, hi = min(lo, a), max(hi, b)
        tis += t_tr
        elec += e_tr
    feasible = viol <= _VIOL_EPS
    # scalar objective for reporting only (ranking is lexicographic)
    obj += cfg.w_bound_p_per_kh * viol
    return PlanResult(
        list(setpoints_per_hour), tis, elec, energy, obj, starts, lo, hi, feasible, violation_kh=viol
    )


def _rank(n: _Node) -> tuple[float, float]:
    """Lexicographic: violation of the hard limits first (float noise ignored), then the objective."""
    return (n.viol if n.viol > _VIOL_EPS else 0.0, n.cost)


def optimise(
    inp: PlanInputs,
    building: ThermalParams,
    plant: PlantParams,
    cop: Callable[[float], float],
    cfg: OptimiserConfig | None = None,
) -> PlanResult:
    """Cheapest hourly setpoint schedule by dynamic programming with state merging."""
    cfg = cfg or OptimiserConfig()
    per_h = round(1 / cfg.step_h)
    hours = _check_inputs(inp, cfg)
    start = _Node(
        0.0,
        0.0,
        0.0,
        inp.ti0_c,
        inp.running0 and inp.heating_enabled,
        inp.current_setpoint_c,
        0,
        inp.ti0_c,
        inp.ti0_c,
        (),
    )
    frontier: dict[tuple[float, int, bool], _Node] = {(start.sp, 0, start.running): start}
    nodes = 0
    for h in range(hours):
        nxt: dict[tuple[float, int, bool], _Node] = {}
        for node in frontier.values():
            for sp in cfg.setpoints:
                move = cfg.w_move_p_per_half_k * abs(sp - node.sp) / 0.5
                ti, running, o, e, v, s, a, b, _, _ = _simulate_hour(
                    node.ti, node.running, sp, h * per_h, per_h, inp, building, plant, cop, cfg
                )
                nodes += 1
                cand = _Node(
                    node.viol + v,
                    node.cost + move + o,
                    node.energy_cost + e,
                    ti,
                    running,
                    sp,
                    node.starts + s,
                    min(node.min_ti, a),
                    max(node.max_ti, b),
                    (*node.path, sp),
                )
                key = (sp, round(ti / cfg.ti_bin_k), running)
                if key not in nxt or _rank(cand) < _rank(nxt[key]):
                    nxt[key] = cand
        frontier = nxt
    best = min(frontier.values(), key=_rank)
    result = evaluate(best.path, inp, building, plant, cop, cfg)
    result.nodes = nodes
    return result


def recommend(
    inp: PlanInputs,
    building: ThermalParams,
    plant: PlantParams,
    cop: Callable[[float], float],
    cfg: OptimiserConfig | None = None,
) -> Recommendation:
    """Optimise and compare with the native schedule; produce a readable explanation."""
    cfg = cfg or OptimiserConfig()
    hours = _check_inputs(inp, cfg)
    if len(inp.baseline_setpoint_c) < hours:
        raise ValueError("baseline schedule shorter than the planning horizon")
    plan = optimise(inp, building, plant, cop, cfg)
    baseline = evaluate(list(inp.baseline_setpoint_c[:hours]), inp, building, plant, cop, cfg)
    sp_now = plan.setpoints_c[0]
    saving = baseline.cost_p - plan.cost_p

    # Describe the schedule as contiguous blocks
    blocks: list[str] = []
    h0 = 0
    for h in range(1, hours + 1):
        if h == hours or plan.setpoints_c[h] != plan.setpoints_c[h0]:
            a, b = add_hours(inp.start, h0), add_hours(inp.start, h)
            days = (b.date() - inp.start.date()).days
            t_b = b.strftime("%H:%M") + (" next day" if days == 1 else f" +{days} d" if days > 1 else "")
            blocks.append(f"{a.strftime('%H:%M')}–{t_b} {plan.setpoints_c[h0]:.1f} °C")
            h0 = h
    if not inp.heating_enabled:
        reason = "Space heating is switched off; no heating planned."
    elif not plan.feasible:
        reason = (
            f"Comfort range {cfg.room_min_c:.1f}–{cfg.room_max_c:.1f} °C cannot be held throughout "
            f"(predicted {plan.min_ti_c:.1f}–{plan.max_ti_c:.1f} °C); best effort shown."
        )
    else:
        change = sp_now - inp.current_setpoint_c
        verb = "keep" if abs(change) < 0.25 else ("raise" if change > 0 else "lower")
        reason = (
            f"{verb.capitalize()} setpoint to {sp_now:.1f} °C. Plan: {'; '.join(blocks[:4])}"
            f"{' …' if len(blocks) > 4 else ''}. Expected {plan.energy_kwh():.2f} kWh, "
            f"{plan.cost_p:.0f}p vs {baseline.cost_p:.0f}p for the current schedule; room "
            f"{plan.min_ti_c:.1f}–{plan.max_ti_c:.1f} °C."
        )
    return Recommendation(
        plan=plan,
        baseline=baseline,
        setpoint_now_c=sp_now,
        saving_p=saving,
        reason=reason,
        start=inp.start,
        details={
            "blocks": blocks,
            "plan_starts": plan.starts,
            "baseline_starts": baseline.starts,
            "baseline_min_c": round(baseline.min_ti_c, 2),
            "baseline_feasible": baseline.feasible,
            "plan_violation_kh": round(plan.violation_kh, 3),
            "nodes": plan.nodes,
        },
    )
