# Offline tools (read-only)

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
export HA_URL=https://homeassistant.example.org HA_TOKEN_FILE=~/ha_token   # never commit tokens
.venv/bin/python tools/ha_export.py --start 2025-09-01 --end 2026-05-15 --out data/lts_hour.csv
.venv/bin/python tools/fit_offline.py        # -> docs/offline_fit_report.md, data/offline_fit.json
.venv/bin/python tools/predheat_replay.py    # -> docs/predheat_replay_report.md
.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/mypy
```

`ha_client.py` only sends allow-listed read-only WebSocket commands and REST GETs. Exported data stays
in `data/` (git-ignored); reports contain aggregates only. Entity roles: `tools/entities.json`;
current Predheat settings: `tools/predheat_current.json`.
