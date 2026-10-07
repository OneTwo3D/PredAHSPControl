"""Shadow optimiser: choose an hourly room-setpoint schedule for the next hours.

Decision: one Daikin room setpoint per hour from ``setpoints`` (0.5 K steps). The house, thermostat,
RT modulation, radiators and COP are simulated with :func:`predictor.plant_step` on 15-minute steps.

Objective (pence)::

    J = Σ electricity_kWh · marginal_rate
      + w_bound · (K·h outside [room_min, room_max])  + w_bound2 · (K²·h outside)
      + w_start · compressor starts (thermostat off→on)
      + w_move  · setpoint changes (per 0.5 K)

The comfort limits are treated as hard in intent: their penalty is far above any plausible saving, so a
plan only violates them when the plant cannot avoid it (reported as infeasible). There is no fixed
night setback: lower night setpoints are chosen only when they reduce J.

Search: dynamic programming over hours with state merging on (setpoint, room temperature bin,
thermostat state), keeping the cheapest path per state. Deterministic and bounded.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .predictor import PlantParams, plant_step
from .thermal_model import ThermalParams


@dataclass(frozen=True)
class OptimiserConfig:
    room_min_c: float = 20.0
    room_max_c: float = 22.0
    setpoints: tuple[float, ...] = (20.0, 20.5, 21.0, 21.5, 22.0)
    horizon_h: int = 24
    step_h: float = 0.25
    ti_bin_k: float = 0.1
    w_bound_p_per_kh: float = 2000.0  # above any plausible price incl. VPP events: limits are hard
    w_bound2_p_per_k2h: float = 20000.0
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


@dataclass
class _Node:
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
) -> tuple[float, bool, float, float, int, float, float, list[float], list[float]]:
    """Simulate ``n`` steps from step ``k0``; returns state, costs and per-step traces."""
    obj = energy = 0.0
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
        below = max(0.0, cfg.room_min_c - ti)
        above = max(0.0, ti - cfg.room_max_c)
        dev = below + above
        if dev > 0:
            obj += (cfg.w_bound_p_per_kh * dev + cfg.w_bound2_p_per_k2h * dev * dev) * dt
        lo, hi = min(lo, ti), max(hi, ti)
        tis.append(ti)
        elec.append(e_kwh)
    return ti, running, obj, energy, starts, lo, hi, tis, elec


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
    ti, running = inp.ti0_c, inp.running0 and inp.heating_enabled
    obj = energy = 0.0
    starts = 0
    lo, hi = ti, ti
    tis, elec = [ti], []
    prev = inp.current_setpoint_c
    for h, sp in enumerate(setpoints_per_hour):
        obj += cfg.w_move_p_per_half_k * abs(sp - prev) / 0.5
        prev = sp
        ti, running, o, e, s, a, b, t_tr, e_tr = _simulate_hour(
            ti, running, sp, h * per_h, per_h, inp, building, plant, cop, cfg
        )
        obj += o
        energy += e
        starts += s
        lo, hi = min(lo, a), max(hi, b)
        tis += t_tr
        elec += e_tr
    feasible = lo >= cfg.room_min_c - 0.05 and hi <= cfg.room_max_c + 0.05
    return PlanResult(list(setpoints_per_hour), tis, elec, energy, obj, starts, lo, hi, feasible)


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
    hours = min(cfg.horizon_h, len(inp.to_c) // per_h)
    start = _Node(
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
                ti, running, o, e, s, a, b, _, _ = _simulate_hour(
                    node.ti, node.running, sp, h * per_h, per_h, inp, building, plant, cop, cfg
                )
                nodes += 1
                cand = _Node(
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
                if key not in nxt or cand.cost < nxt[key].cost:
                    nxt[key] = cand
        frontier = nxt
    best = min(frontier.values(), key=lambda n: n.cost)
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
    per_h = round(1 / cfg.step_h)
    hours = min(cfg.horizon_h, len(inp.to_c) // per_h)
    plan = optimise(inp, building, plant, cop, cfg)
    baseline = evaluate(list(inp.baseline_setpoint_c[:hours]), inp, building, plant, cop, cfg)
    sp_now = plan.setpoints_c[0]
    saving = baseline.cost_p - plan.cost_p

    # Describe the schedule as contiguous blocks
    blocks: list[str] = []
    h0 = 0
    for h in range(1, hours + 1):
        if h == hours or plan.setpoints_c[h] != plan.setpoints_c[h0]:
            t_a = (inp.start + timedelta(hours=h0)).strftime("%H:%M")
            t_b = (inp.start + timedelta(hours=h)).strftime("%H:%M")
            blocks.append(f"{t_a}–{t_b} {plan.setpoints_c[h0]:.1f} °C")
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
        details={
            "blocks": blocks,
            "plan_starts": plan.starts,
            "baseline_starts": baseline.starts,
            "baseline_min_c": round(baseline.min_ti_c, 2),
            "baseline_feasible": baseline.feasible,
            "nodes": plan.nodes,
        },
    )
