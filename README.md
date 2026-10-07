# Daikin MPC (PredAHSPControl)

Self-learning supervisory controller for a Daikin Altherma 3 (via P1P2MQTT), coordinated with
Predbat. Built in stages; **the current release (0.2, milestone M2) is observation-only**: it reads
telemetry, learns the building and heat-pump behaviour and forecasts room temperature and heating
electricity. It contains no code that writes to the heat pump.

- Plan: [`docs/implementation_plan.md`](docs/implementation_plan.md)
- Installation and first checks: [`docs/installation.md`](docs/installation.md)
- Verified entity mapping: [`docs/entity_mapping.md`](docs/entity_mapping.md)
- Offline analysis and Predheat calibration: [`docs/offline_fit_report.md`](docs/offline_fit_report.md),
  [`docs/predheat_replay_report.md`](docs/predheat_replay_report.md),
  [`docs/predheat_calibration.md`](docs/predheat_calibration.md)

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/mypy          # core, no HA needed
pip install pytest-homeassistant-custom-component                          # separate venv recommended
pytest -q tests_ha -o asyncio_mode=auto                                    # integration tests
```
