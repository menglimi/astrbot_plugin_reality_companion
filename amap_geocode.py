# -*- coding: utf-8 -*-
"""Optional Amap reverse-geocoding adapter for the mobile gateway.

The adapter is deliberately small and privacy-first: it returns structured
area labels for prompt use, while keeping the full address only in the
short-lived reality plugin memory when explicitly requested by the user.
"""
from __future__ import annotations

import math
from typing import Any

try:
    import aiohttp
except Exception:  # pragma: no cover - runtime dependency is supplied by AstrBot
    aiohttp = None


AMAP_REVERSE_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/regeo"


def _text(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def amap_cache_key(latitude: Any, longitude: Any) -> str:
    lat = _finite_number(latitude)
    lon = _finite_number(longitude)
    if lat is None or lon is None:
        return ""
    # Three decimals is roughly 100m and avoids turning every GPS sample into
    # a paid request while retaining enough detail for area-level context.
    return f"{lat:.3f},{lon:.3f}"


def normalize_amap_response(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or str(payload.get("status") or "") != "1":
        return None
    regeocode = payload.get("regeocode")
    if not isinstance(regeocode, dict):
        return None
    address_component = regeocode.get("addressComponent")
    if not isinstance(address_component, dict):
        address_component = {}
    city = address_component.get("city")
    if isinstance(city, list):
        city = city[0] if city else ""
    township = address_component.get("township")
    if isinstance(township, list):
        township = township[0] if township else ""
    street = address_component.get("streetNumber")
    if not isinstance(street, dict):
        street = {}
    districts = [
        _text(address_component.get("province"), 40),
        _text(city, 40),
        _text(address_component.get("district"), 40),
    ]
    area_parts: list[str] = []
    for value in districts:
        if value and value not in area_parts:
            area_parts.append(value)
    result = {
        "province": _text(address_component.get("province"), 40),
        "city": _text(city, 40),
        "district": _text(address_component.get("district"), 40),
        "township": _text(township, 40),
        "street": _text(street.get("street"), 60),
        "number": _text(street.get("number"), 24),
        "poi": _text(regeocode.get("pois", [{}])[0].get("name") if isinstance(regeocode.get("pois"), list) and regeocode.get("pois") and isinstance(regeocode.get("pois")[0], dict) else "", 80),
        "formatted_address": _text(regeocode.get("formatted_address"), 180),
        "area_label": "·".join(area_parts),
        "source": "amap_reverse_geocode",
    }
    return result if result["area_label"] or result["formatted_address"] else None


async def reverse_geocode(
    latitude: Any,
    longitude: Any,
    *,
    api_key: str,
    timeout_seconds: float = 5.0,
) -> dict[str, Any] | None:
    if aiohttp is None or not _text(api_key, 160):
        return None
    lat = _finite_number(latitude)
    lon = _finite_number(longitude)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    timeout = aiohttp.ClientTimeout(total=max(1.0, min(20.0, float(timeout_seconds or 5.0))))
    params = {
        "key": _text(api_key, 160),
        "location": f"{lon:.6f},{lat:.6f}",
        # Android location is normally WGS-84; let AMap perform the standard
        # conversion before reverse-geocoding instead of shifting the result
        # in the client or persisting another coordinate representation.
        "coordsys": "gps",
        "extensions": "base",
        "output": "JSON",
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(AMAP_REVERSE_GEOCODE_URL, params=params) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
    except Exception:
        return None
    return normalize_amap_response(payload)
