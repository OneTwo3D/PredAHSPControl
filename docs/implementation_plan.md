# Daikin Altherma MPC — Implementation Plan v2 (optimised)

Updated: 7 October 2026. Supersedes `docs/original_plan_v1.md` (kept for traceability).

Scope: self-learning supervisory controller for space heating, followed by a separately enabled DHW stage.

## 0. What changed from v1

| Area | v1 | v2 |
|---|---|---|
| Order of work | Build integration first, learn live | **Offline-first**: fit models from HA history (incl. last winter's long-term statistics) before writing the integration |
| Heat input → offset link | "Learn a bounded model of heat output" (unspecified) | Explicit **radiator emitter model** `Q = K·ΔT^n` ties offset → LWT → watts |
| Room thermostat | Not considered | **Thermostat gating** modelled explicitly (LWT + room thermostat installation) |
| Predheat | Reference only | **Calibrate Predheat** from the offline fit as an early quick win |
| Optimiser | Beam search, width 50–200 | Hourly decisions with **state-merging dynamic programming** (exact for this small action space) |
| Structure | DHW/safety caveats spread over ~15 sections | One cross-cutting rules section (§3), one DHW section (§12) |
| Delivery | 12 branches | 7 milestones (M0–M6) |
| Data retention | Not addressed | **Raise recorder retention now**, before the heating season, so fine-grained training data exists |

## 1. Installation facts (confirmed)

- Daikin Altherma 3 via P1P2MQTT.
- Emitters: **radiators** (n ≈ 1.3 prior).
- Control: **weather-dependent LWT + room thermostat**, thermostat in a **north-facing living room**.
- Predbat: **HA add-on**. Predheat: **running** and feeding Predbat (settings likely need tuning).
- Season: just leaving summer. Raw recorder history (default 10 days) contains no heating data; last winter exists only as hourly long-term statistics (LTS), and only for entities with a `state_class`.

M0 findings (see `docs/entity_mapping.md`, `docs/data_inventory.md`):

- Native control mode is **RT** (Daikin room-thermostat control) with LWT modulation up to ±5 K, using the Daikin room sensor in the living room. Writable candidates: LWT deviation (`climate.bridge0_lwt_abs_heating`, ±10, step 1) and room setpoint (`climate.bridge0_room_room_heating`, 16–26, step 0.5). Which one is the primary actuator is an open decision (the 25 °C LWT floor removes downward deviation authority above ~9 °C outdoor).
- UA ≈ 94 W/K, net gains ≈ 440 W, C ≈ 3 kWh/K (weak); radiators ≈ 10 kW @ΔT50 (strongly oversized); heat-pump minimum output ≈ 0.7–0.9 kW, so cycling dominates above ~8 °C outdoor. The optimiser objective must treat cycle count/length and run timing as first-class, not only LWT.
- A second (Onecta cloud) control path exists; the MPC gateway must be the only automated writer.
- Raw recorder retention is 60–120 days; last winter exists only as hourly LTS.

Decisions (7 October 2026):

- **Primary space-heating actuator: the Daikin room setpoint** (`climate.bridge0_room_room_heating`, 0.5 K steps), letting native RT modulation and the WD curve do the LWT work. The LWT deviation stays at its native value and is only a possible secondary actuator after separate commissioning. All v1 rules for the deviation (bounds, step, write budget, readback, hold) apply equally to the setpoint; commissioning band ±0.5 K around the scheduled setpoint.
- The Onecta overnight −6 K deviation schedule (bedroom setback) is to be replaced by the HA-connected bedroom radiator thermostat, so the living-room-referenced heat pump is no longer throttled in the cheap 00:00–05:00 window. The bedroom TRV is a comfort input/constraint, not a controller actuator in V1.
- Unit: Daikin Altherma 3 R monobloc **EDLA04E2V3** (4 kW class) for COP/capacity priors.
- Predheat calibration suggestions: `docs/predheat_calibration.md`.

Still to verify: thermostat type (Daikin Madoka/Human Comfort Interface via P1P2 vs external on/off contact), whether its setpoint is visible/writable, exact P1P2 entity names and writability, write-budget entities, energy-meter type, DHW arrangement.

## 2. Objective and non-goals

The controller must, in priority order:

1. Keep the reference room within the comfort band (hard limits are constraints).
2. Keep the Daikin low-and-slow: lowest LWT that meets demand, long continuous runs, few thermostat on/off cycles.
3. Minimise marginal energy cost using Predbat's view of tariffs, battery and PV.
4. Learn building, emitter and heat-pump behaviour with honest uncertainty.
5. Publish a 24–48 h heating (later heating + DHW) electrical-load forecast for Predbat.
6. Leave native Daikin operation fully functional if it stops.

Non-goals: controlling compressor, defrost, backup/booster heaters, refrigeration, installer field settings, Predbat's battery decisions, or the inverter. No modification of Predbat, Predheat or P1P2MQTT source.

Primary actuator: the verified writable P1P2MQTT weather-dependent heating deviation (discovery hints: `LWT_Deviation_Heating`, `Deviation_Heating`). Daikin stays in weather-dependent mode.

## 3. Cross-cutting rules (apply to every phase)

### 3.1 Safety
- Exactly one command gateway. Every write passes `SafetySupervisor`, revalidated immediately before sending.
- Supervisor checks: mode permits writes for that function (heating and DHW permissions are independent), entity availability and freshness, correct native mode, no fault, no defrost (+ recovery interval), no native DHW in progress (for heating writes), bounds, step size, minimum interval, write budget, model validity, value differs from actual.
- A successful service call is not actuation. Confirm by readback; classify timeout or refusal; never retry aggressively.
- Manual changes detected outside the controller trigger a hold/cooldown, never an immediate overwrite.
- Never: disable native safety, force compressor, request/suppress defrost, force heaters, alter installer settings, raise the P1P2 budget, override or postpone DHW hygiene/protection, retry to overcome refusals, make basic heating depend on MPC.

### 3.2 P1P2 write budget
- Discover and verify `Write_Budget`, `Write_Budget_Period`, `Writes_Refused_Budget`, `Writes_Refused_Busy` (or equivalents).
- Default: at most one supervisory change per hour across heating and DHW combined. Never repeat an unchanged value.
- Warn on refusal-counter increases; suspend after repeated busy refusals.
- If budget telemetry is unavailable, Active mode fails closed unless a separately verified conservative policy is configured.

### 3.3 Failure behaviour
- A crash leaves the last offset in place; it does not reset. Every value the controller may write must therefore be acceptable if retained indefinitely (hence ±2 °C max, ±1 °C at commissioning).
- Orderly disable may restore a verified baseline through the gateway if budget and safety permit.
- Stronger fallback guarantees need an independent native schedule or watchdog; document, don't claim.

### 3.4 Data integrity
- Never invent entity IDs, forecast formats, operating-state meanings or writable capabilities. Configure via config/options flow; record verified mappings in `docs/entity_mapping.md`.
- Classify every interval: clean heating / DHW / defrost / backup heater / thermostat-off / stale / gap / manual intervention / anomaly. Expose counts and reasons.
- Never train the building model with DHW heat, never establish COP from heat that was itself estimated from an assumed COP.
- Forecasts carry generation timestamp, version, timezone and units.

### 3.5 Modes
`off` / `shadow` (default) / `active` / `hold`, independently for heating and DHW. Restart always comes up in shadow unless Active-restore is explicitly configured and startup gating passes. Elapsed time or sample counts never enable Active.

## 4. Architecture

```text
custom_components/daikin_mpc/          # HA layer: config flow, coordinator, entities, storage, gateway
    core/                              # pure Python, no HA imports, fully unit-testable
        models.py  telemetry.py  intervals.py
        thermal_model.py  emitter_model.py  heatpump_model.py  thermostat_model.py
        learner.py  forecast.py  cost_model.py  optimiser.py
        controller.py  safety.py  persistence.py  diagnostics.py
        dhw_model.py  dhw_scheduler.py  operating_arbiter.py      # M6
    adapters/  p1p2.py  predbat.py  predheat.py  weather.py  commands.py
tools/
    ha_export.py      # read-only HA API exporter (REST + WebSocket) → parquet/CSV
    fit_offline.py    # offline model identification + report
    replay.py         # replay controller over recorded data, never writes
tests/  (pytest, fixtures from anonymised exports)
docs/   entity_mapping.md  data_inventory.md  offline_fit_report.md  predheat_calibration.md
        commissioning.md  dhw_commissioning.md  forecast_contract.md
```

Data flow: adapters → interval classifier → state estimation → learners (bounded) → simulation → optimiser → arbiter → safety supervisor → single gateway.

Dependencies: standard library + numpy only in `core` (scipy optional offline in `tools/`). No ML frameworks, no cloud services.

## 5. Physical models

### 5.1 Building — 1R1C (V1)

```text
C · dTi/dt = Q_rad + Q_int − UA · (Ti − To)
```

`UA` W/K, `C` Wh/K (exposed as kWh/K), `Q` W, time in hours, exact exponential discretisation over the actual elapsed time. Learn bounded `UA`, `C`, `Q_int` (time-of-day profile later).

Radiators have a fast response, so 1R1C is a reasonable start. 2R2C stays deferred (M5+), but the interface (`predict/update/serialize/deserialize`, versioned) allows a swap.

North-facing reference room: solar gain in the reference room is small, so V1 omits explicit solar. Known issue: south rooms may overheat on sunny days while the living room still calls for heat. Mitigation: optional extra room sensors as diagnostics only in V1; an irradiance/PV-proxy gain term in M5.

### 5.2 Emitter — radiator model (new)

```text
Q_rad = K · max(0, MWT − Ti)^n ,   MWT = (LWT + RWT) / 2
```

- Prior `n = 1.3` (EN 442), learn `K` (W/K^n). Optionally learn `n` within [1.2, 1.4] if data is well excited.
- Cross-check with hydronic `Q = ṁ · cp · (LWT − RWT)` when flow is available. This is the cleanest independent heat measurement and identifies `K` without assuming COP.
- Purpose: predict heat delivered for any candidate LWT, hence the effect of an offset. Steady state: `UA·(Ti − To) − Q_int = K·(MWT − Ti)^n` gives the **minimum LWT that holds the target**, which is the core "low-and-slow" quantity.
- Also yields a check on the native weather curve: if the curve LWT is consistently above the steady-state requirement, the controller recommends a negative offset (and reports the curve-adjustment suggestion for the installer; V1 never edits the curve).

### 5.3 Room-thermostat gating (new)

The heat pump only delivers heat while the room thermostat calls. Model thermostat state explicitly:

- Telemetry: thermostat demand / thermo-on state (to verify in M0), thermostat setpoint, room temperature.
- Simulation: `Q_rad` is applied only while simulated `Ti < setpoint − hysteresis_on`, released at `Ti ≥ setpoint + hysteresis_off` (learnt from observed switching).
- Low-and-slow objective becomes concrete: **choose the lowest offset that keeps the thermostat calling continuously** (duty cycle → 100 %, few starts) while meeting comfort. A too-high offset shows up as on/off cycling with overshoot; too low shows up as sustained shortfall.
- The thermostat setpoint is an effective upper comfort limit. If the thermostat is a Daikin unit with a writable setpoint, setpoint control is a *possible future actuator* but is out of scope until separately verified and approved.
- Data filtering: thermostat-off intervals are valid for learning `UA`/`C` (free cooling) but not for emitter/COP learning.

### 5.4 Heat pump

- `COP = f(To, LWT)`: bounded binned surface with monotonic constraints (COP falls with higher LWT and lower To), prior from Daikin datasheet values for the installed model. Flag extrapolation.
- Capacity limit `Q_max(To, LWT)` from datasheet prior, refined from observed saturated periods. The optimiser must know when the plant cannot deliver.
- Defrost penalty: learnt energy/heat loss per hour as a function of To and humidity (if available), included in forecasts, excluded from steady-state COP.
- Exclude DHW, defrost + recovery, backup heater, start-up transients.

### 5.5 Heat-input hierarchy
1. Measured thermal production (counter → interval, reset handling).
2. Hydronic `ṁ·cp·ΔT` with explicit flow units.
3. Electrical × COP (estimate only; flagged; never used to learn COP).

## 6. Learning

- Recursive least squares with forgetting factor (or bounded rolling least squares), one learner per parameter group.
- Each parameter stores value, variance, bounds, useful-sample count, last update; rate-limit (e.g. `UA` ≤ 2 %/day).
- Identifiability: require excitation (spread of `Ti − To`, LWT, heating on/off) before narrowing uncertainty. Many near-identical samples must not increase confidence.
- Confidence is reported as calibrated prediction error (MAE/bias at 1/3/6/24 h on held-out data), not an invented percentage.
- **Priors come from the offline fit** (M1), so live learning starts close and only refines.

## 7. Forecast inputs

- Weather: HA `weather.get_forecasts` (hourly), interpolated to 30-min slots, 24 h (48 h where supplied). On loss: reuse last forecast with decaying confidence; suspend economic MPC after expiry.
- Predbat (add-on): verify actual entity names/attributes in M0 (typically `predbat.*` entities with tariff, SOC forecast and plan attributes). Consume a timestamped snapshot; rate-limit replanning to avoid feedback loops.
- Predheat: current `predheat.heat_energy` and temperature forecasts as a baseline for comparison.
- Missing economics degrade to tariff-only.

## 8. Optimiser

- Simulation step 30 min; decisions hourly (aligned with the write interval); horizon 12 h, extend to 24 h after validation.
- Actions per decision: keep / −1 / +1 within bounds (commissioning ±1 °C, config up to ±2 °C).
- **State-merging DP**: at each decision step keep the best partial trajectory per (offset value, Ti bin of 0.1 °C, thermostat state). With ≤5 offsets this is a few hundred nodes per step: exact enough, deterministic, and fast (target < 1 s). Beam search remains a fallback if more state dimensions are added (e.g. DHW).
- Runs in an executor; generation counter discards stale results; runtime measured and capped.
- Objective (units documented, weights configurable):

```text
J = Σ marginal_cost(t, kWh_t)
  + w_cold·max(0, soft_min − Ti)² + w_warm·max(0, Ti − soft_max)²     (w_cold > w_warm)
  + w_lwt·max(0, LWT − LWT_required)                                 (low-and-slow)
  + w_cycle·predicted_thermostat_starts
  + w_move·|Δoffset|
```

- Hard limits are constraints (infeasible trajectories rejected), not penalties. Battery scarcity enters only via `marginal_cost`, never as a separate term (no double counting).
- Infeasibility (plant capacity insufficient) is reported, not hidden.

## 9. Energy-cost model

`EnergyCostProvider` (`marginal_cost(t, incremental_kwh)`, `energy_source(t)`, `battery_constraint(t)`):

- V1 tariff-only.
- V2 Predbat-aware: battery covering daytime load is valued at its replacement cost (charge price ÷ round-trip efficiency, or foregone export), limited by remaining usable energy and power. Incremental heating consumes a shared budget so candidate loads can't reuse the same battery energy.
- Required scenario test: 7p night / 30p day with a battery that covers the day → no overnight overheating; battery exhausted before peak → modest precharge only if worth it; PV surplus → bounded preheat (+0.3…0.5 °C, inside soft_max/thermostat setpoint) only when better than export.
- Economic comfort shifting toward soft_min is opt-in.

## 10. Predheat / Predbat interfaces

### 10.1 Predheat calibration (early quick win, M1)
Predheat's model uses `heat_loss_watts`, `heat_loss_degrees`, `heat_gain_static`, `heat_output` (radiator rating), `heat_volume`, `heat_max_power`/`heat_min_power`, `heating_cop`, `flow_temp`, `flow_difference_target`, `weather_compensation`, `hysteresis`/`hysteresis_off`. The offline fit produces directly comparable values:

| Predheat setting | From offline fit |
|---|---|
| `heat_loss_watts` | `UA` (W/K) |
| `heat_loss_degrees` | `UA / C` (1/h) |
| `heat_gain_static` | `Q_int` |
| `heat_output` | radiator `K`, `n` at rated ΔT50 |
| `heating_cop` | median clean COP / COP curve |
| `weather_compensation` | verified native curve (+ current offset) |
| `hysteresis` | observed thermostat switching band |

Deliver `docs/predheat_calibration.md` with current vs suggested values and backtest error before/after. The user applies changes manually.

### 10.2 Predbat load forecast (M4)
Predbat currently consumes Predheat via `load_forecast: - predheat.heat_energy$external`. The MPC will publish an equivalent entity (e.g. `sensor.daikin_mpc_heat_energy` with the same attribute contract, verified against Predbat source at implementation time). Swapping is a manual, documented config change; never include both. Check whether Predbat's base load history already includes the ASHP (and how it is filtered) to avoid double counting.

## 11. Home Assistant surface

Mode selects (heating, later DHW), learning switch, comfort numbers (target, soft/hard min/max), offset bounds, min write interval; sensors for status, recommended/current offset, decision reason (with structured attributes), UA (W/K), C (kWh/K), Q_int (W), emitter K/n, LWT required, predicted Ti 1/3/6 h, energy/cost 24 h, COP and heat output with provenance, prediction error, training-sample counts by class, thermostat duty cycle, last decision/model update. Bounded decision log; optional `daikin_mpc_decision` events. Diagnostics with redaction. Persistence via HA `Store`, versioned JSON with migration and corruption rejection.

## 12. DHW stage (M6, separately enabled)

Condensed from v1 §50; all v1 requirements remain in force.

- Capability gate: only verified native scheduling/request interfaces; setpoint changes are not a substitute for a start command. Without a safe interface → recommendations and forecasts only.
- Lumped cylinder model `C_tank·dT/dt = Q_dhw − UA_tank·(T − T_amb) − Q_draw`; learn standing loss, reheat rate, recovery time; single-sensor uncertainty made explicit.
- Weekday/weekend demand profile; user readiness windows outrank inferred habits.
- Scheduler over 24 h with the shared cost provider; includes standing losses and the space-heating interruption (simulated room temperature dip, especially relevant with a north-facing reference room).
- Operating arbiter shares the global write budget, prevents alternating requests, reports infeasibility with a user-configured priority.
- Independent DHW shadow/active; heating Active never authorises DHW writes. Hygiene/protection cycles are never skipped, postponed or inferred as completed.
- Separate heating, DHW and combined forecasts; the combined one accounts for displacement and recovery.
- Tests: v1 §50.10 items 1–15.

## 13. Milestones

Each milestone = one branch/PR with tests, ruff, mypy.

### M0 — Discovery and data inventory (read-only HA API) ← next
1. Connect read-only (long-lived token of a dedicated non-admin user, from environment secrets `HA_URL`, `HA_TOKEN`).
2. `GET /api/states`: inventory P1P2MQTT, Predbat, Predheat, weather, thermostat, energy entities; record units, `state_class`, attributes. Output: `docs/entity_mapping.md`, unresolved-capability list.
3. Read Predbat and Predheat current configuration as exposed in entities; document what Predheat currently predicts vs actual.
4. Check recorder retention: raw history via `GET /api/history/period/<start>?filter_entity_id=…&minimal_response&no_attributes` (default ~10 days).
5. Last winter: hourly long-term statistics via WebSocket `recorder/statistics_during_period` (`period: hour`, `types: [mean, min, max, sum, state]`) or the `recorder.get_statistics` action. Only entities with a `state_class` have LTS; binary states (defrost, DHW, compressor) have **none**, so DHW/defrost intervals must be inferred (e.g. LWT spikes, energy without room heat). Output: `docs/data_inventory.md` with coverage per entity.
6. **Recommend immediately** (before the heating season): raise `recorder: purge_keep_days` (e.g. 60–120 days, check DB size) or add an InfluxDB/Postgres long-term store, and ensure key sensors (`Ti`, `To`, LWT, RWT, flow, power, energy) have `state_class: measurement/total_increasing` so they also get LTS. This determines the quality of all later learning.

Acceptance: verified mappings, data inventory, retention recommendation actioned or explicitly declined. No guessed IDs, no writes.

### M1 — Offline identification and Predheat calibration
`tools/ha_export.py`, `tools/fit_offline.py`, core `thermal_model`, `emitter_model`, `heatpump_model` (fit-only), `intervals` classifier. Fit UA, C, Q_int, K, n, COP surface on last winter's hourly LTS (coarse) plus any raw data. Hourly means can't resolve C precisely; report its uncertainty honestly and refine live.
Deliver `docs/offline_fit_report.md` and `docs/predheat_calibration.md`.
Acceptance: synthetic-data convergence tests; held-out backtest error reported; Predheat suggestions with before/after error.

### M2 — Telemetry integration + live learning (shadow, no optimiser)
Config flow, coordinator (5-min cycle), freshness/units validation, interval classification, persistence, RLS learners seeded from M1 priors, prediction sensors, diagnostics.
Acceptance: installs/unloads cleanly; predictions and errors visible; invalid data rejected.

### M3 — Shadow MPC
Thermostat model, DP optimiser, tariff-only cost, explanations, replay tool.
Acceptance: replay and ≥2 weeks of live shadow over a range of outdoor temperatures; recommendations respect comfort/offset/timing; runtime bounded; agreed error thresholds met.

### M4 — Safety-controlled writes + Predbat economics
Gateway, supervisor, readback, manual-override hold, budget handling; Active only explicitly; commissioning at ±1 °C, ≥60 min between writes. Then Predbat-aware cost provider and the optional load-forecast entity (disabled by default).
Acceptance: full safety test matrix (zero writes in off/shadow/hold, defrost, DHW, stale, budget exhausted, invalid model, fault, wrong mode; no bypass; no retry storms); documented commissioning; forecast contract verified against Predbat; no double counting.

### M5 — Advanced heating
2R2C if 1R1C residuals show it is needed, solar term, extra rooms, 24–48 h horizon, probabilistic forecasts.

### M6 — DHW (§12), as sub-PRs: telemetry/model → shadow scheduler → arbiter + safety control → combined forecast.

## 14. Initial settings

```text
mode = shadow                 dhw_mode = off (shadow after DHW commissioning)
prediction_interval = 5 min   control_interval = 60 min   sim_step = 30 min   horizon = 12 h
offset bounds = −2…+2 °C      commissioning = −1/0/+1 °C   max change per decision = 1 °C
target = 20.0 °C              soft band ±0.3 °C            hard_min = 18.0 °C   hard_max = 22.0 °C
emitter n prior = 1.3         UA change limit = 2 %/day
```
Comfort values are commissioning examples; validate ordering and keep them consistent with the room thermostat setpoint.

## 15. Testing

- Core pytest without HA: physics/units, learners on synthetic identifiable vs poorly excited data, counter resets, emitter inversion (offset → heat), thermostat gating and cycling, COP monotonicity, optimiser scenarios from §9, safety matrix.
- Mocked HA tests: setup/unload, config flow, command routing through the gateway only.
- Replay never writes; replay costs are model estimates, not measured savings.

## 16. Open items for M0

1. Thermostat type and how its demand/setpoint appear in HA (P1P2 or other).
2. Exact writable deviation entity, step size, bounds; write-budget entities.
3. Energy/thermal meter types (power vs interval vs cumulative), flow-rate availability.
4. Native weather-curve points and current deviation.
5. Recorder retention, DB backend, which sensors have `state_class`, and any external long-term store (InfluxDB etc.).
6. Predbat entity/attribute contract (tariff, SOC forecast, plan timestamp); Predheat configuration currently in `apps.yaml`.
7. DHW arrangement and native schedule (inventory only).

## References (verify before implementing)
- P1P2MQTT HA docs: https://github.com/Arnold-n/P1P2MQTT/blob/main/doc/HomeAssistant.md
- Predheat: https://springfall2008.github.io/batpred/predheat/
- Predbat: https://github.com/springfall2008/batpred
- HA REST API: https://developers.home-assistant.io/docs/api/rest/
- HA WebSocket API / statistics: https://developers.home-assistant.io/docs/api/websocket/ , https://www.home-assistant.io/actions/recorder.get_statistics/
- HA developer docs: https://developers.home-assistant.io/
