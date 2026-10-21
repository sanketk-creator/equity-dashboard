"""Parameter overrides, the equity edition of the Nifty dashboard's system:
validated against the code defaults, typed off them, stored as a diff.
NOTE the methodology's own warning — ANY change invalidates the backtest's
§10 results; the Parameters card says so."""
from __future__ import annotations

import math
from typing import Any

from . import config


def validate_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(overrides, dict):
        raise ValueError("overrides must be an object")
    defaults = config.STRATEGY_PARAMS
    unknown = [k for k in overrides if k not in defaults]
    if unknown:
        raise ValueError("unknown parameter(s): " + ", ".join(sorted(unknown)))
    clean: dict[str, Any] = {}
    for k, raw in overrides.items():
        d = defaults[k]
        if isinstance(d, str):
            v = str(raw).strip()
            allowed = config.PARAM_CHOICES.get(k)
            if allowed and v not in allowed:
                raise ValueError(f"{k}: must be one of {', '.join(allowed)}")
        else:
            try:
                f = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{k}: '{raw}' is not a number") from None
            if not math.isfinite(f) or f <= 0:
                raise ValueError(f"{k}: must be a positive finite number")
            v = int(f) if isinstance(d, int) else f
            if isinstance(d, int) and f != int(f):
                raise ValueError(f"{k}: expected a whole number")
        if v != d:
            clean[k] = v
    return clean
