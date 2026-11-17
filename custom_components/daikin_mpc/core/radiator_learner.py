"""Online estimate of the radiator constant ``K`` in ``Q = K · (MWT − Ti)^n``.

Learned from steady space heating only (compressor running continuously for a while, no DHW, no
defrost, real flow), where the heat in the water is what the radiators emit. Each sample gives
``K = Q / (MWT − Ti)^n``; the estimate is the median of a rolling window, so single odd samples (a
start-up transient, a sensor glitch) do not move it.

Why: the offline fit (63.1, winter 2025/26) used the bridge's flow × ΔT with the return sensor ~0.6 K
high and came out about a third low; the model then under-predicted the heat reaching the room and
the room ended warmer than forecast whenever the heat pump ran (Nov 2026).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

STATE_VERSION = 1


@dataclass
class RadiatorLearner:
    n: float = 1.3
    min_dt_k: float = 3.0  # MWT − Ti below this is too small to identify K
    min_run_s: float = 20 * 60.0  # space heating running at least this long (water circuit settled)
    min_flow_l_min: float = 5.0
    window: int = 600  # samples kept (5-min steps: ~50 h of steady heating)
    min_samples: int = 72  # ~6 h of steady heating before the estimate is used
    bounds: tuple[float, float] = (30.0, 250.0)
    samples: list[float] = field(default_factory=list)

    def add(self, heat_w: float, mwt_c: float, ti_c: float) -> bool:
        """Add one steady sample; returns whether it was used."""
        if not all(math.isfinite(v) for v in (heat_w, mwt_c, ti_c)):
            return False
        dt = mwt_c - ti_c
        if dt < self.min_dt_k or heat_w <= 0:
            return False
        k = heat_w / dt**self.n
        lo, hi = self.bounds
        if not lo <= k <= hi:
            return False
        self.samples.append(k)
        if len(self.samples) > self.window:
            del self.samples[: len(self.samples) - self.window]
        return True

    def estimate(self) -> float | None:
        """Median K once enough samples exist, else None (the configured prior applies)."""
        if len(self.samples) < self.min_samples:
            return None
        return float(statistics.median(self.samples))

    def to_dict(self) -> dict[str, Any]:
        return {"version": STATE_VERSION, "n": self.n, "samples": [round(k, 3) for k in self.samples]}

    def load_dict(self, d: dict[str, Any]) -> bool:
        """Restore samples; returns False (nothing changed) on invalid data or a different exponent."""
        if d.get("version") != STATE_VERSION or float(d.get("n", -1)) != self.n:
            return False
        lo, hi = self.bounds
        ks = [float(k) for k in d.get("samples") or []]
        if not all(math.isfinite(k) and lo <= k <= hi for k in ks):
            return False
        self.samples = ks[-self.window :]
        return True
