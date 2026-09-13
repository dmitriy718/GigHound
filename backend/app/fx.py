"""Dated operator-supplied FX for cross-currency draft bids; never live quotes."""

import json
import math
import os
from datetime import datetime, timezone, timedelta


def conversion_factor(source: str, destination: str) -> float | None:
    if source and source == destination:
        return 1.0
    try:
        configured = json.loads(os.environ.get("GIGHOUND_FX_RATES_JSON", "{}"))
        as_of = datetime.fromisoformat(configured["as_of"])
        if as_of.tzinfo is None or not timedelta(0) <= datetime.now(
            timezone.utc
        ) - as_of <= timedelta(days=1):
            return None
        if (
            not isinstance(configured["source"], str)
            or not configured["source"].strip()
        ):
            return None
        rates = {**configured["usd_per_unit"], "USD": 1.0}
        values = [float(rates[c]) for c in (source, destination)]
        if not all(math.isfinite(v) and v > 0 for v in values):
            return None
        return values[0] / values[1]
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
