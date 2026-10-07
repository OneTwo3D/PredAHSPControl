"""Small, dependency-light regression helpers (ordinary least squares with standard errors)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class OlsResult:
    """Result of an ordinary least-squares fit ``y ≈ X @ coef``."""

    coef: FloatArray
    stderr: FloatArray
    r2: float
    rmse: float
    n: int


def ols(x: FloatArray, y: FloatArray) -> OlsResult:
    """Fit ``y = X·b`` by least squares and return coefficients with classical standard errors.

    Raises ``ValueError`` when there are not more samples than parameters.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n, p = x.shape
    if n <= p:
        raise ValueError(f"need more samples ({n}) than parameters ({p})")
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ coef
    dof = n - p
    s2 = float(resid @ resid) / dof
    cov = s2 * np.linalg.pinv(x.T @ x)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else float("nan")
    return OlsResult(coef=coef, stderr=np.sqrt(np.clip(np.diag(cov), 0, None)), r2=r2, rmse=s2**0.5, n=n)
