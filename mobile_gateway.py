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
import inspect
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
    import aiohttp
    from aiohttp import web
except Exception:  # pragma: no cover - dependency diagnostics run at startup
    aiohttp = None
    web = None


PLUGIN_NAME = "astrbot_plugin_reality_companion"
MOBILE_API_VERSION = "1.0"
MOBILE_MAX_BODY_BYTES = 256 * 1024
MOBILE_MAX_SESSIONS_PER_USER = 8
MOBILE_PROXY_MAX_WS_BYTES = 16 * 1024 * 1024


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
        self._mobile_location_notify_tasks: dict[str, asyncio.Task] = {}
        self._mobile_proxy_session: Any | None = None
        self._mobile_room_upstream_cache: dict[str, str] = {}

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
        place = item.get("place") if isinstance(item.get("place"), dict) else {}
        prompt_place = {
            "matched": bool(place.get("matched")),
            "name": _clean(place.get("name"), 40),
            "kind": _clean(place.get("kind"), 24),
            "distance_m": round(max(0.0, _number(place.get("distance_m"))), 0),
            "radius_m": round(max(0.0, _number(place.get("radius_m"))), 0),
        }
        confidence = _clean(place.get("confidence"), 32)
        aliases = [_clean(item, 40) for item in list(place.get("aliases") or [])[:8] if _clean(item, 40)]
        parent_name = _clean(place.get("parent_name"), 40)
        if confidence:
            prompt_place["confidence"] = confidence
        if aliases:
            prompt_place["aliases"] = aliases
        if parent_name:
            prompt_place["parent_name"] = parent_name
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
            # Explicitly saved places are trusted only as environmental facts,
            # never as instructions supplied by the device.
            "place": prompt_place,
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

    async def _notify_private_companion_location(self, user_id: str) -> None:
        try:
            getter = getattr(self, "_private_companion_api", None)
            api = getter() if callable(getter) else None
            notifier = getattr(api, "notify_mobile_location_update", None) if api is not None else None
            if not callable(notifier):
                return
            result = notifier(user_id)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.debug("[RealityCompanion] 手机位置主动联动暂时失败: %s", _clean(exc, 160))

    def _schedule_private_companion_location_notification(self, user_id: str) -> None:
        normalized = _clean(user_id, 120)
        if not normalized:
            return
        previous = self._mobile_location_notify_tasks.get(normalized)
        if isinstance(previous, asyncio.Task) and not previous.done():
            return
        try:
            creator = getattr(self, "_create_lifecycle_background_task", None)
            task = (
                creator(self._notify_private_companion_location(normalized), label="mobile_location_notify")
                if callable(creator)
                else asyncio.create_task(self._notify_private_companion_location(normalized))
            )
        except RuntimeError:
            return
        self._mobile_location_notify_tasks[normalized] = task

        def clear(done: asyncio.Task) -> None:
            if self._mobile_location_notify_tasks.get(normalized) is done:
                self._mobile_location_notify_tasks.pop(normalized, None)

        task.add_done_callback(clear)

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

    async def _mobile_game_status(self) -> dict[str, Any]:
        game = self._mobile_find_plugin("astrbot_plugin_game_companion")
        if game is None:
            return {
                "available": False,
                "ready": False,
                "blockers": ["未安装或未加载游戏伴侣插件"],
                "games": [],
            }
        getter = getattr(game, "mobile_status", None)
        if not callable(getter):
            return {
                "available": True,
                "ready": False,
                "blockers": ["游戏伴侣版本过旧，缺少手机房间能力"],
                "games": [],
            }
        try:
            result = await self._mobile_call_gateway_aware(getter)
            return result if isinstance(result, dict) else {
                "available": True,
                "ready": False,
                "blockers": ["游戏伴侣状态响应无效"],
                "games": [],
            }
        except Exception as exc:
            return {
                "available": True,
                "ready": False,
                "blockers": [f"读取游戏伴侣状态失败：{_clean(exc, 160)}"],
                "games": [],
            }

    async def _mobile_status_response(self, auth: dict[str, Any]) -> dict[str, Any]:
        user_id = _clean(auth.get("user_id"), 120)
        game_status = await self._mobile_game_status()
        together = self._mobile_find_plugin("astrbot_plugin_together_companion")
        together_status: dict[str, Any] = {"available": False}
        if together is not None:
            try:
                result = await together.page_status()
                data = result.get("data") if isinstance(result, dict) else {}
                together_status = {
                    "available": True,
                    "enabled": bool(isinstance(data, dict) and data.get("enabled")),
                    "running": bool(isinstance(data, dict) and data.get("running")),
                    "base_url": _clean(data.get("base_url"), 300) if isinstance(data, dict) else "",
                    "capabilities": data.get("capabilities", {}) if isinstance(data, dict) else {},
                    "tunnel": data.get("tunnel", {}) if isinstance(data, dict) else {},
                }
            except Exception as exc:
                together_status = {"available": True, "running": False, "message": _clean(exc, 160)}
        together_capabilities = (
            together_status.get("capabilities")
            if isinstance(together_status.get("capabilities"), dict)
            else {}
        )
        chat_capability = (
            together_capabilities.get("chat")
            if isinstance(together_capabilities.get("chat"), dict)
            else {}
        )
        work_capability = (
            together_capabilities.get("work")
            if isinstance(together_capabilities.get("work"), dict)
            else {}
        )
        room_ready = bool(
            together_status.get("available")
            and together_status.get("enabled")
            and chat_capability.get("available")
        )
        room_blockers: list[str] = []
        if not together_status.get("available"):
            room_blockers.append("未安装或未加载一起房间插件")
        elif not together_status.get("enabled"):
            room_blockers.append("一起房间服务未启用")
        if together_status.get("available") and not chat_capability.get("available"):
            room_blockers.append("未配置实时共处对话模型")
        together_status["ready"] = room_ready
        together_status["blockers"] = room_blockers
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
                "games": game_status,
                "capabilities": {
                    "room": room_ready,
                    "call": room_ready,
                    "watch": room_ready,
                    "work": room_ready and bool(work_capability.get("available")),
                    "location": True,
                    "screen_upload": self._mobile_screen_upload_enabled(),
                    "games": bool(game_status.get("ready")),
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

    async def _mobile_call_gateway_aware(self, callback: Any, *args: Any) -> Any:
        """Call a cross-plugin API through the gateway when its wrapper permits it."""
        try:
            result = callback(*args, via_mobile_gateway=True)
        except TypeError as exc:
            message = str(exc).lower()
            if "via_mobile_gateway" not in message and "unexpected keyword" not in message:
                raise
            result = callback(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _mobile_prepare_together_access(self, together: Any) -> dict[str, Any]:
        """Prepare a room through the unified gateway, even for wrapped APIs."""
        access_preparer = getattr(together, "_ensure_mobile_room_access", None)
        if not callable(access_preparer):
            raise RuntimeError("一起房间插件版本过旧，缺少手机安全访问能力")
        result = await self._mobile_call_gateway_aware(access_preparer)
        return result if isinstance(result, dict) else {}

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
        async with self._mobile_room_start_lock:
            try:
                await self._mobile_prepare_together_access(together)
            except Exception as exc:
                return self._mobile_json_error(f"手机房间准备失败：{_clean(exc, 180)}", 503)
            ticket = together.issue_room_ticket(mode=mode, user_id=user_id)
            room_url = together._ticket_url(ticket)
        # 原生 App 的摄像头/麦克风走系统权限而非浏览器安全上下文；
        # 经统一代理时允许 LAN 明文通话，浏览器场景仍强制 HTTPS。
        native_client = _clean(payload.get("client"), 40).lower() in {"android_native", "android-native"}
        room_url = self._mobile_room_url_via_gateway(room_url)
        if (
            mode == "call"
            and urlsplit(room_url).scheme.lower() != "https"
            and not (native_client and self._mobile_proxy_rooms_enabled())
        ):
            revoker = getattr(together, "_revoke_unused_ticket", None)
            if callable(revoker):
                revoker(ticket)
            return self._mobile_json_error(
                "视频通话需要 HTTPS 安全地址，才能由手机浏览器安全申请摄像头和麦克风",
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

    async def mobile_create_game_room(self) -> tuple[dict[str, Any], int]:
        auth = self._mobile_authorize()
        if not auth:
            return self._mobile_json_error("未授权的移动端请求", 401)
        game = self._mobile_find_plugin("astrbot_plugin_game_companion")
        if game is None:
            return self._mobile_json_error("未安装或未加载游戏伴侣插件", 503)
        creator = getattr(game, "mobile_create_room", None)
        if not callable(creator):
            return self._mobile_json_error("游戏伴侣版本过旧，缺少手机房间能力", 503)
        payload = await self._mobile_request_payload()
        game_type = _clean(payload.get("game_type"), 32)
        try:
            result = await self._mobile_call_gateway_aware(
                creator,
                _clean(auth.get("user_id"), 120),
                game_type,
            )
        except (ValueError, PermissionError) as exc:
            return self._mobile_json_error(str(exc), 409)
        except (RuntimeError, OSError) as exc:
            return self._mobile_json_error(str(exc), 503)
        except Exception as exc:
            logger.exception("[RealityCompanion] 手机游戏房间创建失败: %s", _clean(exc, 180))
            return self._mobile_json_error("手机游戏房间创建失败", 500)
        if not isinstance(result, dict) or not result.get("url"):
            return self._mobile_json_error("游戏伴侣没有返回可用房间链接", 503)
        result = dict(result)
        result["url"] = self._mobile_room_url_via_gateway(str(result.get("url") or ""))
        return {"ok": True, "data": result}, 200

    async def mobile_prepare_rooms(self) -> tuple[dict[str, Any], int]:
        """Warm mobile HTTPS room access without issuing a room ticket."""
        auth = self._mobile_authorize()
        if not auth:
            return self._mobile_json_error("未授权的移动端请求", 401)
        together = self._mobile_find_plugin("astrbot_plugin_together_companion")
        if together is None:
            return self._mobile_json_error("未安装或未加载一起房间插件", 503)
        if together._get_chat_provider() is None:
            return self._mobile_json_error("一起房间尚未配置实时对话模型", 409)
        async with self._mobile_room_start_lock:
            try:
                access = await self._mobile_prepare_together_access(together)
            except Exception as exc:
                return self._mobile_json_error(f"共同房间预热失败：{_clean(exc, 180)}", 503)
        return {"ok": True, "data": {"ready": bool(access.get("tunnel_ready", False)) if isinstance(access, dict) else True}}, 200

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

    # ------------------------------------------------------------------
    # 统一房间代理：手机只连移动网关，房间页面/接口/媒体/WS 由网关转发。
    # 路径原样保持（/join、/ws、/media、/avatar → 一起；/room/<token>、
    # /api/room → 游戏），因此新旧客户端与网页相对路径都无需改动。
    # ------------------------------------------------------------------

    def _mobile_proxy_rooms_enabled(self) -> bool:
        return bool(self._cfg_bool("mobile.proxy_rooms", True))

    def _mobile_room_upstream(self, service: str) -> str:
        if service in self._mobile_room_upstream_cache:
            return self._mobile_room_upstream_cache[service]
        plugin_name = {
            "together": "astrbot_plugin_together_companion",
            "game": "astrbot_plugin_game_companion",
        }.get(service, "")
        if not plugin_name:
            return ""
        plugin = self._mobile_find_plugin(plugin_name)
        base = ""
        if plugin is not None:
            room_server = getattr(plugin, "room_server", None)
            base = str(getattr(room_server, "local_base_url", "") or "")
            if not base:
                getter = getattr(plugin, "_room_base_url", None)
                base = str(getter() or "") if callable(getter) else ""
        base = base.strip().rstrip("/")
        try:
            parsed = urlsplit(base)
            if parsed.hostname in {"0.0.0.0", ""}:
                # 绑定 0.0.0.0 时本机回环可达；网关代为转发后手机不再感知
                netloc = "127.0.0.1" if parsed.port is None else f"127.0.0.1:{parsed.port}"
                base = urlunsplit((parsed.scheme or "http", netloc, parsed.path, "", ""))
        except Exception:
            pass
        if base:
            self._mobile_room_upstream_cache[service] = base
        return base

    @staticmethod
    def _mobile_room_upstream_origin(upstream: str) -> str:
        """Return the origin that the local room service sees behind the proxy."""
        try:
            parsed = urlsplit(str(upstream or ""))
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        except Exception:
            pass
        return ""

    def _mobile_room_proxy_headers(self, aio_request: Any, upstream: str) -> dict[str, str]:
        """Forward browser headers while satisfying the upstream origin check."""
        forward_headers: dict[str, str] = {}
        for key in ("Range", "Content-Type", "Accept", "Authorization", "Referer", "User-Agent"):
            value = aio_request.headers.get(key)
            if value:
                forward_headers[key] = value
        upstream_origin = self._mobile_room_upstream_origin(upstream)
        if upstream_origin:
            # The browser Origin is the mobile gateway, but the room service
            # validates same-origin requests against its own local listener.
            forward_headers["Origin"] = upstream_origin
        return forward_headers

    def _mobile_room_url_via_gateway(self, room_url: str) -> str:
        """把房间链接改写为移动网关自身地址，路径与查询原样保留。"""
        fallback = self._mobile_rewrite_room_url(room_url)
        if not self._mobile_proxy_rooms_enabled():
            return fallback
        try:
            parsed = urlsplit(str(room_url or ""))
            if not parsed.path:
                return fallback
            host = self._mobile_normalize_host(self._mobile_request_host())
            if not host:
                return fallback
            port = self._mobile_server_bound_port or self._mobile_port()
            netloc = f"[{host}]" if ":" in host else host
            query = f"?{parsed.query}" if parsed.query else ""
            return f"http://{netloc}:{port}{parsed.path}{query}"
        except Exception:
            return fallback

    async def _mobile_room_proxy(self, aio_request: Any, service: str) -> Any:
        if web is None or aiohttp is None:  # pragma: no cover - startup guarded
            return web.Response(status=503, text="aiohttp unavailable", content_type="text/plain")
        upstream = self._mobile_room_upstream(service)
        if not upstream:
            return web.Response(
                status=503,
                text=f"{service} 房间服务不可用",
                content_type="text/plain",
            )
        target = upstream + aio_request.path
        if aio_request.query_string:
            target = f"{target}?{aio_request.query_string}"
        forward_headers = self._mobile_room_proxy_headers(aio_request, upstream)
        body = await aio_request.read() if aio_request.can_read_body else None
        session = self._mobile_proxy_session or aiohttp.ClientSession()
        owns_session = session is self._mobile_proxy_session
        try:
            async with session.request(
                aio_request.method,
                target,
                headers=forward_headers,
                data=body,
                allow_redirects=True,
            ) as response:
                if response.status >= 400:
                    error_body = await response.read()
                    return web.Response(
                        status=response.status,
                        body=error_body[:MOBILE_MAX_BODY_BYTES],
                        content_type=response.content_type or "text/plain",
                    )
                stream = web.StreamResponse(status=response.status, reason=response.reason)
                for header in (
                    "Content-Type",
                    "Content-Range",
                    "Accept-Ranges",
                    "Cache-Control",
                    "Content-Disposition",
                ):
                    value = response.headers.get(header)
                    if value:
                        stream.headers[header] = value
                await stream.prepare(aio_request)
                async for chunk in response.content.iter_chunked(64 * 1024):
                    await stream.write(chunk)
                await stream.write_eof()
                return stream
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[RealityCompanion] 房间代理转发失败: service=%s path=%s error=%s",
                service,
                _clean(aio_request.path, 120),
                _clean(exc, 160),
            )
            return web.Response(status=502, text="房间代理转发失败", content_type="text/plain")
        finally:
            if not owns_session:
                await session.close()

    async def _mobile_room_assets_proxy(self, aio_request: Any) -> Any:
        # 一起与游戏的房间页面都引用 /assets/<name>：按 Referer 区分来源，
        # 无 Referer 时先试一起，404 再试游戏。
        referer = str(aio_request.headers.get("Referer") or "")
        if "/room/" in referer:
            return await self._mobile_room_proxy(aio_request, "game")
        if "/join/" in referer:
            return await self._mobile_room_proxy(aio_request, "together")
        first = await self._mobile_room_proxy(aio_request, "together")
        if first.status < 400:
            return first
        second = await self._mobile_room_proxy(aio_request, "game")
        return second if second.status < 400 else first

    async def _mobile_room_ws_proxy(self, aio_request: Any) -> Any:
        if web is None or aiohttp is None:  # pragma: no cover - startup guarded
            return web.Response(status=503, text="aiohttp unavailable", content_type="text/plain")
        upstream = self._mobile_room_upstream("together")
        if not upstream:
            return web.Response(status=503, text="together 房间服务不可用", content_type="text/plain")
        server_ws = web.WebSocketResponse(
            heartbeat=20.0,
            autoping=True,
            max_msg_size=MOBILE_PROXY_MAX_WS_BYTES,
        )
        await server_ws.prepare(aio_request)
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=10.0))
        upstream_ws = None
        try:
            headers = {"Origin": upstream}
            for key, value in aio_request.headers.items():
                if key.lower().startswith("x-together"):
                    headers[key] = value
            target = f"{upstream}/ws"
            if aio_request.query_string:
                target = f"{target}?{aio_request.query_string}"
            upstream_ws = await session.ws_connect(
                target,
                headers=headers,
                heartbeat=20.0,
                max_msg_size=MOBILE_PROXY_MAX_WS_BYTES,
            )

            async def pump(source: Any, sink: Any) -> None:
                async for message in source:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await sink.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await sink.send_bytes(message.data)
                    else:
                        break

            tasks = [
                asyncio.create_task(pump(server_ws, upstream_ws)),
                asyncio.create_task(pump(upstream_ws, server_ws)),
            ]
            try:
                _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
            finally:
                for task in tasks:
                    task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[RealityCompanion] 房间 WS 代理失败: %s", _clean(exc, 160))
        finally:
            for closer in (upstream_ws, session):
                try:
                    if closer is not None:
                        await closer.close()
                except Exception:
                    pass
            try:
                await server_ws.close()
            except Exception:
                pass
        return server_ws

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
        raw_place = payload.get("place") if isinstance(payload.get("place"), dict) else {}
        place = {
            "matched": bool(raw_place.get("matched")),
            "name": _clean(raw_place.get("name"), 40),
            "kind": _clean(raw_place.get("kind"), 24),
            "distance_m": max(0.0, min(100_000.0, _number(raw_place.get("distance_m")))),
            "radius_m": max(20.0, min(5_000.0, _number(raw_place.get("radius_m"), 150.0))),
            "confidence": _clean(raw_place.get("confidence"), 32),
            "aliases": [
                _clean(item, 40)
                for item in list(raw_place.get("aliases") or [])[:8]
                if _clean(item, 40)
            ],
            "parent_name": _clean(raw_place.get("parent_name"), 40),
        }
        item = {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": max(0.0, min(100_000.0, accuracy)) if math.isfinite(accuracy) else 0.0,
            "altitude_m": altitude if altitude is not None and math.isfinite(altitude) else None,
            "speed_mps": max(0.0, min(1000.0, speed)) if math.isfinite(speed) else 0.0,
            "bearing": bearing % 360.0 if bearing is not None and math.isfinite(bearing) else None,
            "label": _clean(payload.get("label"), 40),
            "place": place,
            "captured_at": captured_at,
            "received_at": now,
        }
        with self._mobile_state_lock:
            self._mobile_locations[user_id] = item
        self._schedule_private_companion_location_notification(user_id)
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

    async def mobile_location_heartbeat(self) -> tuple[dict[str, Any], int]:
        auth = self._mobile_authorize()
        if not auth:
            return self._mobile_json_error("未授权的移动端请求", 401)
        user_id = _clean(auth.get("user_id"), 120)
        with self._mobile_state_lock:
            item = self._mobile_locations.get(user_id)
            if not item:
                return self._mobile_json_error("当前没有可保活的位置，请重新获取定位", 409)
            item["received_at"] = time.time()
        return {"ok": True, "data": self._mobile_location_snapshot(user_id)}, 200

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
            ("POST", "/game/room/create", self.mobile_create_game_room),
            ("POST", "/room/prepare", self.mobile_prepare_rooms),
            ("POST", "/location", self.mobile_location),
            ("POST", "/location/heartbeat", self.mobile_location_heartbeat),
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

        # 统一房间代理：路径与各房间服务保持一致，注册在自有路由之后。
        # /room/{access_token} 与 POST /room/create 方法不同，aiohttp 会继续
        # 匹配后续动态路由，互不冲突。
        if self._mobile_proxy_rooms_enabled() and aiohttp is not None:
            proxy_routes = (
                ("*", "/join/{tail:.*}", functools.partial(self._mobile_room_proxy, service="together")),
                ("GET", "/media/{tail:.*}", functools.partial(self._mobile_room_proxy, service="together")),
                ("GET", "/avatar", functools.partial(self._mobile_room_proxy, service="together")),
                ("*", "/assets/{name}", self._mobile_room_assets_proxy),
                ("*", "/api/room/{tail:.*}", functools.partial(self._mobile_room_proxy, service="game")),
                ("GET", "/room/{access_token}", functools.partial(self._mobile_room_proxy, service="game")),
                ("GET", "/ws", self._mobile_room_ws_proxy),
            )
            for method, path, handler in proxy_routes:
                app.router.add_route(method, path, handler)
            self._mobile_proxy_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=10.0),
            )
            logger.info("[RealityCompanion] 移动网关已启用统一房间代理")

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
        session = self._mobile_proxy_session
        self._mobile_proxy_session = None
        try:
            if session is not None:
                await session.close()
        except Exception:
            pass
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
        register_api(f"{prefix}/game/room/create", route(self.mobile_create_game_room), ["POST"], "Reality Companion mobile game room")
        register_api(f"{prefix}/room/prepare", route(self.mobile_prepare_rooms), ["POST"], "Reality Companion warm mobile room")
        register_api(f"{prefix}/location", route(self.mobile_location), ["POST"], "Reality Companion mobile location")
        register_api(f"{prefix}/location/heartbeat", route(self.mobile_location_heartbeat), ["POST"], "Reality Companion mobile location heartbeat")
        register_api(f"{prefix}/location/revoke", route(self.mobile_revoke_location), ["POST"], "Reality Companion revoke mobile location")
        register_api(f"{prefix}/screen/heartbeat", route(self.mobile_screen_heartbeat), ["POST"], "Reality Companion mobile screen status")
        register_api(f"{prefix}/session/close", route(self.mobile_close_session), ["POST"], "Reality Companion mobile close")
