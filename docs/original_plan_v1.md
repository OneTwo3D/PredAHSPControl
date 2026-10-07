# Claude Code implementation plan — Self-learning Daikin Altherma MPC controller

Updated: 7 October 2026. Scope: space heating plus a dedicated, separately enabled domestic hot water (DHW) development stage.

This is the complete implementation brief. Retain the staged rollout: telemetry, prediction, learning, shadow recommendations, safety-controlled space heating, Predbat coordination, then DHW. DHW must not become an implicit part of the first space-heating release.

## 1. Objective

Build a Home Assistant custom integration that acts as a self-learning supervisory controller for a Daikin Altherma 3 connected through P1P2MQTT.

The controller must:

1. Maintain indoor comfort.
2. Keep the Daikin operating predominantly low-and-slow.
3. Optimise leaving-water temperature (LWT) through the native weather-dependent heating deviation.
4. Learn the building's thermal behaviour.
5. Learn heat-pump performance versus outdoor temperature and LWT.
6. Use future weather and temperature forecasts.
7. Incorporate Predbat battery, PV and tariff information.
8. Cooperate with Predbat without replacing its battery optimiser.
9. Initially use Predheat as an independent forecast/reference.
10. Eventually publish a 24–48-hour electrical-load forecast consumable by Predbat.
11. Leave native Daikin operation functional if this integration stops.
12. Add a separate DHW stage that learns cylinder behaviour and schedules permitted native DHW operation around demand, comfort and electricity economics.

Do not directly control compressor operation, defrost, backup heater operation or refrigeration functions.

Primary space-heating actuator: the installation's verified writable P1P2MQTT weather-dependent heating deviation entity. Names such as `LWT_Deviation_Heating` and `Deviation_Heating` are discovery hints, not guaranteed entity IDs. Keep the Daikin in weather-dependent LWT mode.

DHW actuators must be independently discovered and explicitly configured. Use only verified native DHW scheduling/setpoint/request interfaces; never infer writability from telemetry.

## 2. Architectural principle

Do not modify Predbat, Predheat or P1P2MQTT source. Build `custom_components/daikin_mpc/` with a testable Python core independent from Home Assistant.

Data flow:

1. HA telemetry and forecasts enter adapters.
2. State estimation feeds the building and heat-pump models.
3. Conservative learning updates bounded parameters.
4. Simulation predicts comfort, heat and electrical consumption.
5. The optimiser evaluates trajectories.
6. The safety supervisor validates each proposed action.
7. A single HA command gateway sends permitted native control requests.

Predbat supplies battery/PV/tariff information. Predheat supplies a baseline heating forecast. P1P2MQTT supplies Daikin telemetry and verified control entities. The later DHW model shares telemetry infrastructure and the safety gateway, but has separate state, training samples, forecasts and scheduling logic.

## 3. Do not duplicate Predbat

Predbat remains responsible for battery charge/discharge, PV utilisation, import/export arbitrage, reserve and tariff optimisation. Daikin MPC handles thermal comfort, efficiency, heating timing and LWT supervision; later it handles bounded DHW scheduling.

The controller must never command the inverter or battery.

Long-term interfaces:

- Daikin MPC → predicted heating and DHW electrical load → Predbat.
- Predbat → forecast energy availability and marginal-cost information → Daikin MPC.

Forecasts must have explicit generation timestamps and versions. Avoid circular feedback: consume a stable Predbat snapshot, publish a bounded updated load forecast, and rate-limit replanning. Do not repeatedly assume extra heating can consume the same forecast battery reserve.

## 4. Repository structure

```text
custom_components/daikin_mpc/
    __init__.py
    manifest.json
    config_flow.py
    const.py
    coordinator.py
    sensor.py
    binary_sensor.py
    switch.py
    number.py
    select.py
    diagnostics.py
    core/
        __init__.py
        models.py
        telemetry.py
        thermal_model.py
        heatpump_model.py
        learner.py
        forecast.py
        optimiser.py
        cost_model.py
        controller.py
        safety.py
        persistence.py
        diagnostics.py
        dhw_model.py          # dedicated DHW stage
        dhw_learner.py
        dhw_scheduler.py
        operating_arbiter.py
    adapters/
        p1p2.py
        predbat.py
        predheat.py
        weather.py
        commands.py

tests/
    test_thermal_model.py
    test_learner.py
    test_heatpump_model.py
    test_cost_model.py
    test_optimiser.py
    test_safety.py
    test_controller.py
    test_dhw_model.py
    test_dhw_scheduler.py
    test_operating_arbiter.py
    fixtures/
tools/
    replay.py
docs/
    entity_mapping.md
    installation.md
    commissioning.md
    dhw_commissioning.md
    forecast_contract.md
```

Core classes must not import Home Assistant. HA-specific services, entity access and persistence wrappers belong in the integration layer.

## 5. Entity configuration

Do not hard-code actual HA entity IDs. Provide a config/options flow for selection and validation.

Required space-heating telemetry:

- Indoor temperature.
- Outdoor temperature, from P1P2MQTT or another sensor.
- Actual LWT and return-water temperature.
- Current requested LWT.
- Current heating deviation.
- Compressor status.
- Defrost status.
- Daikin heating mode/status.

Discovery hints include `Temperature_Outside`, `Temperature_R2T_Leaving_Water`, `Temperature_R4T_Return_Water`, `LWT_Setpoint`, `Deviation_Heating`, `Compressor` and `Defrost_Active`. Verify semantics against the installed firmware and actual HA entities.

Strongly recommended: flow rate, dedicated ASHP electrical power and cumulative energy, reliable thermal production data and COP data. Determine whether each production/energy entity is power, interval energy or a cumulative counter before using it.

Optional: irradiance, additional room temperatures, windows/doors, occupancy and internal consumption.

DHW entity mappings are introduced in Stage 9. Missing DHW control capability must leave space-heating functionality usable.

## 6. Predbat / Predheat inputs

Provide configurable mappings and adapters; do not assume exact entity names or attribute schemas.

Consume where available:

- Battery SOC now and forecast.
- Import/export tariffs and future tariff series.
- PV forecast.
- Planned import and charge/discharge periods.
- Predbat load forecast and plan timestamp.
- Predheat heating-energy and room-temperature forecasts.

`predheat.heat_energy` is a discovery hint. Validate the actual format and units. Record MPC predictions, Predheat predictions and actual outcomes for comparisons. Predheat is initially a reference, not the control algorithm.

Missing optional economic inputs should degrade to a documented tariff-only provider rather than inventing a battery plan.

## 7. Control strategy

Keep native weather compensation and apply a bounded heating offset. Do not repeatedly switch the heat pump on/off. Do not automatically modify the underlying weather-compensation curve in V1.

Initial configured limits:

```text
min_offset = -2 °C
max_offset = +2 °C
max_single_change = 1 °C
```

First active commissioning should use only −1/0/+1 °C. Normally move one step per control decision. Read actual entity bounds and supported increments; reject incompatible configuration.

DHW supervision is a separate mode and does not use the heating offset to force cylinder heating.

## 8. Controller timing

Prediction cycle: every 5 minutes, ingest valid data, update state/model and forecast.

Control cycle: default 60 minutes. A cycle need not produce a write.

Write only if:

- The proposed value differs from the actual value.
- The recommendation has persisted through configured hysteresis.
- Minimum write interval has elapsed.
- P1P2 budget and safety checks permit it.

Do not consume a write to repeat the current value. Keep telemetry cadence, optimisation timestep and write cadence separate. DHW scheduling shares the same global bus budget and introduces its own actuator-specific timing limits.

## 9. Thermal model — V1

Use an interpretable 1R1C model, not a neural network:

```text
C × dTi/dt = Qheat + Qinternal + Qsolar − UA × (Ti − To)
```

Variables and units:

- `Ti`, `To`: indoor/outdoor temperature, °C.
- `UA`: building heat-loss coefficient, W/K.
- `C`: effective thermal capacity, Wh/K.
- `Qheat`, `Qinternal`, `Qsolar`: heat flows, W.
- Time in hours when using Wh/K and W.

Learn bounded `UA`, `C` and baseline internal gains. Use a stable discretisation and actual elapsed time. Model timestep must be independent of sensor sampling frequency. Clearly convert exposed `C` to kWh/K.

## 10. Thermal model — V2

After validating V1, optionally introduce 2R2C with indoor air `Ti` and effective building mass `Tm`, capacities `C_air`, `C_mass`, and resistances `R_out`, `R_mass`.

Do not implement this before validating the simpler model. Keep interchangeable interfaces:

```python
ThermalModel.predict(...)
ThermalModel.update(...)
ThermalModel.serialize()
ThermalModel.deserialize(...)
```

Version model representations and support migration or a safe reset.

## 11. Heat input estimation

Preferred hierarchy:

1. Reliable thermal-production measurement. Convert cumulative counters to interval heat with reset handling.
2. Hydronic calculation: `Q = mass_flow × Cp × (LWT − RWT)`, with explicit flow units and configured fluid properties.
3. Electrical estimate: `heat_output = electrical_power × estimated_COP`.

Mark source and confidence. Do not silently mix incompatible methods. Avoid circular learning: heat estimated from an assumed COP cannot independently establish a measured COP. Do not assign DHW heat to the building model.

## 12. Heat-pump efficiency model

Learn a bounded surface `COP = f(outdoor_temperature, LWT)` using binned interpolation, constrained regression or a manufacturer-inspired model. Avoid overfitting and heavy dependencies.

Within the validated operating envelope, enforce physically reasonable trends: higher LWT should not produce implausibly improved COP, and colder outdoor conditions should generally reduce it. Flag extrapolation and uncertainty.

Exclude or flag defrost, DHW, backup heater operation, startup transients and invalid sensors. Later train a separate DHW performance model.

COP alone is insufficient for MPC: also identify a bounded model of useful heating output versus requested/actual LWT, outdoor temperature and operating state. Validate that changing offset produces plausible heat-output and electrical-load changes. Do not assume an offset produces an arbitrary fixed amount of heat.

Expose estimated COP and heat output with provenance.

## 13. Self-learning

Use recursive least squares, exponentially weighted regression or bounded rolling least squares. Persist parameters across restarts.

Store value, uncertainty, useful-observation count, update time and bounds. Limit update rates; for example, configurable small percentage changes in `UA` per day. Reject implausible updates and retain the last validated model.

Assess parameter identifiability and excitation. Do not report high confidence merely because many near-identical samples exist. Distinguish parameter confidence from forecast error and calibrate any percentage confidence against held-out outcomes.

## 14. Training-data filtering

Exclude or down-weight DHW, defrost, backup heater, stale/unavailable data, restart gaps, large missing intervals, manual interventions, abnormal configured door/window events and temperature sensor anomalies.

Classify intervals as clean heating, clean cooling, disturbed or invalid. Expose counts and exclusion reasons. Align samples in time and require enough valid interval coverage before updating a model.

## 15. Solar gains

V1: adaptive residual internal/solar gains.

V2: optionally learn `Qsolar = irradiance × learned_gain`, or use PV power as a coarse proxy with explicit limitations. Solar modelling is optional and must not cause the model to assign all heating error to sunlight.

## 16. Weather forecast

Use the supported current HA forecast interface and interpolate hourly data onto simulation slots. Begin with a 24-hour available forecast; support 48 hours where supplied.

If unavailable, temporarily use the last valid short-term forecast, reduce confidence and suspend economic MPC after configured expiry. Never invent weather indefinitely. Preserve timestamps, timezone and forecast age.

## 17. Temperature target

Support fixed target, thermostat target entity, schedule and comfort band. Represent desired, soft minimum/maximum and hard minimum/maximum explicitly.

Example configuration, not universal limits:

```text
target = 20.0 °C
soft_min = 19.7 °C
soft_max = 20.3 °C
hard_min = 18.5 °C
hard_max = 22.0 °C
```

Soft limits influence the objective; hard limits are constraints subject to the limitations of the predictive model and available heating capacity. Validate ordering and schedule transitions.

## 18. MPC optimiser

Initial simulation timestep: 30 minutes. Initial horizon: 12 hours; expand to 24 hours after performance validation.

At each run:

1. Snapshot state and forecast versions.
2. Generate maintain/−1/+1 relative-offset trajectories.
3. Simulate indoor temperature and useful heat.
4. Predict electrical consumption.
5. Calculate energy cost and comfort penalties.
6. Calculate efficiency and movement penalties.
7. Reject infeasible trajectories.
8. Choose the best feasible trajectory.
9. Apply only its first permitted action.
10. Recompute on the next cycle.

Candidate action times must respect the real 60-minute write interval even though simulation slots are 30 minutes. Report infeasibility explicitly; do not claim the optimiser can guarantee comfort against insufficient plant capacity.

## 19. Optimisation method

Use discrete beam search before adopting large optimisation libraries. A 12-hour horizon contains 24 half-hour slots; a 24-hour horizon contains 48.

Initial configurable beam width: 50–200. Retain the best N feasible partial trajectories per step. Include actuator timing and relevant model state in each node.

Run outside the HA event loop. Discard stale results when a newer state/configuration supersedes them; provide bounded cancellation or generation checks and runtime limits. Measure execution time.

## 20. Objective function

```text
J = energy_cost
  + comfort_penalty
  + high_LWT_penalty
  + cycling_penalty
  + control_change_penalty
  + battery_depletion_penalty
```

Make weights configurable and document scaling/units. Penalise cold discomfort more heavily than equal warm deviation by default. Penalise unnecessarily high LWT, frequent movement and predicted cycling only where the model can support that prediction.

Hard constraints must not be traded away using only a finite penalty. Avoid double-counting battery scarcity if it already appears in marginal cost.

## 21. Energy-cost model

Create an `EnergyCostProvider` abstraction, initially tariff-only, later Predbat-aware:

```python
marginal_cost(timestamp, incremental_kwh)
energy_source(timestamp)
battery_constraint(timestamp)
```

Account for grid, battery, PV and foregone export. Predbat remains authoritative on battery behaviour.

A battery's historical purchase price is not automatically its present marginal value. Include relevant losses, power/capacity constraints, reserve, foregone export and the extra future import caused by incremental heating. Forecast SOC alone is insufficient to establish an unlimited cheap energy supply.

Use verified plan data or bounded incremental-load scenarios. If reliable marginal valuation cannot be derived, expose a conservative approximation and its uncertainty rather than presenting it as exact.

## 22. Important tariff principle

Test this scenario: overnight import 7p/kWh, daytime import 30p/kWh, battery charged overnight.

When the battery can cover daytime heating within its constraints, do not automatically overheat overnight solely because the raw daytime tariff is high. Compare low-LWT daytime operation supplied by the battery with thermal precharge, including COP, losses, export opportunity and battery capacity.

Preheating may still be useful where battery capacity/power is insufficient or later marginal energy is more expensive.

## 23. Battery-aware comfort shifting

Optionally allow a bounded drift toward `soft_min` if Predbat predicts scarcity and expensive imports. Maintain normal comfort when supply is adequate. Never intentionally optimise below configured hard limits.

Make economic comfort shifting opt-in, explain it and do not silently rewrite the user's thermostat schedule.

## 24. PV-aware heating

Permit modest preheating when it is better than export or other energy use. Compare foregone export, storage losses, COP and future heat need. Do not blindly convert surplus PV into heat.

Initial optional preheat cap: approximately +0.3 to +0.5 °C above the normal target, always inside the configured upper bound.

## 25. Predheat integration

Keep Predheat enabled initially. Compare Predheat/MPC predicted heating electricity and temperature against actual outcomes. Expose MAE, bias and 1/6/24-hour errors where data supports them.

Do not replace Predheat's Predbat forecast until the MPC model demonstrates sufficient accuracy. Ensure comparisons use the same forecast issue time, horizon and units.

## 26. Predbat forecast output — later phase

Publish an optional entity such as `sensor.daikin_mpc_heat_energy`. Verify the exact upstream external-load contract before implementing its attributes: timestamp format, cumulative versus interval energy, starting baseline, units and timezone.

Illustrative configuration only, subject to verified adapter contract:

```yaml
load_forecast:
  - sensor.daikin_mpc_heat_energy$forecast
```

Do not change Predbat configuration automatically. Document switching from Predheat and avoiding duplicate forecast entries.

At the DHW stage provide separate heating, DHW and combined forecasts. The combined series must sum electrical loads once and account for native operation priority. Check whether baseline house load already includes the ASHP to avoid double-counting.

## 27. Shadow mode

Default mode is Shadow: ingest, learn, forecast, optimise and publish recommendations with zero Daikin writes.

Expose recommended offset and structured decision reason. Enable Active only explicitly; elapsed time or sufficient sample count must never enable it automatically.

DHW has its own independent shadow/active enablement. Space-heating Active does not authorise DHW writes.

## 28. Active modes

- **Off:** no optimisation.
- **Shadow:** optimisation, no writes.
- **Active:** optimisation plus safety-permitted writes.
- **Hold:** retain the actual current offset; no changes.

Learning can be separately enabled where valid. Clearly define mode changes and optional bounded restoration. Default DHW mode is disabled until its stage is commissioned, then Shadow.

## 29. Safety supervisor

All actuator requests pass through `SafetySupervisor` and one command gateway. No other module may call a service that controls the Daikin.

Validate active permission, entity availability/freshness, correct native mode, no fault, no defrost, relevant DHW state, offset/setpoint bounds, step size, minimum interval, available write budget, model validity/confidence and difference from actual state.

Return allowed/rejected plus a structured reason. Revalidate immediately before sending, not only during optimisation. A service-call success is not confirmed actuation: wait for authoritative readback, classify timeout/refusal, and do not retry aggressively.

DHW validation adds independent permission, capability, bounds, native hygiene precedence and mutually compatible operating requests.

## 30. P1P2 write budget

Discover and verify semantics of budget telemetry, including possible `Write_Budget`, `Write_Budget_Period`, `Writes_Refused_Budget` and `Writes_Refused_Busy` entities.

Rules:

1. Never write when budget is exhausted.
2. Never repeat an unchanged value.
3. Default to at most one supervisory control change per hour across heating and DHW.
4. Warn when budget-refusal counters increase.
5. Temporarily suspend writes on repeated busy refusals.
6. Never automatically increase the budget.
7. If budget telemetry is unavailable, fail closed for Active commissioning unless a separately verified conservative policy is explicitly configured.

Reserve budget for any explicitly configured restoration, and never assume it will remain available during an outage.

## 31. Defrost handling

Never request, suppress or interrupt defrost. Block writes during defrost and recovery. Exclude affected data from steady-state COP learning.

Forecasts should eventually include observed defrost energy/heat penalties rather than ignoring their contribution to daily consumption. Resume only after a configurable recovery interval and valid telemetry.

## 32. DHW handling and staged introduction

Space-heating V1 detects DHW and excludes cylinder heat from building/COP training. It does not optimise DHW.

Implement dedicated DHW functionality as Stage 9, detailed in Section 50. Keep separate cylinder model, demand forecast, COP/performance model and permission. The operating arbiter accounts for DHW interruptions to space heating and shares the P1P2 budget.

Do not mix DHW and space-heating heat or electricity measurements. Unattributed intervals remain uncertain rather than being guessed.

## 33. Backup heater

Never deliberately command backup/immersion heater operation or alter installer heater settings. When detected, exclude samples from ordinary heat-pump COP learning, record separately in energy accounting and publish a warning.

The supervisor can limit its requests but cannot guarantee the native Daikin will never autonomously use a heater. Preserve native protection and hygiene behaviour.

## 34. Failure behaviour

Native heating must remain functional without MPC. Bounded offset supervision leaves the underlying WC intact, but a crash can leave the last applied offset or DHW setpoint in place; it does not automatically reset to zero.

On orderly disable, optionally restore a verified commissioning baseline through the same gateway if budget and safety permit. On crash, issue no new commands. Default changes must remain acceptable if retained indefinitely.

If a stronger timeout/reset guarantee is required, it needs an independently configured and tested native schedule or external watchdog. Document this limitation rather than claiming automatic fallback.

DHW commissioning must retain an independently functioning native schedule/reheat policy.

## 35. Home Assistant entities

Create UI entities with unique IDs and appropriate units/device classes:

```text
select.daikin_mpc_mode
switch.daikin_mpc_learning
number.daikin_mpc_target_temperature
number.daikin_mpc_soft_min
number.daikin_mpc_soft_max
number.daikin_mpc_hard_min
number.daikin_mpc_hard_max
number.daikin_mpc_max_positive_offset
number.daikin_mpc_max_negative_offset
number.daikin_mpc_min_write_interval
sensor.daikin_mpc_status
sensor.daikin_mpc_recommended_offset
sensor.daikin_mpc_current_offset
sensor.daikin_mpc_decision_reason
sensor.daikin_mpc_model_confidence
sensor.daikin_mpc_heat_loss_coefficient       # W/K
sensor.daikin_mpc_thermal_capacity           # kWh/K
sensor.daikin_mpc_internal_gain             # W
sensor.daikin_mpc_temperature_1h
sensor.daikin_mpc_temperature_3h
sensor.daikin_mpc_temperature_6h
sensor.daikin_mpc_energy_24h
sensor.daikin_mpc_cost_24h
sensor.daikin_mpc_estimated_cop
sensor.daikin_mpc_estimated_heat_output
sensor.daikin_mpc_last_decision
sensor.daikin_mpc_last_model_update
sensor.daikin_mpc_prediction_error
sensor.daikin_mpc_training_samples
```

These are proposed integration-owned IDs, not assumed existing entities. Additional DHW controls/diagnostics appear in Section 50.

## 36. Explainability

Every decision provides machine-readable components plus a concise readable reason. Include actual/proposed offset, target and predicted room temperatures under alternatives, outside temperature, LWT, battery-plan timestamp, forecast SOC, energy source/cost, uncertainty and safety result.

Example: recommend +1 °C because the current trajectory predicts 19.4 °C by 17:30 against a 20 °C target; alternative predicts 19.9 °C. Report whether peak import is expected and why.

Do not invent a numeric confidence such as 86% without a calibrated definition. DHW decisions similarly explain readiness deadline, predicted cylinder state, selected heating slot and impact on room comfort.

## 37. Diagnostics

Implement diagnostics containing version, configured entity mappings, parameter values/bounds/uncertainties, recent forecasts, optimisation results, safety results, write budget and prediction-error metrics.

Redact credentials, keys, addresses and tariff account identifiers. Add DHW capability mappings, demand-profile confidence, scheduling outcomes and native hygiene-state observations when available.

## 38. Event log

Keep bounded rolling decision history with timestamp, mode, indoor/outdoor/target temperatures, actual/proposed/applied offset, predicted 3-hour temperature, SOC and forecast SOC, marginal cost, confidence, reason and safety result.

Track command attempt, acknowledgement and confirmed readback distinctly. Optionally emit `daikin_mpc_decision` events. Later add cylinder state, DHW action, deadline and operating arbitration results. Keep retention configurable and avoid unlimited HA attribute growth.

## 39. Persistence

Use HA storage wrappers and JSON-compatible versioned core state; never pickle.

Persist parameters, uncertainty, training counters, COP/output models, last safe/confirmed actuator values and recent error statistics. Store relevant write timing conservatively across restarts.

DHW adds cylinder parameters, demand-profile statistics, validated baseline settings and scheduler state. Implement migrations and safe rejection of corrupt/non-finite/out-of-bounds data.

## 40. Startup

Load and validate persisted state, read entities, verify freshness and start passively. Produce forecasts only with complete valid state. Do not immediately write.

Initially restart into Shadow. Restore Active only if explicitly configured, after startup gating. Manual changes detected outside this controller must trigger a defined hold/cooldown policy, not be immediately overwritten.

DHW requires separate startup gating and must not replay a stale pending heating request after restart.

## 41. Testing strategy

Use pytest for core tests without real HA or Daikin hardware. Add mocked HA integration tests separately for setup/unload, config flow and command routing; never require a live installation.

Thermal tests: colder outside cools the house, heat raises temperature, higher UA increases loss, higher C slows response, correct timestep/units.

Learning tests: synthetic identifiable datasets with known UA/C/gains converge; poorly excited/noisy data cannot falsely produce high confidence; outliers, missing intervals and counter resets are handled.

Heat-pump tests: plausible COP trends, bounded extrapolation, output response to LWT, correct operating-state exclusions.

Optimisation scenarios:

- Adequate cheap battery energy: no unnecessary overnight preheat.
- Battery exhaustion before expensive peak: modest precharge only if worthwhile.
- Midday PV surplus: optional bounded preheat.
- Comfort limit: comfort overrides economic saving within available plant capability.
- Shared reserve: candidate loads cannot reuse the same available battery energy indefinitely.

Safety: zero writes in Shadow/Off/Hold, defrost, stale telemetry, exhausted budget, invalid model, fault or incompatible mode; no gateway bypass; refused/unconfirmed writes do not provoke retries.

DHW tests are specified in Section 50.

## 42. Replay testing

Build an offline CSV/JSON replay tool for indoor/outdoor temperatures, LWT/RWT, flow, electrical/thermal energy, compressor/defrost/DHW/heater states, tariffs, SOC and PV. Replay never writes.

Report model fit, predicted versus actual temperature/energy, proposed offsets, estimated cost and comfort deviations. Add cylinder temperatures/draw events/native DHW schedule in the DHW stage.

Recorded telemetry evaluates predictions under historical actions; it cannot establish actual savings for hypothetical actions. Counterfactual control comparisons require a separately validated simulation and uncertainty reporting. Avoid presenting replay cost as measured savings.

## 43. Development phases

### Phase 0 — Discovery

Inspect the existing repository and actual HA entity export/installation. Identify Predbat, Predheat and P1P2 mappings, telemetry units, writable capability, WC mode, budgets and native settings. Document in `docs/entity_mapping.md`. Inventory DHW capabilities without enabling DHW control.

Acceptance: verified mappings and explicit unresolved capability list; no guessed entity IDs.

### Phase 1 — Telemetry integration

Implement config flow, mappings, coordinator, validation and diagnostics. No learning or control.

Acceptance: necessary data visible reliably, freshness and unit errors surfaced.

### Phase 2 — Fixed thermal model

Implement bounded 1R1C with configured UA/C and temperature forecasts. No learning or writes.

Acceptance: forecasts work in Shadow and core physical/unit tests pass.

### Phase 3 — Model learning

Estimate UA, C and baseline gains with uncertainty and update bounds.

Acceptance: identifiable synthetic data converges; replay behaves sensibly; invalid data is rejected.

### Phase 4 — Heat-pump model

Implement thermal-output and electricity/COP models, excluding DHW/defrost/heater intervals from ordinary learning.

Acceptance: physically reasonable output/COP predictions and documented uncertainty.

### Phase 5 — Shadow MPC

Implement simulation, beam search, explanations and recommended offset. No writes.

Acceptance: replay/live recommendations respect comfort, offset and future timing constraints; runtime is bounded.

### Phase 6 — Safety-controlled space-heating writes

Implement single gateway, SafetySupervisor, readback, manual override and budget handling. Enable writes only with explicit Active mode.

Initial limits: −1/0/+1 °C, at least 60 minutes between writes.

Acceptance: comprehensive safety tests, successful shadow validation and documented commissioning; no bypass paths.

### Phase 7 — Predbat-aware cost model

Integrate verified tariffs, battery/PV and plan snapshots. Do not command or replace Predbat.

Acceptance: marginal valuation respects losses, scarcity and incremental load; approximation confidence is exposed.

### Phase 8 — Publish heating forecast

Publish optional verified Predbat-compatible heating electrical forecast. Disabled by default.

Acceptance: correct upstream schema and manual consumption verified, no baseline/Predheat double-counting.

### Phase 9 — Dedicated DHW model and optimiser

Implement Section 50 incrementally: capability discovery, cylinder telemetry/model, demand learning, shadow scheduling, operating arbitration, safety-controlled native requests, combined electrical forecast.

Acceptance: separate opt-in, independent model and safety tests, native hygiene/protection retained, successful shadow validation and working native fallback.

### Phase 10 — Advanced features

Only after stable operation: 2R2C, explicit solar gains, multiple rooms, occupancy, adaptive comfort bands, probabilistic forecasts and extended horizons. DHW is already a dedicated Phase 9 deliverable, not an unspecified future feature.

## 44. Initial conservative settings

```text
mode = shadow
prediction_interval = 5 minutes
control_interval = 60 minutes
optimisation_timestep = 30 minutes
horizon = 12 hours initially
min_offset = -2 °C
max_offset = +2 °C
max_change_per_decision = 1 °C
target = 20.0 °C
soft_band = ±0.3 °C
hard_min = 18.0 °C        # configurable commissioning example
first_active_offsets = {-1, 0, +1} °C
dhw_enabled = false
dhw_mode_on_commissioning = shadow
```

Validate all important settings. DHW temperature limits, baseline target and hygiene settings must come from verified existing installation requirements, not generic invented defaults.

## 45. Coding standards

Use type hints, dataclasses, enums, structured logging, pytest, ruff and mypy where practical. Use asyncio correctly in HA and a pure synchronous core where feasible.

Avoid global mutable state, hidden constants, magic IDs, direct MQTT writes, modifications to upstream projects, heavy ML frameworks and cloud dependencies. Public interfaces need docstrings and explicit units/time semantics.

Use the current supported HA lifecycle, config entry, executor, forecast, storage and service interfaces after inspecting upstream documentation.

## 46. Git strategy

One coherent phase per branch/PR, with relevant tests:

```text
feat/telemetry
feat/thermal-model
feat/self-learning
feat/heatpump-model
feat/mpc-shadow
feat/safety-control
feat/predbat-cost
feat/predbat-load-forecast
feat/dhw-telemetry-model
feat/dhw-shadow-scheduler
feat/dhw-safety-control
feat/dhw-combined-forecast
```

Do not combine initial learning, optimisation and real control in one large commit. Split Phase 9 into these DHW submilestones.

## 47. Absolute safety requirements

Never disable native safety, alter refrigeration controls, force compressor operation, suppress/request defrost, change installer field settings, force backup/immersion heaters, increase P1P2 budget, retry repeatedly to overcome refusals or make basic heating depend on MPC.

Do not disable, weaken, skip or economically postpone native DHW hygiene/protection cycles. Do not claim a single cylinder sensor proves a hygiene cycle has completed. Existing installer/native safeguards remain authoritative.

Native Daikin heating and DHW must remain usable if MPC stops. No direct control should be enabled without verified writable interfaces and explicit per-function permission.

## 48. Definition of done

### Space-heating V1

1. Installs and unloads cleanly in HA.
2. UI mappings for P1P2 and optional Predbat/Predheat.
3. Building parameters self-calibrate with uncertainty.
4. Temperature and electrical-load forecasts work.
5. Shadow MPC produces explainable recommendations.
6. Comprehensive safety tests and one command gateway.
7. Active can conservatively change verified WC deviation.
8. Budget, freshness, defrost, DHW and faults are respected.
9. Persistence and replay work.
10. Optional Predbat load export has a tested contract.
11. Native operation remains functional with retained bounded settings.

### DHW stage completion

1. DHW telemetry and verified capabilities documented.
2. Separate cylinder/demand/performance models persisted.
3. Uncertainty-aware readiness forecasting and shadow scheduling work.
4. Shared budget and space-heating/DHW arbitration work.
5. Independent explicit DHW Active permission required.
6. Hygiene, protection, native priority and baseline fallback preserved.
7. Command/readback and manual-override handling tested.
8. Separate and combined forecasts avoid double-counting.
9. Offline and live shadow acceptance criteria in Section 50 pass.

## 49. Instructions to Claude Code

Before each phase inspect the repository, tests, affected interfaces and current upstream contracts. Implement the smallest coherent increment. After each phase run tests/linting and report changed files, validation and limitations.

Do not enable real heating writes before shadow validation and the safety supervisor are complete. Do not enable DHW writes merely because heating is Active.

Never invent entity IDs, forecast formats, operating-state meanings or writable capabilities. Require explicit selection of verified writable entities. If installation details are missing, complete pure-core work, mocked adapters and discovery documentation while listing the exact information needed for live commissioning.

First milestone: reliable telemetry, accurate self-learning prediction and safe Shadow recommendations. Later milestones add controlled heating, battery-aware economics and independently commissioned DHW.

Upstream starting points supplied with the original brief; verify current source before implementing:

- P1P2MQTT: https://github.com/Arnold-n/P1P2MQTT/blob/main/doc/HomeAssistant.md
- Predheat: https://springfall2008.github.io/batpred/predheat/
- Predbat: https://github.com/springfall2008/batpred
- Home Assistant developer documentation: https://developers.home-assistant.io/

## 50. Phase 9 specification — Self-learning DHW supervision

### 50.1 Objective and scope

Provide enough usable hot water at configured readiness deadlines while reducing electrical cost and avoiding unnecessary cylinder reheating. Use the same verified Predbat economic provider as space heating and coordinate native DHW priority with room comfort.

This is a supervisory scheduler, not a replacement for Daikin cylinder controls. Preserve native hysteresis, protection, cycle termination, heater decisions and hygiene policy. Do not require the controller for baseline hot-water availability.

### 50.2 Discovery and capability gate

Document actual indoor unit/cylinder arrangement, sensor positions, cylinder volume if known, native DHW mode, baseline schedule/reheat policy, target bounds, available scheduling/request interfaces and cancellation/readback semantics.

Required for meaningful DHW modelling:

- Cylinder temperature and timestamp.
- Actual DHW operating state.
- Existing DHW target and native mode.
- Electrical consumption with suitable resolution, or clearly labelled uncertain estimates.
- Outside temperature and known compressor/defrost/heater state.

Recommended: separate thermal production attributable to DHW, multiple cylinder temperatures, DHW flow/draw sensor and native cycle start/end states.

If the installation lacks a safe supported request/scheduling interface, implement recommendations and forecasts only. Setpoint changes must not be used as an undocumented substitute for a native start command.

### 50.3 Cylinder model

Start with a bounded lumped model:

```text
C_tank × dTtank/dt = Qdhw − UA_tank × (Ttank − Tambient) − Qdraw
```

Use explicit Wh/K, W and elapsed-hour units. Distinguish measured sensor temperature from average cylinder temperature and usable hot-water availability. One sensor does not reveal full stratification or draw volume.

Learn standing loss, effective capacity where identifiable, cycle heating rate and recovery time. Use manufacturer/volume-informed priors when supplied, record uncertainty and do not pretend to identify missing parameters from inadequate measurements.

Introduce a multi-node stratified model only after the simpler model demonstrably fails and additional data supports it. Keep DHW performance separate from space-heating COP.

### 50.4 Draw and demand learning

Build a bounded weekday/weekend time-of-day demand profile from measured draw data or confidently identified temperature drops. With temperature alone, report inferred events and uncertain draw magnitude; separate sensor anomalies, mixing and reheating transients.

Support user-defined readiness windows, minimum availability, away mode and an explicit demand/boost request using only supported native functionality. User schedules outrank inferred habits. Unexpected demand updates the forecast without immediately making large parameter changes.

Do not assume forecast use is guaranteed or suppress all native reheat because a statistical profile predicts no demand.

### 50.5 Scheduling

Plan candidate native DHW windows over 24 hours, with 30-minute forecast slots and actuator timing appropriate to the verified interface.

For each candidate estimate:

- Cylinder temperature/usable availability at readiness deadlines.
- Heating duration and electrical consumption.
- Predbat-consistent marginal cost and energy constraints.
- Standing losses from early heating.
- Space-heating interruption and room-temperature effect.
- Uncertainty margin and native scheduled events.

Prefer a cost-effective feasible slot rather than heating at every cheapest-price interval. Compare daytime PV/battery supply with overnight heating using marginal value and storage losses. Maintain sufficient readiness margin; once a native cycle begins, do not repeatedly reschedule or interrupt it for a price change.

Do not invent a universal tank target, safety temperature or hygiene frequency. Preserve the verified commissioned policy and allow optimisation only within its explicitly approved bounds.

### 50.6 Operating arbiter

Create one arbiter for space-heating and DHW proposals. It must respect native mode/priority and cannot force simultaneous operation if the plant does not support it.

During native DHW, pause space-heating writes/steady-state training as appropriate and simulate the interruption. Prevent alternating heating/DHW requests, reserve the shared budget conservatively and avoid contradictory commands.

If a DHW deadline and room comfort cannot both be satisfied, report infeasibility, retain native protection and use a documented user-configured priority. Do not silently sacrifice either hard requirement.

### 50.7 DHW safety and failure handling

Every DHW write passes the existing supervisor and command gateway, with:

- Explicit DHW Active permission.
- Fresh valid cylinder and operating telemetry.
- Verified actuator mode, bounds and service semantics.
- No fault/defrost or incompatible native operating state.
- Shared write budget and actuator-specific dwell/step limits.
- Native hygiene/protection precedence.
- No unresolved prior command or manual override.
- Confirmed readback and bounded timeout handling.

Unknown hygiene state must never be interpreted as permission to bypass the native policy. Do not alter hygiene schedules or infer successful disinfection from the controller's thermal model.

On missing data, loss of forecast or integration failure, leave native baseline DHW schedule/reheat functional. A retained adjusted target must remain within the independently acceptable commissioned range. Restore a baseline only on orderly disable when verified and safely possible.

### 50.8 HA entities and diagnostics

Add proposed integration-owned entities:

```text
select.daikin_mpc_dhw_mode                   # off/shadow/active/hold
switch.daikin_mpc_dhw_learning
sensor.daikin_mpc_dhw_status
sensor.daikin_mpc_dhw_temperature_forecast
sensor.daikin_mpc_dhw_next_recommended_start
sensor.daikin_mpc_dhw_ready_by
sensor.daikin_mpc_dhw_readiness_confidence
sensor.daikin_mpc_dhw_energy_24h
sensor.daikin_mpc_dhw_cost_24h
sensor.daikin_mpc_dhw_standing_loss
sensor.daikin_mpc_dhw_recovery_time
sensor.daikin_mpc_dhw_decision_reason
sensor.daikin_mpc_total_ashp_energy_24h
```

Provide UI readiness schedules and approved target bounds. Expose observed native DHW/hygiene state only when authoritative telemetry exists. Explain why a slot was chosen, predicted readiness, forecast uncertainty, room-comfort impact and rejected alternatives.

### 50.9 Forecast output

Publish separate heating/DHW and combined electrical series with a common timestamp grid and version. The combined forecast accounts for displaced space heating and subsequent recovery, not merely two unrelated forecasts added without arbitration.

Use only the verified Predbat external-load contract. Document how users replace a heating-only source with the combined source and avoid simultaneous inclusion of combined plus component loads.

### 50.10 Tests and acceptance

Required tests:

1. Standing-loss model cools correctly; units and elapsed time are correct.
2. Clean cycles improve recovery predictions; DHW heat never trains the building model.
3. Inferred draw uncertainty is wider when only one temperature sensor exists.
4. Morning/evening readiness schedules survive cheap-price periods at unsuitable times.
5. PV/battery scenarios use capacity-constrained marginal values.
6. Early heating includes standing losses.
7. DHW interruptions are included in room-temperature forecasts.
8. No writes occur in DHW Shadow, even if heating is Active.
9. Hygiene/protection events cannot be skipped or overridden economically.
10. Shared budget prevents simultaneous/rapid conflicting requests.
11. Faults, stale data, manual override and unconfirmed/refused commands block further writes.
12. Restart does not replay stale requests or re-enable DHW Active by default.
13. Native baseline settings remain usable when the integration stops.
14. Separate/combined energy forecasts have no double-counting.
15. Unsupported DHW control interfaces leave the integration recommendation-only.

Commissioning sequence:

- Record baseline native DHW settings and representative historical cycles/demand.
- Run telemetry/model learning and replay without writes.
- Run live DHW Shadow through representative readiness windows and operating conditions.
- Agree installation-specific prediction-error/readiness acceptance thresholds; sample count alone is insufficient.
- Verify native fallback, actuator readback, hygiene precedence and shared-budget arbitration.
- Require explicit DHW Active enablement, then commission conservatively with limited scheduling changes.

Phase 9 is complete only when readiness forecasting, native-safe scheduling, separate permission, shared arbitration, tested failure behaviour and combined-load publication work as specified.
