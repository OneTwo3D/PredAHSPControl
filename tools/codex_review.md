# Adversarial Codex review — how to run

Prerequisite: `OPENAI_API_KEY` set in the environment. Install with `npm i -g @openai/codex`.

```bash
codex exec --sandbox read-only --skip-git-repo-check "$(sed -n '/^## Prompt/,$p' tools/codex_review.md | tail -n +2)" \
  > review_codex.md
```

Then check every finding against the code (reproduce with a test where possible), fix the confirmed bugs
(tests + ruff + mypy green, bump version, CHANGELOG, release), and report design-level points to the owner.
Never commit `data/`, tokens or raw HA exports.

## Prompt

You are an adversarial reviewer for this repository: a Home Assistant custom integration `daikin_mpc`
(shadow-mode model-predictive control for a Daikin Altherma 3 heat pump with radiators, Predbat battery
tariffs) plus offline tools. Try hard to break it. Read `docs/implementation_plan.md` for intent, then
review milestones M1–M3:

1. `custom_components/daikin_mpc/core/` — thermal/emitter/heat-pump models, `predictor.py`,
   `learner.py`, `cop_learner.py`, `accumulator.py`, `telemetry.py` (validation, staleness, heartbeat),
   `cost_model.py` (Predbat change-point series, timezones/DST, fallback tariff, battery/export marginal
   price), `optimiser.py` (DP with state merging: is merging sound? hard 20–22 °C limits, comfort targets,
   horizon/forecast length mismatches), `engine.py`.
2. HA layer — `coordinator.py`, `config_flow.py` (user/reconfigure/options), `sensor.py`,
   `diagnostics.py`, `__init__.py`: startup ordering (entities unavailable at boot), blocking I/O in the
   event loop, exceptions escaping, persistence/migration of stored state, unit handling, stale data.
3. `tools/` and `.github/workflows/ci.yml`: correctness of the offline fit/replay; read-only guarantee
   of `tools/ha_client.py`; token leakage.

Look for: wrong physics or units, sign errors, off-by-one in time steps/slots, DST and timezone bugs
(Europe/London), division by zero/NaN propagation, states that silently disable learning, optimiser
plans that violate comfort limits, cost errors (pence vs £, kWh vs W), race conditions, security issues.

Output a list ranked by severity. For each: file:line, one-sentence defect, a concrete failing input or
scenario, and the suggested fix. Mark each CONFIRMED (you traced it) or SUSPECTED. No style nits.
