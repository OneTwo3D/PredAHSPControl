"""Heat-pump performance: binned COP versus outdoor temperature.

COP per bin is the ratio of summed heat to summed electricity (not the mean of ratios, which is
biased by low-energy days). Bins are then made monotone non-decreasing in outdoor temperature with
pool-adjacent-violators, weighted by electricity, so colder weather never looks more efficient.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import numpy as np

from .regression import FloatArray


@dataclass(frozen=True)
class CopCurve:
    centres_c: tuple[float, ...]
    cop: tuple[float, ...]
    heat_kwh: tuple[float, ...]
    elec_kwh: tuple[float, ...]

    def at(self, to_c: float) -> tuple[float, bool]:
        """COP at outdoor temperature; second value is True when extrapolated (clamped)."""
        lo, hi = self.centres_c[0], self.centres_c[-1]
        extrapolated = not (lo <= to_c <= hi)
        return float(np.interp(to_c, self.centres_c, self.cop)), extrapolated


def _pava(values: FloatArray, weights: FloatArray) -> FloatArray:
    """Weighted isotonic (non-decreasing) regression."""
    blocks: list[list[float]] = []  # [value, weight, count]
    for v, w in zip(values, weights, strict=True):
        blocks.append([float(v), float(w), 1.0])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, c2 = blocks.pop()
            v1, w1, c1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, c1 + c2])
    out: list[float] = []
    for v, _, c in blocks:
        out.extend([v] * int(c))
    return np.asarray(out)


def fit_cop_curve(
    to_c: FloatArray,
    heat_kwh: FloatArray,
    elec_kwh: FloatArray,
    edges_c: FloatArray,
    min_elec_kwh: float = 5.0,
) -> CopCurve:
    """Fit a monotone COP curve from per-period (e.g. daily) outdoor temperature and energy totals."""
    to = np.asarray(to_c, float)
    heat = np.asarray(heat_kwh, float)
    elec = np.asarray(elec_kwh, float)
    ok = np.isfinite(to) & np.isfinite(heat) & np.isfinite(elec) & (elec > 0)
    centres, cops, hs, es = [], [], [], []
    for lo, hi in pairwise(edges_c):
        m = ok & (to > lo) & (to <= hi)
        e = float(elec[m].sum())
        if e < min_elec_kwh:
            continue
        h = float(heat[m].sum())
        centres.append(float(np.average(to[m], weights=elec[m])))
        cops.append(h / e)
        hs.append(h)
        es.append(e)
    if len(centres) < 2:
        raise ValueError("not enough populated bins for a COP curve")
    mono = _pava(np.asarray(cops), np.asarray(es))
    return CopCurve(tuple(centres), tuple(float(v) for v in mono), tuple(hs), tuple(es))


def min_output_w(full_run_heat_w: FloatArray, quantile: float = 0.05) -> float:
    """Estimate the minimum modulated heat output from intervals where the compressor ran throughout."""
    q = np.asarray(full_run_heat_w, float)
    q = q[np.isfinite(q) & (q > 0)]
    if len(q) < 20:
        raise ValueError("not enough full-run samples")
    return float(np.quantile(q, quantile))
