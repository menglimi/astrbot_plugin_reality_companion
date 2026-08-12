# -*- coding: utf-8 -*-
"""Small, consent-first HTTP bridge for the Android companion app.

The gateway intentionally lives beside Reality Companion.  It exposes only
device-facing operations and keeps location/session state in memory so a
restart drops the most sensitive context automatically.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import hmac
import ipaddress
import math
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    from quart import request
except Exception:  # pragma: no cover - AstrBot supplies Quart at runtime
    request = None

from astrbot.api import logger

try:
    from aiohttp import web
except Exception:  # pragma: no cover - dependency diagnostics run at startup
    web = None


PLUGIN_NAME = "astrbot_plugin_reality_companion"
MOBILE_API_VERSION = "1.0"
MOBILE_MAX_BODY_BYTES = 256 * 1024
MOBILE_MAX_SESSIONS_PER_USER = 8


@dataclass(slots=True)
class _MobileRequestState:
    headers: dict[str, str] = field(default_factory=dict)
    host: str = ""
    remote_addr: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


_mobile_request_state: contextvars.ContextVar[_MobileRequestState | None] = contextvars.ContextVar(
    "reality_companion_mobile_request",
    default=None,
)


def _clean(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class MobileGatewayMixin:
    """Expose the minimum bridge needed by the mobile companion client."""

    def _mobile_gateway_init(self) -> None:
        self._mobile_sessions: dict[str, dict[str, Any]] = {}
        self._mobile_locations: dict[str, dict[str, Any]] = {}
        self._mobile_screen_clients: dict[str, dict[str, Any]] = {}
        self._mobile_pair_attempts: dict[str, list[float]] = {}
        self._mobile_server_runner: Any | None = None
        self._mobile_server_site: Any | None = None
        self._mobile_server_bound_port = 0
        self._mobile_room_start_lock = asyncio.Lock()
        self._mobile_state_lock = threading.RLock()
        self._mobile_runtime_stopped = False

    def _mobile_enabled(self) -> bool:
        return bool(self._cfg_bool("mobile.enabled", False))

    def _mobile_pairing_token(self) -> str:
        return self._cfg_str("mobile.pairing_token", "")

    def _mobile_host(self) -> str:
        return self._cfg_str("mobile.host", "0.0.0.0") or "0.0.0.0"

    def _mobile_port(self) -> int:
        return self._cfg_int("mobile.port", 6322, 1, 65535)

    def _mobile_session_ttl(self) -> int:
        hours = self._cfg_int("mobile.session_ttl_hours", 168, 1, 720)
        return hours * 60 * 60

    def _mobile_allowed_user_id(self) -> str:
        configured = self._cfg_str("mobile.allowed_user_id", "")
        if configured:
            return configured
        getter = getattr(self, "_private_companion_api", None)
        api = getter() if callable(getter) else None
        ids_getter = getattr(api, "get_reality_touch_authorized_user_ids", None)
        try:
            values = ids_getter() if callable(ids_getter) else []
        except Exception:
            values = []
        candidates: set[str] = set()
        if isinstance(values, (list, tuple, set)):
            candidates = {
                _clean(item, 120)
                for item in values
                if _clean(item, 120)
            }
        # Never guess which identity owns a physical device in a multi-user
        # installation. The administrator must make that binding explicit.
        return next(iter(candidates)) if len(candidates) == 1 else ""

    def _mobile_location_ttl(self) -> int:
        return max(60, min(24 * 60 * 60, self._cfg_int("mobile.location_ttl_seconds", 900, 60, 24 * 60 * 60)))

    def _mobile_screen_upload_enabled(self) -> bool:
        return bool(self._cfg_bool("mobile.screen_upload_enabled", True))

    def _mobile_token_from_request(self) -> str:
        current = _mobile_request_state.get()
        request_obj = current if current is not None else request
        if request_obj is None:
            return ""
        header_name = "x-companion-mobile-token" if current is not None else "X-Companion-Mobile-Token"
        auth_name = "authorization" if current is not None else "Authorization"
        token = _clean(request_obj.headers.get(header_name), 240)
        if token:
            return token
        auth = _clean(request_obj.headers.get(auth_name), 260)
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()[:240]
        return ""

    async def _mobile_request_payload(self) -> dict[str, Any]:
        current = _mobile_request_state.get()
        if current is not None:
            return dict(current.payload)
        if request is None:
            return {}
        payload = await request.get_json(silent=True) or {}
        return payload if isinstance(payload, dict) else {}

    def _mobile_request_host(self) -> str:
        current = _mobile_request_state.get()
        if current is not None:
            return _clean(current.host, 180)
        return _clean(getattr(request, "host", ""), 180) if request is not None else ""

    def _mobile_request_remote_addr(self) -> str:
        current = _mobile_request_state.get()
        if current is not None:
            return _clean(current.remote_addr, 96)
        return _clean(getattr(request, "remote_addr", ""), 96) if request is not None else ""

    @staticmethod
    def _mobile_normalize_host(value: Any) -> str:
        raw = _clean(value, 180).split(",", 1)[0].strip()
        if not raw or any(char in raw for char in ("/", "\\", "@", "?", "#")):
            return ""
        try:
            parsed = urlsplit(f"//{raw}")
            if parsed.username or parsed.password:
                return ""
            host = str(parsed.hostname or "").strip().rstrip(".")
        except (TypeError, ValueError):
            return ""
        if not host:
            return ""
        try:
            address = ipaddress.ip_address(host)
            return "" if address.is_unspecified else str(address)
        except ValueError:
            try:
                ascii_host = host.encode("idna").decode("ascii").lower()
            except UnicodeError:
                return ""
            labels = ascii_host.split(".")
            if any(
                not label
                or len(label) > 63
                or not label[0].isalnum()
                or not label[-1].isalnum()
                or any(not (char.isalnum() or char == "-") for char in label)
                for label in labels
            ):
                return ""
            return ascii_host

    @staticmethod
    def _mobile_token_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8", errors="ignore")).hexdigest()

    def _mobile_cleanup_sessions(self) -> None:
        now = time.time()
        with self._mobile_state_lock:
            expired = [
                key
                for key, item in self._mobile_sessions.items()
                if not isinstance(item, dict) or _number(item.get("expires_at")) <= now
            ]
            for key in expired:
                self._mobile_sessions.pop(key, None)

            stale_locations = [
                user_id
                for user_id, item in self._mobile_locations.items()
                if not isinstance(item, dict)
                or now - _number(item.get("received_at") or item.get("captured_at")) > self._mobile_location_ttl()
            ]
            for user_id in stale_locations:
                self._mobile_locations.pop(user_id, None)

            stale_screen_clients = [
                user_id
                for user_id, item in self._mobile_screen_clients.items()
                if not isinstance(item, dict) or now - _number(item.get("last_seen_at")) > 5 * 60
            ]
            for user_id in stale_screen_clients:
                self._mobile_screen_clients.pop(user_id, None)

    def _mobile_authorize(self, *, allow_pairing: bool = False) -> dict[str, Any] | None:
        if self._mobile_runtime_stopped or not self._mobile_enabled():
            return None
        token = self._mobile_token_from_request()
        if not token:
            return None
        if allow_pairing:
            configured = self._mobile_pairing_token()
            if configured and hmac.compare_digest(token, configured):
                return {"user_id": self._mobile_allowed_user_id(), "pairing": True}
        self._mobile_cleanup_sessions()
        with self._mobile_state_lock:
            session = self._mobile_sessions.get(self._mobile_token_key(token))
            if not isinstance(session, dict):
                return None
            session["last_seen_at"] = time.time()
            return dict(session)

    def _mobile_pair_rate_limited(self, *, succeeded: bool = False) -> bool:
        key = self._mobile_request_remote_addr() or "unknown"
        now = time.time()
        with self._mobile_state_lock:
            self._mobile_pair_attempts = {
                remote: [value for value in values if now - value <= 60]
                for remote, values in self._mobile_pair_attempts.items()
                if any(now - value <= 60 for value in values)
            }
            attempts = [value for value in self._mobile_pair_attempts.get(key, []) if now - value <= 60]
            if succeeded:
                self._mobile_pair_attempts.pop(key, None)
                return False
            limited = len(attempts) >= 8
            if not limited:
                attempts.append(now)
                self._mobile_pair_attempts[key] = attempts
            return limited

    def _mobile_store_session(self, token: str, session: dict[str, Any]) -> None:
        user_id = _clean(session.get("user_id"), 120)
        with self._mobile_state_lock:
            existing = [
                (key, item)
                for key, item in self._mobile_sessions.items()
                if isinstance(item, dict) and _clean(item.get("user_id"), 120) == user_id
            ]
            if len(existing) >= MOBILE_MAX_SESSIONS_PER_USER:
                oldest_key, _ = min(existing, key=lambda pair: _number(pair[1].get("created_at")))
                self._mobile_sessions.pop(oldest_key, None)
            self._mobile_sessions[self._mobile_token_key(token)] = session

    @staticmethod
    def _mobile_json_error(message: str, status: int = 400) -> tuple[dict[str, Any], int]:
        return {"ok": False, "message": _clean(message, 240)}, status

    def _mobile_find_plugin(self, name: str) -> Any | None:
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            metadata = getter(name)
            return getattr(metadata, "star_cls", None) if metadata is not None else None
        except Exception:
            return None

    def _mobile_reality_status(self) -> dict[str, Any]:
        status_getter = getattr(self, "integration_status", None)
        try:
            value = status_getter() if callable(status_getter) else {}
        except Exception:
            value = {}
        return value if isinstance(value, dict) else {}

    def _mobile_location_snapshot(self, user_id: str, *, prompt_safe: bool = False) -> dict[str, Any]:
        self._mobile_cleanup_sessions()
        with self._mobile_state_lock:
            stored = self._mobile_locations.get(_clean(user_id, 120))
            item = dict(stored) if isinstance(stored, dict) else None
        if not isinstance(item, dict):
            return {"available": False, "reason": "no_recent_location"}
        captured_at = _number(item.get("captured_at"))
        received_at = _number(item.get("received_at") or captured_at)
        age = max(0, int(time.time() - captured_at)) if captured_at else 0
        retention_age = max(0, int(time.time() - received_at)) if received_at else 0
        if not captured_at or not received_at or retention_age > self._mobile_location_ttl():
            return {"available": False, "reason": "location_expired", "age_seconds": age}
        return {
            "available": True,
            "latitude": round(_number(item.get("latitude")), 3),
            "longitude": round(_number(item.get("longitude")), 3),
            "accuracy_m": round(max(0.0, _number(item.get("accuracy_m"))), 1),
            "altitude_m": round(_number(item.get("altitude_m")), 1) if item.get("altitude_m") is not None else None,
            "speed_mps": round(max(0.0, _number(item.get("speed_mps"))), 1),
            "bearing": round(_number(item.get("bearing")), 1) if item.get("bearing") is not None else None,
            # Device-provided free text is useful in the app status but must not
            # become a higher-trust prompt instruction in Private Companion.
            "label": "" if prompt_safe else _clean(item.get("label"), 40),
            "captured_at": captured_at,
            "age_seconds": age,
            "source": "android_foreground_location",
        }

    def mobile_context(self, user_id: str = "") -> dict[str, Any]:
        """Return a prompt-safe, coarse location context for Private Companion."""
        normalized = _clean(user_id, 120)
        if self._mobile_runtime_stopped or not self._mobile_enabled():
            snapshot = {"available": False, "reason": "mobile_gateway_disabled"}
        else:
            snapshot = (
                self._mobile_location_snapshot(normalized, prompt_safe=True)
                if normalized
                else {"available": False, "reason": "user_missing"}
            )
        return {
            "available": bool(snapshot.get("available")),
            "user_id": normalized,
            "location": snapshot,
            "privacy": {
                "coordinates_rounded": True,
                "foreground_only": True,
                "expires_after_seconds": self._mobile_location_ttl(),
            },
        }

    def _mobile_screen_status(self, user_id: str) -> dict[str, Any]:
        self._mobile_cleanup_sessions()
        with self._mobile_state_lock:
            stored = self._mobile_screen_clients.get(_clean(user_id, 120))
            item = dict(stored) if isinstance(stored, dict) else None
        if not isinstance(item, dict):
            return {"available": self._mobile_screen_upload_enabled(), "streaming": False}
        age = max(0, int(time.time() - _number(item.get("last_seen_at"))))
        return {
            "available": self._mobile_screen_upload_enabled(),
            "streaming": age <= 90 and bool(item.get("streaming")),
            "client_id": _clean(item.get("client_id"), 100),
            "age_seconds": age,
        }

    async def _mobile_status_response(self, auth: dict[str, Any]) -> dict[str, Any]:
        user_id = _clean(auth.get("user_id"), 120)
        together = self._mobile_find_plugin("astrbot_plugin_together_companion")
        together_status: dict[str, Any] = {"available": False}
        if together is not None:
            try:
                result = await together.page_status()
                data = result.get("data") if isinstance(result, dict) else {}
                together_status = {
                    "available": True,
                    "running": bool(isinstance(data, dict) and data.get("running")),
                    "base_url": _clean(data.get("base_url"), 300) if isinstance(data, dict) else "",
                    "capabilities": data.get("capabilities", {}) if isinstance(data, dict) else {},
                }
            except Exception as exc:
                together_status = {"available": True, "running": False, "message": _clean(exc, 160)}
        return {
            "ok": True,
            "data": {
                "api_version": MOBILE_API_VERSION,
                "paired": not bool(auth.get("pairing")),
                "user_id": user_id,
                "reality": self._mobile_reality_status(),
                "location": self._mobile_location_snapshot(user_id),
                "screen": self._mobile_screen_status(user_id),
                "together": together_status,
                "capabilities": {
                    "room": bool(together_status.get("available")),
                    "location": True,
                    "screen_upload": self._mobile_screen_upload_enabled(),
                },
            },
        }

    async def mobile_health(self) -> tuple[dict[str, Any], int]:
        """Return a deliberately small liveness response without credentials."""
        return {
            "ok": True,
            "data": {
                "service": "reality_companion_mobile",
                "api_version": MOBILE_API_VERSION,
                "pairing_required": True,
            },
        }, 200

    async def mobile_pair(self) -> tuple[dict[str, Any], int]:
        if self._mobile_pair_rate_limited():
            return self._mobile_json_error("配对尝试过于频繁，请稍后再试", 429)
        auth = self._mobile_authorize(allow_pairing=True)
        if not auth or not auth.get("pairing"):
            return self._mobile_json_error("配对令牌无效", 401)
        payload = await self._mobile_request_payload()
        requested_user = _clean(payload.get("user_id"), 120)
        configured_user = self._mobile_allowed_user_id()
        if not configured_user:
            return self._mobile_json_error("未配置可配对的主要用户 ID", 400)
        if requested_user and requested_user != configured_user:
            return self._mobile_json_error("请求绑定的用户与网关配置不一致", 403)
        user_id = configured_user
        context_getter = getattr(self, "_private_companion_api", None)
        try:
            api = context_getter() if callable(context_getter) else None
        except Exception as exc:
            logger.warning("[RealityCompanion] 获取移动端绑定用户授权接口失败: %s", _clean(exc, 160))
            return self._mobile_json_error("暂时无法校验陪伴用户授权", 503)
        checker = getattr(api, "get_reality_touch_host_context", None) if api is not None else None
        if api is not None and not callable(checker):
            return self._mobile_json_error("陪伴插件未提供移动端授权校验", 503)
        if callable(checker):
            try:
                host_context = checker(user_id)
            except Exception as exc:
                logger.warning("[RealityCompanion] 校验移动端绑定用户失败: %s", _clean(exc, 160))
                return self._mobile_json_error("暂时无法校验陪伴用户授权", 503)
            if not isinstance(host_context, dict) or not bool(host_context.get("eligible")):
                return self._mobile_json_error("该用户没有现实触及设备授权资格", 403)
        session_token = secrets.token_urlsafe(32)
        now = time.time()
        session = {
            "user_id": user_id,
            "device_name": _clean(payload.get("device_name"), 100) or "陪伴终端 Android",
            "created_at": now,
            "last_seen_at": now,
            "expires_at": now + self._mobile_session_ttl(),
        }
        self._mobile_store_session(session_token, session)
        self._mobile_pair_rate_limited(succeeded=True)
        result = await self._mobile_status_response(session)
        result["data"]["session_token"] = session_token
        result["data"]["session_expires_at"] = session["expires_at"]
        logger.info("[RealityCompanion] Android 移动端已配对: user=%s device=%s", user_id, session["device_name"])
        return result, 200

    async def mobile_status(self) -> tuple[dict[str, Any], int]:
        auth = self._mobile_authorize()
        if not auth:
            return self._mobile_json_error("未授权的移动端请求", 401)
        return await self._mobile_status_response(auth), 200

    async def mobile_create_room(self) -> tuple[dict[str, Any], int]:
        auth = self._mobile_authorize()
        if not auth:
            return self._mobile_json_error("未授权的移动端请求", 401)
        payload = await self._mobile_request_payload()
        mode = _clean(payload.get("mode"), 16).lower()
        if mode not in {"call", "watch", "work"}:
            mode = "call"
        user_id = _clean(auth.get("user_id"), 120)
        together = self._mobile_find_plugin("astrbot_plugin_together_companion")
        if together is None:
            return self._mobile_json_error("未安装或未加载一起房间插件", 503)
        if mode == "work" and not bool(together.work_collaboration_available()):
            return self._mobile_json_error("工作协同需要先启用屏幕伙伴", 409)
        if together._get_chat_provider() is None:
            return self._mobile_json_error("一起房间尚未配置实时对话模型", 409)
        access_preparer = getattr(together, "_ensure_mobile_room_access", None)
        if not callable(access_preparer):
            return self._mobile_json_error("一起房间插件版本过旧，缺少手机安全访问能力", 503)
        async with self._mobile_room_start_lock:
            try:
                await access_preparer()
            except Exception as exc:
                return self._mobile_json_error(f"手机房间准备失败：{_clean(exc, 180)}", 503)
            ticket = together.issue_room_ticket(mode=mode, user_id=user_id)
            room_url = together._ticket_url(ticket)
        room_url = self._mobile_rewrite_room_url(room_url)
        if mode == "call" and urlsplit(room_url).scheme.lower() != "https":
            revoker = getattr(together, "_revoke_unused_ticket", None)
            if callable(revoker):
                revoker(ticket)
            return self._mobile_json_error(
                "视频通话需要 HTTPS 安全地址，否则 Android WebView 无法使用摄像头和麦克风",
                409,
            )
        return {
            "ok": True,
            "data": {
                "url": room_url,
                "mode": mode,
                "expires_at": ticket.expires_at,
            },
        }, 200

    def _mobile_rewrite_room_url(self, room_url: str) -> str:
        """Replace loopback room hosts with the host used by the mobile request."""
        try:
            parsed = urlsplit(str(room_url or ""))
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                return room_url
            host_only = self._mobile_normalize_host(self._mobile_request_host())
            if not host_only:
                return room_url
            netloc = f"[{host_only}]" if ":" in host_only else host_only
            if parsed.port is not None:
                netloc = f"{netloc}:{parsed.port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        except Exception:
            return room_url

    async def mobile_location(self) -> tuple[dict[str, Any], int]:
        auth = self._mobile_authorize()
        if not auth:
            return self._mobile_json_error("未授权的移动端请求", 401)
        payload = await self._mobile_request_payload()
        latitude = _number(payload.get("latitude"), 999.0)
        longitude = _number(payload.get("longitude"), 999.0)
        if not (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90.0 <= latitude <= 90.0
            and -180.0 <= longitude <= 180.0
        ):
            return self._mobile_json_error("经纬度不在有效范围内", 400)
        captured_at = _number(payload.get("captured_at"), time.time())
        if captured_at > 10_000_000_000:
            captured_at /= 1000.0
        now = time.time()
        if not math.isfinite(captured_at) or abs(now - captured_at) > 10 * 60:
            return self._mobile_json_error("定位时间过旧，请重新获取位置", 400)
        user_id = _clean(auth.get("user_id"), 120)
        accuracy = _number(payload.get("accuracy_m"), 0.0)
        altitude = _number(payload.get("altitude_m")) if payload.get("altitude_m") is not None else None
        speed = _number(payload.get("speed_mps"), 0.0)
        bearing = _number(payload.get("bearing")) if payload.get("bearing") is not None else None
        item = {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": max(0.0, min(100_000.0, accuracy)) if math.isfinite(accuracy) else 0.0,
            "altitude_m": altitude if altitude is not None and math.isfinite(altitude) else None,
            "speed_mps": max(0.0, min(1000.0, speed)) if math.isfinite(speed) else 0.0,
            "bearing": bearing % 360.0 if bearing is not None and math.isfinite(bearing) else None,
            "label": _clean(payload.get("label"), 40),
            "captured_at": captured_at,
            "received_at": now,
        }
        with self._mobile_state_lock:
            self._mobile_locations[user_id] = item
        return {"ok": True, "data": self._mobile_location_snapshot(user_id)}, 200

    async def mobile_screen_heartbeat(self) -> tuple[dict[str, Any], int]:
        auth = self._mobile_authorize()
        if not auth:
            return self._mobile_json_error("未授权的移动端请求", 401)
        if not self._mobile_screen_upload_enabled():
            return self._mobile_json_error("手机屏幕共享上报已在配置中关闭", 409)
        payload = await self._mobile_request_payload()
        user_id = _clean(auth.get("user_id"), 120)
        with self._mobile_state_lock:
            self._mobile_screen_clients[user_id] = {
                "client_id": _clean(payload.get("client_id"), 100),
                "streaming": bool(payload.get("streaming")),
                "last_seen_at": time.time(),
            }
        return {"ok": True, "data": self._mobile_screen_status(user_id)}, 200

    async def mobile_revoke_location(self) -> tuple[dict[str, Any], int]:
        auth = self._mobile_authorize()
        if not auth:
            return self._mobile_json_error("未授权的移动端请求", 401)
        with self._mobile_state_lock:
            self._mobile_locations.pop(_clean(auth.get("user_id"), 120), None)
        return {"ok": True, "data": {"revoked": True}}, 200

    async def mobile_close_session(self) -> tuple[dict[str, Any], int]:
        token = self._mobile_token_from_request()
        auth = self._mobile_authorize()
        if not token or not auth:
            return self._mobile_json_error("未授权的移动端请求", 401)
        with self._mobile_state_lock:
            self._mobile_sessions.pop(self._mobile_token_key(token), None)
        return {"ok": True, "data": {"closed": True}}, 200

    @staticmethod
    def _mobile_aiohttp_response(result: Any) -> Any:
        body: dict[str, Any]
        status = 200
        if isinstance(result, tuple) and len(result) == 2:
            candidate, candidate_status = result
            body = candidate if isinstance(candidate, dict) else {"ok": False, "message": "移动端响应格式无效"}
            try:
                status = int(candidate_status)
            except (TypeError, ValueError):
                status = 500
        elif isinstance(result, dict):
            body = result
        else:
            body = {"ok": False, "message": "移动端响应格式无效"}
            status = 500
        return web.json_response(
            body,
            status=max(100, min(599, status)),
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _mobile_aiohttp_dispatch(self, aio_request: Any, handler: Any) -> Any:
        if web is None:  # pragma: no cover - guarded by server startup
            raise RuntimeError("aiohttp 不可用")
        payload: dict[str, Any] = {}
        if aio_request.can_read_body:
            content_length = aio_request.content_length
            if content_length is not None and content_length > MOBILE_MAX_BODY_BYTES:
                return self._mobile_aiohttp_response(self._mobile_json_error("请求体过大", 413))
            try:
                candidate = await aio_request.json()
            except Exception:
                return self._mobile_aiohttp_response(self._mobile_json_error("请求体必须是 JSON 对象", 400))
            if not isinstance(candidate, dict):
                return self._mobile_aiohttp_response(self._mobile_json_error("请求体必须是 JSON 对象", 400))
            payload = candidate

        transport = getattr(aio_request, "transport", None)
        socket_name = transport.get_extra_info("sockname") if transport is not None else None
        local_host = str(socket_name[0]) if isinstance(socket_name, tuple) and socket_name else ""
        state = _MobileRequestState(
            headers={str(key).lower(): str(value) for key, value in aio_request.headers.items()},
            # Use the accepted socket address rather than Host/X-Forwarded-Host,
            # so a request cannot inject the hostname embedded in a room ticket.
            host=local_host,
            remote_addr=str(aio_request.remote or ""),
            payload=payload,
        )
        context_token = _mobile_request_state.set(state)
        try:
            result = await handler()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[RealityCompanion] 移动端 API 处理失败: %s", _clean(exc, 180))
            result = self._mobile_json_error("移动端服务处理失败", 500)
        finally:
            _mobile_request_state.reset(context_token)
        return self._mobile_aiohttp_response(result)

    async def _start_mobile_server(self) -> bool:
        self._mobile_runtime_stopped = False
        if not self._mobile_enabled():
            return False
        if self._mobile_server_runner is not None:
            return True
        if web is None:
            self._mobile_runtime_stopped = True
            logger.error("[RealityCompanion] 移动端网关已启用，但缺少 aiohttp 依赖")
            return False
        if not self._mobile_pairing_token():
            self._mobile_runtime_stopped = True
            logger.error("[RealityCompanion] 移动端网关未启动：请先配置 mobile.pairing_token")
            return False
        if len(self._mobile_pairing_token()) < 24:
            logger.warning("[RealityCompanion] mobile.pairing_token 较短，建议改用至少 24 位随机令牌")

        prefix = f"/{PLUGIN_NAME}/mobile"
        app = web.Application(client_max_size=MOBILE_MAX_BODY_BYTES)
        canonical_routes = (
            ("GET", "/health", self.mobile_health),
            ("POST", "/pair", self.mobile_pair),
            ("GET", "/status", self.mobile_status),
            ("POST", "/room/create", self.mobile_create_room),
            ("POST", "/location", self.mobile_location),
            ("POST", "/location/revoke", self.mobile_revoke_location),
            ("POST", "/screen/heartbeat", self.mobile_screen_heartbeat),
            ("POST", "/session/close", self.mobile_close_session),
        )
        # Keep the namespaced paths as compatibility aliases for an older APK,
        # while the dedicated port exposes concise canonical paths to new clients.
        routes = canonical_routes + tuple(
            (method, f"{prefix}{path}", handler)
            for method, path, handler in canonical_routes
            if path != "/health"
        )
        for method, path, handler in routes:
            app.router.add_route(
                method,
                path,
                functools.partial(self._mobile_aiohttp_dispatch, handler=handler),
            )

        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, host=self._mobile_host(), port=self._mobile_port())
            await site.start()
        except Exception as exc:
            try:
                await runner.cleanup()
            except Exception as cleanup_exc:
                logger.debug("[RealityCompanion] 移动端网关失败后的清理异常: %s", _clean(cleanup_exc, 160))
            self._mobile_runtime_stopped = True
            logger.error(
                "[RealityCompanion] 移动端网关启动失败: host=%s port=%s error=%s",
                self._mobile_host(),
                self._mobile_port(),
                _clean(exc, 220),
            )
            return False

        self._mobile_server_runner = runner
        self._mobile_server_site = site
        self._mobile_server_bound_port = self._mobile_port()
        server = getattr(site, "_server", None)
        sockets = getattr(server, "sockets", None)
        if sockets:
            try:
                self._mobile_server_bound_port = int(sockets[0].getsockname()[1])
            except (AttributeError, IndexError, TypeError, ValueError):
                pass
        logger.info(
            "[RealityCompanion] Android 移动端网关已监听: http://%s:%s",
            self._mobile_host(),
            self._mobile_server_bound_port,
        )
        return True

    async def _stop_mobile_server(self) -> None:
        site = self._mobile_server_site
        runner = self._mobile_server_runner
        self._mobile_server_site = None
        self._mobile_server_runner = None
        self._mobile_server_bound_port = 0
        self._mobile_runtime_stopped = True
        try:
            if site is not None:
                await site.stop()
        finally:
            try:
                if runner is not None:
                    await runner.cleanup()
            finally:
                # Disabling/unloading the device bridge must immediately discard
                # every sensitive in-memory session and observation, even when
                # the underlying listener reports an error while shutting down.
                with self._mobile_state_lock:
                    self._mobile_sessions.clear()
                    self._mobile_locations.clear()
                    self._mobile_screen_clients.clear()
                    self._mobile_pair_attempts.clear()

    def _register_mobile_api(self) -> None:
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            logger.warning("[RealityCompanion] 当前 AstrBot 不支持移动端网关 API")
            return

        def route(handler):
            @functools.wraps(handler)
            async def wrapped(*args, **kwargs):
                return await handler(*args, **kwargs)

            return wrapped

        prefix = f"/{PLUGIN_NAME}/mobile"
        register_api(f"{prefix}/pair", route(self.mobile_pair), ["POST"], "Reality Companion mobile pair")
        register_api(f"{prefix}/status", route(self.mobile_status), ["GET"], "Reality Companion mobile status")
        register_api(f"{prefix}/room/create", route(self.mobile_create_room), ["POST"], "Reality Companion mobile room")
        register_api(f"{prefix}/location", route(self.mobile_location), ["POST"], "Reality Companion mobile location")
        register_api(f"{prefix}/location/revoke", route(self.mobile_revoke_location), ["POST"], "Reality Companion revoke mobile location")
        register_api(f"{prefix}/screen/heartbeat", route(self.mobile_screen_heartbeat), ["POST"], "Reality Companion mobile screen status")
        register_api(f"{prefix}/session/close", route(self.mobile_close_session), ["POST"], "Reality Companion mobile close")
