# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import time
from typing import Any


def _now_ts() -> float:
    return time.time()


def _safe_int(value: Any, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _safe_float(
    value: Any,
    default: float,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _single_line(text: Any, limit: int = 80) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]
