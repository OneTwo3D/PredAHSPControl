"""Export hourly long-term statistics from Home Assistant to CSV (read-only).

Usage::

    HA_URL=https://... HA_TOKEN_FILE=/path/token python tools/ha_export.py \
        --start 2025-09-01 --end 2026-05-15 --out data/lts_hour.csv

Output columns: ``time`` (UTC ISO), ``key`` (role from tools/entities.json), ``statistic_id``,
``mean``, ``min``, ``max``, ``sum``, ``state``. Exports contain household data: keep them out of git
(``data/`` is ignored).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ha_client import ws_calls

FIELDS = ("mean", "min", "max", "sum", "state")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="start date (local ISO date or datetime)")
    ap.add_argument("--end", required=True)
    ap.add_argument("--period", default="hour", choices=["5minute", "hour", "day"])
    ap.add_argument("--entities", default=str(Path(__file__).with_name("entities.json")))
    ap.add_argument("--out", default="data/lts_hour.csv")
    a = ap.parse_args(argv)

    roles: dict[str, str] = json.loads(Path(a.entities).read_text())["statistics"]
    to_utc = lambda s: datetime.fromisoformat(s).astimezone(UTC).isoformat()  # noqa: E731
    msg = {
        "type": "recorder/statistics_during_period",
        "start_time": to_utc(a.start),
        "end_time": to_utc(a.end),
        "period": a.period,
        "statistic_ids": sorted(set(roles.values())),
        "types": list(FIELDS),
    }
    (result,) = ws_calls([msg])
    by_id = {sid: key for key, sid in roles.items()}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "key", "statistic_id", *FIELDS])
        for sid, points in result.items():
            for p in points:
                t = datetime.fromtimestamp(p["start"] / 1000, UTC).isoformat()
                w.writerow([t, by_id.get(sid, sid), sid, *(p.get(f) for f in FIELDS)])
                rows += 1
    missing = sorted(set(roles.values()) - set(result))
    print(f"wrote {rows} rows for {len(result)} statistics to {out}")
    if missing:
        print("no long-term statistics for:", ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
