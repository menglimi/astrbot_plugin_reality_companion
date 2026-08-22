# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import base64
import copy
import importlib
import json
import os
import re
import secrets
import sys
import time
import uuid
import zoneinfo
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.api.web import request
except Exception:  # pragma: no cover - allows isolated tests without AstrBot
    request = None

from .helpers import _now_ts, _safe_float, _safe_int, _single_line
from .mobile_gateway import MOBILE_API_VERSION, MobileGatewayMixin
from .wakeup_alarm import WakeupAlarmMixin


PLUGIN_NAME = "astrbot_plugin_reality_companion"
PLUGIN_VERSION = "0.2.8"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"
MANAGED_PAGE_MESSAGE = (
    "当前能力已由“我会永远陪着你”统一管理，请前往陪伴插件的“陪伴面板”继续操作。"
)
_active_plugin: "RealityCompanionPlugin | None" = None
_MISSING = object()

_CAMERA_CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "index": 0,
    "min_interval_seconds": 60,
    "capture_timeout_seconds": 5,
    "analysis_timeout_seconds": 25,
    "proactive_curiosity_enabled": False,
    "proactive_min_tier": 4,
    "proactive_max_daily": 1,
    "proactive_cooldown_minutes": 240,
}
_CAMERA_CONFIG_BOUNDS: dict[str, tuple[int, int]] = {
    "index": (0, 100000),
    "min_interval_seconds": (10, 3600),
    "capture_timeout_seconds": (2, 20),
    "analysis_timeout_seconds": (5, 90),
    "proactive_min_tier": (1, 5),
    "proactive_max_daily": (0, 10),
    "proactive_cooldown_minutes": (10, 1440),
}
_LEGACY_CAMERA_CONFIG_KEYS: dict[str, str] = {
    "enabled": "camera_enabled",
    "index": "camera_index",
    "min_interval_seconds": "camera_min_interval_seconds",
    "capture_timeout_seconds": "camera_capture_timeout_seconds",
    "analysis_timeout_seconds": "camera_analysis_timeout_seconds",
    "proactive_curiosity_enabled": "camera_proactive_curiosity_enabled",
    "proactive_min_tier": "camera_proactive_min_tier",
    "proactive_max_daily": "camera_proactive_max_daily",
    "proactive_cooldown_minutes": "camera_proactive_cooldown_minutes",
}
_AUDIO_CONFIG_DEFAULTS: dict[str, Any] = {"default_playback_volume": 35}


def _reality_touch_tool_name(tool: Any) -> str:
    return _single_line(getattr(tool, "name", ""), 80)


def _reality_touch_camera_tool_payload(tool_result: Any) -> dict[str, Any] | None:
    """Read the camera receipt from AstrBot's CallToolResult or test-friendly forms."""
    candidates: list[Any] = [tool_result]
    if not isinstance(tool_result, (str, bytes, dict)):
        candidates.extend(list(getattr(tool_result, "content", []) or []))
        structured = getattr(tool_result, "structuredContent", None)
        if structured is not None:
            candidates.append(structured)
    for candidate in candidates:
        if isinstance(candidate, dict):
            if "status" in candidate or "answer_available" in candidate:
                return candidate
            text = candidate.get("text")
        elif isinstance(candidate, bytes):
            text = candidate.decode("utf-8", errors="replace")
        elif isinstance(candidate, str):
            text = candidate
        else:
            text = getattr(candidate, "text", None)
        if not isinstance(text, str):
            continue
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, dict) and (
            "status" in decoded or "answer_available" in decoded
        ):
            return decoded
    return None


def _reality_touch_camera_reply_needs_uncertain_fallback(text: str) -> bool:
    """Only catch explanations and guesses forbidden by an uncertain camera receipt."""
    return bool(
        re.search(
            r"(?:画面.{0,5}(?:黑|糊|模糊)|镜头.{0,5}(?:挡|遮)|看错了?|"
            r"(?:又在)?吃(?:什么)?(?:不健康|东西)|老实交代|快点.{0,6}交代|"
            r"你(?:刚才)?给我看了什么)",
            str(text or ""),
        )
    )


def get_reality_companion_api() -> Any | None:
    plugin = _active_plugin
    return getattr(plugin, "extension_api", None) if plugin is not None else None


class RealityCompanionExtensionAPI:
    """Stable bridge used by the companion series without exposing mutable state."""

    def __init__(self, plugin: "RealityCompanionPlugin") -> None:
        self._plugin = plugin

    def status(self) -> dict[str, Any]:
        return self._plugin.integration_status()

    def page_snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._plugin._reality_touch_page_snapshot())

    def list_external_reality_capabilities(self) -> list[dict[str, Any]]:
        """Expose provider metadata without exposing provider internals."""
        api = self._plugin._private_companion_api()
        getter = getattr(api, "list_reality_touch_providers", None) if api is not None else None
        result = getter() if callable(getter) else []
        return result if isinstance(result, list) else []

    async def call_external_reality_capability(
        self,
        provider: str,
        operation: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a registered provider; failures stay isolated from audio/chat."""
        api = self._plugin._private_companion_api()
        caller = getattr(api, "call_reality_touch_provider", None) if api is not None else None
        if not callable(caller):
            return {"ok": False, "reason": "private_companion_unavailable"}
        normalized_operation = _single_line(operation, 64).lower()
        request = dict(payload or {})
        if normalized_operation in {"run_scene", "control_device", "get_health_summary"}:
            user_id = _single_line(request.get("user_id"), 120)
            context_getter = getattr(api, "get_reality_touch_host_context", None)
            context = context_getter(user_id) if callable(context_getter) else {}
            if not isinstance(context, dict) or context.get("eligible") is not True:
                return {"ok": False, "reason": "user_not_authorized"}
            if normalized_operation in {"run_scene", "control_device"} and request.get("confirmed") is not True:
                return {"ok": False, "reason": "explicit_confirmation_required"}
        try:
            result = await caller(provider, normalized_operation, request)
        except Exception:
            return {"ok": False, "reason": "provider_call_failed"}
        return result if isinstance(result, dict) else {"ok": bool(result)}

    async def resolve_external_reality_request(self, user_id: str, request: str) -> dict[str, Any]:
        """Let the companion model translate a natural-language home/health request."""
        api = self._plugin._private_companion_api()
        resolver = getattr(api, "resolve_reality_touch_request", None) if api is not None else None
        if not callable(resolver):
            return {"ok": False, "reason": "private_companion_planner_unavailable"}
        context_getter = getattr(api, "get_reality_touch_host_context", None)
        context = context_getter(user_id) if callable(context_getter) else {}
        if not isinstance(context, dict) or context.get("eligible") is not True:
            return {"ok": False, "reason": "user_not_authorized"}
        try:
            result = await resolver(_single_line(user_id, 120), _single_line(request, 500))
        except Exception:
            return {"ok": False, "reason": "planner_failed"}
        return result if isinstance(result, dict) else {"ok": bool(result)}

    async def page_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._plugin._perform_page_action(dict(payload or {}))

    def audio_consented(self, user_id: str) -> bool:
        user = self._plugin._user(user_id, create=False)
        return bool(user and self._plugin._reality_touch_audio_consented(user))

    def camera_user_eligible(self, user_id: str) -> bool:
        return self._plugin._reality_touch_camera_user_eligible(user_id)

    def camera_proactive_state(self, user_id: str) -> dict[str, Any]:
        user = self._plugin._user(user_id, create=False)
        if not user:
            return {"available": False, "direct_allowed": False, "reason": "user_missing"}
        return self._plugin._reality_touch_camera_proactive_state(user, user_id=user_id)

    def camera_proactive_prompt(self, user_id: str) -> str:
        user = self._plugin._user(user_id, create=False)
        return self._plugin._reality_touch_camera_proactive_prompt(user, user_id=user_id) if user else ""

    def proactive_voice_allowed(self, user_id: str) -> bool:
        user = self._plugin._user(user_id, create=False)
        return bool(user and self._plugin._reality_touch_proactive_voice_allowed(user))

    async def mirror_proactive_voice(self, user_id: str, audio_path: str) -> bool:
        user = self._plugin._user(user_id, create=False)
        return bool(user and await self._plugin._mirror_reality_touch_proactive_voice(user, audio_path))

    async def schedule_reminder(
        self,
        user_id: str,
        payload: dict[str, Any],
        *,
        source_text: str,
        trigger_umo: str = "",
    ) -> bool:
        user = self._plugin._user(user_id)
        if trigger_umo:
            user["umo"] = _single_line(trigger_umo, 180)
        self._plugin._save_data_sync()
        return await self._plugin._schedule_reality_touch_official_reminder(
            user_id,
            payload,
            source_text=source_text,
            trigger_umo=trigger_umo,
        )

    async def camera_snapshot(self, user_id: str, purpose: str, *, source: str = "manual") -> dict[str, Any]:
        return await self._plugin._reality_touch_camera_snapshot_for_user(
            user_id,
            purpose,
            source=source,
        )

    def legacy_command(self, user_id: str, value: str, *, umo: str = "") -> tuple[str, Any]:
        user = self._plugin._user(user_id)
        if umo:
            user["umo"] = _single_line(umo, 180)
        result = self._plugin._wakeup_alarm_command(user, value)
        self._plugin._save_data_sync()
        return result

    async def test_wakeup(self, user_id: str) -> None:
        user = self._plugin._user(user_id)
        await self._plugin._test_wakeup_alarm(user)

    def mobile_context(self, user_id: str = "") -> dict[str, Any]:
        return self._plugin.mobile_context(user_id)

    async def record_reality_touch_output(
        self,
        user_id: str,
        text: str,
        *,
        source: str = "reality_touch_audio",
        delivered_at: float | None = None,
    ) -> dict[str, Any]:
        """Persist cross-device output in the reality plugin's own store."""
        normalized_user_id = _single_line(user_id, 120)
        visible = _single_line(text, 500)
        if not normalized_user_id or not visible:
            return {"recorded": False, "reason": "invalid_payload"}
        timestamp = _safe_float(delivered_at, _now_ts(), 0.0) or _now_ts()
        host_identity = self._plugin._host_identity(normalized_user_id)
        subject_ref = _single_line(host_identity.get("reality_subject_ref"), 160) or normalized_user_id
        outputs = self._plugin.data.setdefault("reality_touch_outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}
            self._plugin.data["reality_touch_outputs"] = outputs
        output = {
            "text": visible,
            "source": _single_line(source, 80) or "reality_touch_audio",
            "delivered_at": timestamp,
        }
        outputs[subject_ref] = output
        user = self._plugin._user(normalized_user_id, create=True)
        user["last_proactive_message"] = visible
        user["last_proactive_sent_at"] = timestamp
        self._plugin._save_data_sync()
        return {"recorded": True, "user_id": normalized_user_id, "delivered_at": timestamp}

    def recent_output(self, user_id: str) -> dict[str, Any]:
        """Return a detached continuity record owned by this plugin."""
        normalized = _single_line(user_id, 120)
        host_identity = self._plugin._host_identity(normalized)
        subject_ref = _single_line(host_identity.get("reality_subject_ref"), 160) or normalized
        outputs = self._plugin.data.get("reality_touch_outputs")
        output = outputs.get(subject_ref) if isinstance(outputs, dict) else None
        if not isinstance(output, dict):
            user = self._plugin._user(normalized, create=False)
            output = user.get("last_reality_touch_output") if isinstance(user, dict) else None
        return copy.deepcopy(output) if isinstance(output, dict) else {}

    def apply_pending_confirmation(self, user_id: str, text: str) -> str | None:
        user = self._plugin._user(user_id, create=False)
        return self._plugin._reality_touch_apply_pending_confirmation(user, text) if user else None


@register(
    PLUGIN_NAME,
    "menglimi",
    "我会来到你身边：本机音频、摄像头单帧与现实提醒联动。",
    PLUGIN_VERSION,
)
class RealityCompanionPlugin(MobileGatewayMixin, WakeupAlarmMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        global _active_plugin
        super().__init__(context)
        self.config = config
        self.extension_api = RealityCompanionExtensionAPI(self)
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "reality_companion.json"
        self.data = self._load_data()
        self._data_lock = asyncio.Lock()
        self._save_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._wakeup_contact_tasks: dict[str, asyncio.Task] = {}
        self._reality_touch_camera_operation_lock = asyncio.Lock()
        self._reality_touch_camera_continuations: dict[str, dict[str, Any]] = {}
        self._mobile_gateway_init()
        self._legacy_migration_attempted = False
        self._sync_runtime_config()
        self.plugin_vision_provider_id = self._cfg_str("vision_provider_id", "")
        self.environment_perception_timezone = self._cfg_str("timezone", "Asia/Shanghai") or "Asia/Shanghai"
        self.check_interval_seconds = 30
        self.authorized_user_ids = {
            _single_line(item, 120)
            for item in self._cfg("authorized_user_ids", [])
            if _single_line(item, 120)
        }
        self._register_page_api()
        _active_plugin = self

    def _cfg(self, dotted_key: str, default: Any = None) -> Any:
        if dotted_key in self.config:
            return self.config.get(dotted_key, default)
        current: Any = self.config
        for part in dotted_key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current.get(part)
        return default if current is None else current

    def _cfg_str(self, key: str, default: str = "") -> str:
        return str(self._cfg(key, default) or "").strip()

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return self._coerce_config_bool(self._cfg(key, default), default)

    @staticmethod
    def _coerce_config_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "y", "on", "enable", "enabled", "启用", "开启", "开", "是"}:
                return True
            if lowered in {"false", "0", "no", "n", "off", "disable", "disabled", "停用", "关闭", "关", "否", ""}:
                return False
        return default if value is None else bool(value)

    def _cfg_int(self, key: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
        return _safe_int(self._cfg(key, default), default, minimum, maximum)

    def _sync_runtime_config(self) -> None:
        """Refresh device runtime fields after config mutation or startup."""
        self.enable_experimental_bluetooth_wakeup = self._cfg_bool("enabled", False)
        self.enable_reality_touch_camera = self._cfg_bool("camera.enabled", False)
        self.reality_touch_camera_index = self._cfg_int("camera.index", 0, 0, 100000)
        self.reality_touch_camera_min_interval_seconds = self._cfg_int(
            "camera.min_interval_seconds", 60, 10, 3600
        )
        self.reality_touch_camera_capture_timeout_seconds = self._cfg_int(
            "camera.capture_timeout_seconds", 5, 2, 20
        )
        self.reality_touch_camera_analysis_timeout_seconds = self._cfg_int(
            "camera.analysis_timeout_seconds", 25, 5, 90
        )
        self.enable_reality_touch_camera_proactive_curiosity = self._cfg_bool(
            "camera.proactive_curiosity_enabled", False
        )
        self.reality_touch_camera_proactive_min_tier = self._cfg_int(
            "camera.proactive_min_tier", 4, 1, 5
        )
        self.reality_touch_camera_proactive_max_daily = self._cfg_int(
            "camera.proactive_max_daily", 1, 0, 10
        )
        self.reality_touch_camera_proactive_cooldown_minutes = self._cfg_int(
            "camera.proactive_cooldown_minutes", 240, 10, 1440
        )
        self.tts_local_playback_volume = self._cfg_int(
            "audio.default_playback_volume", 35, 0, 100
        )

    def _load_data(self) -> dict[str, Any]:
        if not self.data_file.is_file():
            return {"version": 1, "users": {}, "reality_touch": {}, "reality_touch_outputs": {}}
        try:
            loaded = json.loads(self.data_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                loaded.setdefault("version", 1)
                loaded.setdefault("users", {})
                loaded.setdefault("reality_touch", {})
                loaded.setdefault("reality_touch_outputs", {})
                return loaded
        except Exception as exc:
            logger.warning("[RealityCompanion] 读取数据失败，将使用空数据: %s", _single_line(exc, 160))
        return {"version": 1, "users": {}, "reality_touch": {}, "reality_touch_outputs": {}}

    def _save_data_sync(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Saves can arrive from both the event loop and a background thread.
        # A shared temp filename lets concurrent writers delete each other's
        # source before os.replace runs.
        temp = self.data_file.with_name(
            f".{self.data_file.name}.{uuid.uuid4().hex}.tmp"
        )
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        try:
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.data_file)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _schedule_data_save(self, delay: float = 0.2) -> None:
        if isinstance(self._save_task, asyncio.Task) and not self._save_task.done():
            return

        async def delayed_save() -> None:
            await asyncio.sleep(max(0.0, delay))
            await asyncio.to_thread(self._save_data_sync)

        self._save_task = self._create_lifecycle_background_task(delayed_save(), label="save_data")

    def _create_lifecycle_background_task(self, awaitable: Any, *, label: str = "background") -> asyncio.Task:
        task = asyncio.create_task(awaitable, name=f"reality_companion:{label}")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _user(self, user_id: Any, *, create: bool = True) -> dict[str, Any] | None:
        normalized = _single_line(user_id, 120)
        if not normalized:
            return None
        users = self.data.setdefault("users", {})
        if not isinstance(users, dict):
            users = {}
            self.data["users"] = users
        user = users.get(normalized)
        if not isinstance(user, dict):
            if not create:
                return None
            user = {"user_id": normalized}
            users[normalized] = user
        user.setdefault("user_id", normalized)
        return user

    async def initialize(self) -> None:
        await self._try_legacy_migration()
        await self._start_mobile_server()
        self._create_lifecycle_background_task(self._alarm_loop(), label="alarm_loop")
        if not self.data.get("legacy_migration_completed"):
            self._create_lifecycle_background_task(self._migration_loop(), label="legacy_migration")
        logger.info(
            "[RealityCompanion] 已加载: enabled=%s camera=%s linked=%s",
            self.enable_experimental_bluetooth_wakeup,
            self.enable_reality_touch_camera,
            self._private_companion_api() is not None,
        )

    async def _migration_loop(self) -> None:
        """Retry once after AstrBot finishes loading the companion host plugin."""
        for _ in range(24):
            if self.data.get("legacy_migration_completed"):
                return
            if await self._try_legacy_migration():
                return
            await asyncio.sleep(5)

    async def terminate(self) -> None:
        global _active_plugin
        try:
            await self._stop_mobile_server()
        except Exception as exc:
            logger.warning("[RealityCompanion] 停止移动端网关失败: %s", _single_line(exc, 180))
        tasks = list(self._background_tasks) + list(self._wakeup_contact_task_registry().values())
        for task in tasks:
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._wakeup_contact_tasks.clear()
        await asyncio.to_thread(self._save_data_sync)
        if _active_plugin is self:
            _active_plugin = None

    async def _alarm_loop(self) -> None:
        while True:
            try:
                await self._run_wakeup_alarm_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[RealityCompanion] 起床提醒轮询失败: %s", _single_line(exc, 160))
            await asyncio.sleep(30)

    def _private_companion_api(self) -> Any | None:
        module_names = (
            "data.plugins.astrbot_plugin_private_companion.main",
            "astrbot_plugin_private_companion.main",
        )
        suffixes = tuple(name.removeprefix("data.plugins.") for name in module_names)
        modules = [sys.modules.get(name) for name in module_names]
        modules.extend(
            module
            for name, module in list(sys.modules.items())
            if module is not None and any(name.endswith(suffix) for suffix in suffixes)
        )
        for module in modules:
            if module is None:
                continue
            try:
                namespace = object.__getattribute__(module, "__dict__")
            except Exception:
                namespace = {}
            getter = namespace.get("get_private_companion_api") if isinstance(namespace, dict) else None
            try:
                api = getter() if callable(getter) else None
            except Exception:
                api = None
            if api is not None:
                return api
        getter = getattr(self.context, "get_registered_star", None)
        if callable(getter):
            try:
                metadata = getter("astrbot_plugin_private_companion")
                instance = getattr(metadata, "star_cls", None) if metadata is not None else None
                return getattr(instance, "extension_api", None)
            except Exception:
                pass
        return None

    def _normalize_group_config_value(self, group_key: str, key: str, value: Any) -> Any:
        defaults = _CAMERA_CONFIG_DEFAULTS if group_key == "camera" else _AUDIO_CONFIG_DEFAULTS
        default = defaults[key]
        if isinstance(default, bool):
            return self._coerce_config_bool(value, default)
        if group_key == "camera":
            minimum, maximum = _CAMERA_CONFIG_BOUNDS[key]
        else:
            minimum, maximum = 0, 100
        return _safe_int(value, int(default), minimum, maximum)

    def _group_config_field_values(self, group_key: str, key: str) -> list[Any]:
        values: list[Any] = []
        dotted_key = f"{group_key}.{key}"
        if dotted_key in self.config:
            values.append(self.config.get(dotted_key))
        group = self.config.get(group_key)
        if isinstance(group, dict) and key in group:
            values.append(group.get(key))
        return values

    def _group_config_is_default(self, group_key: str, defaults: dict[str, Any]) -> bool:
        for key, default in defaults.items():
            for value in self._group_config_field_values(group_key, key):
                if self._normalize_group_config_value(group_key, key, value) != default:
                    return False
        return True

    def _set_group_config_field(self, group_key: str, key: str, value: Any) -> bool:
        changed = False
        dotted_key = f"{group_key}.{key}"
        dotted_present = dotted_key in self.config
        if dotted_present and self.config.get(dotted_key) != value:
            self.config[dotted_key] = value
            changed = True

        group = self.config.get(group_key)
        if isinstance(group, dict):
            if group.get(key, _MISSING) != value:
                group[key] = value
                changed = True
        elif not dotted_present:
            self.config[group_key] = {key: value}
            changed = True
        return changed

    def _merge_legacy_config_group(
        self,
        group_key: str,
        defaults: dict[str, Any],
        legacy_keys: dict[str, str],
        legacy_config: dict[str, Any],
    ) -> bool:
        whole_group_is_default = self._group_config_is_default(group_key, defaults)
        changed = False
        for target_key, legacy_key in legacy_keys.items():
            if legacy_key not in legacy_config:
                continue
            field_exists = bool(self._group_config_field_values(group_key, target_key))
            if not whole_group_is_default and field_exists:
                continue
            value = self._normalize_group_config_value(
                group_key,
                target_key,
                legacy_config.get(legacy_key),
            )
            changed = self._set_group_config_field(group_key, target_key, value) or changed
        return changed

    def _migrate_legacy_config(self, legacy_config: dict[str, Any]) -> bool:
        """Import defaults only when the new config has no explicit choice."""
        if not isinstance(self.config, dict) or not isinstance(legacy_config, dict):
            return False

        changed = False
        if "enabled" in legacy_config:
            current = self.config.get("enabled", _MISSING)
            current_is_default = current is _MISSING or not self._coerce_config_bool(current, False)
            if current_is_default:
                migrated_enabled = self._coerce_config_bool(legacy_config.get("enabled"), False)
                if current is _MISSING or current != migrated_enabled:
                    self.config["enabled"] = migrated_enabled
                    changed = True

        changed = self._merge_legacy_config_group(
            "camera",
            _CAMERA_CONFIG_DEFAULTS,
            _LEGACY_CAMERA_CONFIG_KEYS,
            legacy_config,
        ) or changed
        changed = self._merge_legacy_config_group(
            "audio",
            _AUDIO_CONFIG_DEFAULTS,
            {"default_playback_volume": "audio_default_playback_volume"},
            legacy_config,
        ) or changed
        return changed

    async def _try_legacy_migration(self) -> bool:
        if self.data.get("legacy_migration_completed"):
            return True
        api = self._private_companion_api()
        exporter = getattr(api, "export_reality_touch_legacy_state", None) if api is not None else None
        if not callable(exporter):
            return False
        try:
            legacy = exporter()
        except Exception as exc:
            logger.debug("[RealityCompanion] 旧数据迁移暂不可用: %s", exc)
            return False
        if not isinstance(legacy, dict):
            return False
        users = legacy.get("users") if isinstance(legacy.get("users"), dict) else {}
        for user_id, fields in users.items():
            if not isinstance(fields, dict):
                continue
            user = self._user(user_id)
            for key, value in fields.items():
                if key not in user:
                    user[key] = copy.deepcopy(value)
        store = legacy.get("reality_touch")
        if isinstance(store, dict) and not self.data.get("reality_touch"):
            self.data["reality_touch"] = copy.deepcopy(store)
        legacy_config = legacy.get("config") if isinstance(legacy.get("config"), dict) else {}
        config_changed = self._migrate_legacy_config(legacy_config) if legacy_config else False
        self._sync_runtime_config()
        if config_changed:
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                try:
                    saver()
                except Exception as exc:
                    logger.debug("[RealityCompanion] 保存迁移配置失败: %s", _single_line(exc, 160))
        self.data["legacy_migration_completed"] = True
        self.data["legacy_migration_at"] = _now_ts()
        self._save_data_sync()
        logger.info("[RealityCompanion] 已从主插件迁移 %s 位用户的现实触及数据", len(users))
        return True

    def _host_identity(self, user_id: Any) -> dict[str, Any]:
        normalized = _single_line(user_id, 120)
        api = self._private_companion_api()
        getter = getattr(api, "get_reality_touch_host_context", None) if api is not None else None
        if callable(getter):
            try:
                result = getter(normalized)
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
        return {}

    def _permission_identity_id(self, user_id: Any) -> str:
        return _single_line(user_id, 120)

    def _is_configured_admin_user_id(self, user_id: Any) -> bool:
        normalized = _single_line(user_id, 120)
        return normalized in self.authorized_user_ids or bool(self._host_identity(normalized).get("is_admin"))

    def _relationship_owner_user_ids(self) -> set[str]:
        owners = set(self.authorized_user_ids)
        api = self._private_companion_api()
        getter = getattr(api, "get_reality_touch_authorized_user_ids", None) if api is not None else None
        if callable(getter):
            try:
                owners.update(_single_line(item, 120) for item in getter() if _single_line(item, 120))
            except Exception:
                pass
        return owners

    def _proactive_quota_policy(self, user: dict[str, Any]) -> dict[str, Any]:
        context = self._host_identity(user.get("user_id"))
        tier = _safe_int(context.get("proactive_tier"), 1, 1, 5)
        return {"tier": tier, "label": f"L{tier}"}

    def _environment_today_key(self) -> str:
        return self._wakeup_now().strftime("%Y-%m-%d")

    @staticmethod
    def _safe_event_is_private(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return False

    @staticmethod
    def _private_user_id_for_event(event: AstrMessageEvent) -> str:
        return _single_line(event.get_sender_id(), 120)

    def _provider_supports_image(self, provider: Any) -> bool:
        return provider is not None and callable(getattr(provider, "text_chat", None))

    def _record_llm_usage(self, **kwargs: Any) -> None:
        return None

    async def _synthesize_realtime_voice(self, text: str, **kwargs: Any) -> dict[str, Any]:
        api = self._private_companion_api()
        synthesizer = getattr(api, "synthesize_realtime_voice", None) if api is not None else None
        if not callable(synthesizer):
            return {"success": False, "error": "未连接我会永远陪着你，无法取得 TTS 音频"}
        kwargs["play_local"] = False
        return await synthesizer(text, **kwargs)

    async def _llm_call(self, prompt: str, **kwargs: Any) -> str:
        api = self._private_companion_api()
        caller = getattr(api, "generate_reality_touch_text", None) if api is not None else None
        if not callable(caller):
            return ""
        return str(await caller(prompt, **kwargs) or "")

    async def _resolve_proactive_persona_prompt(self, user: dict[str, Any], **kwargs: Any) -> str:
        api = self._private_companion_api()
        getter = getattr(api, "get_realtime_context", None) if api is not None else None
        if not callable(getter):
            return ""
        result = getter(_single_line(user.get("user_id"), 120), purpose="reality_touch")
        return str(result.get("prompt") or "") if isinstance(result, dict) else ""

    def _format_proactive_relationship_fact(self, user: dict[str, Any]) -> str:
        return _single_line(self._host_identity(user.get("user_id")).get("relationship"), 500)

    async def _recent_private_conversation_for_proactive_review(self, user: dict[str, Any], limit: int = 8) -> str:
        return _single_line(self._host_identity(user.get("user_id")).get("recent_dialogue"), 2400)

    def _task_provider(self, *provider_ids: Any) -> str:
        return next((_single_line(item, 160) for item in provider_ids if _single_line(item, 160)), "")

    def _official_cron_manager(self) -> Any | None:
        api = self._private_companion_api()
        getter = getattr(api, "get_reality_touch_cron_manager", None) if api is not None else None
        return getter() if callable(getter) else None

    def _llm_timer_timezone_name(self) -> str:
        return self.environment_perception_timezone

    def _llm_timer_run_at(self, scheduled_ts: float) -> datetime:
        return self._environment_fromtimestamp(scheduled_ts)

    def _environment_fromtimestamp(self, value: float) -> datetime:
        try:
            zone = zoneinfo.ZoneInfo(self.environment_perception_timezone)
        except Exception:
            zone = datetime.now().astimezone().tzinfo
        return datetime.fromtimestamp(float(value), tz=zone)

    async def _delete_official_llm_timer_job(self, job_id: str) -> tuple[bool, str]:
        api = self._private_companion_api()
        deleter = getattr(api, "delete_reality_touch_cron_job", None) if api is not None else None
        if not callable(deleter):
            return False, "未连接主插件 Cron"
        result = await deleter(job_id)
        if isinstance(result, tuple):
            return bool(result[0]), str(result[1] if len(result) > 1 else "")
        return bool(result), ""

    async def _send_chain_components(self, umo: str, components: list[Any]) -> bool:
        api = self._private_companion_api()
        sender = getattr(api, "send_reality_touch_chat", None) if api is not None else None
        if not callable(sender):
            return False
        text = "".join(str(getattr(item, "text", "") or "") for item in components)
        return bool(await sender(umo, text))

    async def _record_reality_touch_delivery(self, user: dict[str, Any], text: str, *, source: str) -> bool:
        user_id = _single_line(user.get("user_id"), 120) if isinstance(user, dict) else ""
        api = self._private_companion_api()
        recorder = getattr(api, "record_reality_touch_output", None) if api is not None else None
        if not user_id or not callable(recorder):
            return False
        try:
            result = await recorder(user_id, text, source=source, delivered_at=_now_ts())
        except Exception as exc:
            logger.warning("[RealityCompanion] 现实触及输出回写失败: %s", _single_line(exc, 160))
            return False
        return bool(isinstance(result, dict) and result.get("recorded"))

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        sender = getattr(event, "send", None)
        if callable(sender):
            await sender(event.plain_result(str(text or "")))

    def integration_status(self) -> dict[str, Any]:
        cleanup = getattr(self, "_mobile_cleanup_sessions", None)
        if callable(cleanup):
            cleanup()
        managed = self._private_companion_api() is not None
        return {
            "available": True,
            "enabled": bool(self.enable_experimental_bluetooth_wakeup),
            "camera_enabled": bool(self.enable_reality_touch_camera),
            "private_companion_linked": managed,
            "managed_by_private_companion": managed,
            "users": len(self.data.get("users", {})) if isinstance(self.data.get("users"), dict) else 0,
            "audio": self._reality_touch_audio_snapshot(),
            "camera": self._reality_touch_camera_page_snapshot(),
            "mobile": {
                "enabled": self._mobile_enabled(),
                "running": self._mobile_server_runner is not None,
                "host": self._mobile_host(),
                "port": self._mobile_server_bound_port or self._mobile_port(),
                "pairing_configured": bool(self._mobile_pairing_token()),
                "session_ttl_hours": self._mobile_session_ttl() // 3600,
                "location_ttl_seconds": self._mobile_location_ttl(),
                "amap_reverse_geocode_enabled": self._mobile_amap_enabled(),
                "amap_api_key_configured": bool(self._mobile_amap_api_key()),
                "amap_cache_ttl_seconds": self._mobile_amap_cache_ttl(),
                "amap_request_timeout_seconds": self._mobile_amap_timeout_seconds(),
                "telemetry_enabled": self._mobile_telemetry_enabled(),
                "telemetry_ttl_seconds": self._mobile_telemetry_ttl(),
                "activity_enabled": self._mobile_activity_enabled(),
                "activity_ttl_seconds": self._mobile_activity_ttl(),
                "active_sessions": len(self._mobile_sessions),
                "screen_upload_enabled": self._mobile_screen_upload_enabled(),
            },
        }

    @filter.command("现实触及", alias={"来到身边"})
    async def reality_touch_command(self, event: AstrMessageEvent):
        if not self._safe_event_is_private(event):
            yield event.plain_result("现实触及只在私聊窗口设置，避免群聊误触发本机设备。")
            return
        user_id = self._private_user_id_for_event(event)
        if not self._reality_touch_camera_user_eligible(user_id):
            yield event.plain_result("只有 AstrBot 管理员、主要用户或本插件明确配置的用户可以设置现实触及。")
            return
        raw = str(getattr(event, "message_str", "") or "")
        value = re.sub(r"^\s*/?(?:现实触及|来到身边)\s*", "", raw, count=1).strip()
        command_action = re.sub(r"[\s，,。.!！;；:：]+", "", value)
        if command_action in {"配对令牌", "查看配对令牌", "输出配对令牌", "生成配对令牌"}:
            yield event.plain_result(await self._mobile_pairing_token_command(rotate=False))
            return
        if command_action in {"重置配对令牌", "重新生成配对令牌", "刷新配对令牌"}:
            yield event.plain_result(await self._mobile_pairing_token_command(rotate=True))
            return
        user = self._user(user_id)
        user["umo"] = _single_line(getattr(event, "unified_msg_origin", ""), 180)
        if command_action in {
            "摄像头单帧", "输出摄像头单帧", "查看摄像头单帧", "摄像头截图", "输出摄像头截图",
        }:
            result = await self._reality_touch_camera_snapshot_for_user(
                user_id,
                "用户通过现实触及指令明确请求输出当前摄像头单帧",
                include_preview=True,
                source="manual_command",
            )
            observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
            detail = _single_line(observation.get("summary"), 300)
            message = _single_line(result.get("message"), 240) or "摄像头单帧读取失败"
            if result.get("captured"):
                message = "摄像头单帧读取完成" + (f"：{detail}" if detail else "。")
            preview = str(result.get("preview_data_url") or "")
            if preview.startswith("data:image/jpeg;base64,"):
                try:
                    image_bytes = base64.b64decode(preview.split(",", 1)[1], validate=True)
                except (ValueError, TypeError):
                    image_bytes = b""
                if image_bytes:
                    yield event.chain_result([Plain(message), Image.fromBytes(image_bytes)])
                    return
            yield event.plain_result(message)
            return
        if command_action in {"语音试听", "试听语音", "音频试听", "试听音频", "声音试听"}:
            if not bool(self.enable_experimental_bluetooth_wakeup):
                yield event.plain_result("现实触及总开关未开启，无法进行语音试听。")
                return
            if not self._reality_touch_audio_consented(user):
                yield event.plain_result("尚未完成现实触及音频知情确认，请先发送“现实触及 确认”。")
                return
            audio = self._reality_touch_audio_snapshot()
            policy = self._reality_touch_policy(user)
            configured_volume = policy.get("playback_volume")
            volume = _safe_int(
                configured_volume if configured_volume is not None else audio.get("playback_volume"),
                35,
                0,
                100,
            )
            played = await self._play_reality_touch_test_audio(volume)
            device = _single_line(audio.get("label"), 160) or "跟随系统默认输出"
            if played:
                yield event.plain_result(f"语音试听已播放。输出设备：{device}；音量：{volume}%。")
            else:
                yield event.plain_result(f"语音试听失败。当前输出设备：{device}；请检查设备连接和音频依赖。")
            return
        if command_action in {"位置检查", "检查位置", "查看位置", "定位检查", "检查定位"}:
            yield event.plain_result(self._reality_touch_location_check_text(user_id))
            return
        response, followup = self._wakeup_alarm_command(user, value)
        self._save_data_sync()
        yield event.plain_result(response)
        if isinstance(followup, dict) and followup.get("camera_snapshot"):
            result = await self._reality_touch_camera_snapshot_for_user(
                user_id,
                followup.get("purpose"),
                source="manual_test",
            )
            observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
            detail = _single_line(observation.get("summary"), 300)
            yield event.plain_result(
                "单帧读取完成：" + detail
                if result.get("status") == "success" and detail
                else _single_line(result.get("message"), 240)
            )
        elif followup:
            self._create_lifecycle_background_task(self._test_wakeup_alarm(user), label="wakeup_test")

    def _reality_touch_location_check_text(self, user_id: str) -> str:
        context = self.mobile_context(user_id)
        location = context.get("location") if isinstance(context.get("location"), dict) else {}
        if not context.get("available"):
            reason = _single_line(location.get("reason"), 80)
            if reason == "mobile_gateway_disabled":
                return "位置检查：手机陪伴终端网关未启用或尚未运行。"
            ttl = _safe_int(
                (context.get("privacy") or {}).get("expires_after_seconds")
                if isinstance(context.get("privacy"), dict) else 0,
                900,
                60,
            )
            return f"位置检查：当前没有有效的手机前台位置；可能尚未上报、已撤销或已超过 {ttl} 秒有效期。"

        parts = ["位置检查：手机陪伴终端位置有效"]
        place = location.get("place") if isinstance(location.get("place"), dict) else {}
        place_name = _single_line(place.get("name"), 40) if place.get("matched") else ""
        label = _single_line(location.get("label"), 40)
        if place_name:
            parts.append(f"标记地点：{place_name}")
        elif label:
            parts.append(f"设备标记：{label}")
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is not None and longitude is not None:
            parts.append(f"约略坐标：{latitude}, {longitude}")
        accuracy = _safe_float(location.get("accuracy_m"), 0.0, 0.0)
        if accuracy > 0:
            parts.append(f"精度约 {round(accuracy)} 米")
        captured_at = _safe_float(location.get("captured_at"), 0.0, 0.0)
        if captured_at > 0:
            try:
                timezone = zoneinfo.ZoneInfo(self.environment_perception_timezone or "Asia/Shanghai")
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                timezone = zoneinfo.ZoneInfo("Asia/Shanghai")
            captured_text = datetime.fromtimestamp(captured_at, timezone).strftime("%Y-%m-%d %H:%M:%S")
            parts.append(f"采集时间：{captured_text}")
        age = _safe_int(location.get("age_seconds"), 0, 0)
        parts.append(f"距采集约 {age} 秒")
        return "；".join(parts) + "。"

    async def _mobile_pairing_token_command(self, *, rotate: bool) -> str:
        token = self._mobile_pairing_token()
        generated = rotate or not token
        if generated:
            token = secrets.token_urlsafe(32)
            self._set_group_config_field("mobile", "pairing_token", token)
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver()
            await self._stop_mobile_server()
            await self._start_mobile_server()

        state = "已重置" if rotate else ("已生成" if generated else "当前")
        gateway = (
            f"{self._mobile_host()}:{self._mobile_server_bound_port or self._mobile_port()}"
            if self._mobile_enabled()
            else "移动端网关尚未启用"
        )
        return (
            f"Android 配对令牌（{state}）：\n{token}\n"
            f"网关：{gateway}\n"
            "该令牌可用于建立新的移动端会话，请勿转发、截图或发送到群聊。"
        )

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=221000)
    async def capture_private_identity_and_confirmation(self, event: AstrMessageEvent):
        text = str(getattr(event, "message_str", "") or "").strip()
        if re.match(r"^\s*/?(?:现实触及|来到身边)(?:\s|$)", text):
            return
        user_id = self._private_user_id_for_event(event)
        user = self._user(user_id)
        if user is None:
            return
        user["umo"] = _single_line(getattr(event, "unified_msg_origin", ""), 180)
        user["last_private_activity_at"] = _now_ts()
        self._schedule_data_save()
        confirmation = self._reality_touch_apply_pending_confirmation(user, text)
        if confirmation:
            yield event.plain_result(confirmation)
            event.stop_event()
            return
        handled = await self._maybe_handle_wakeup_feedback(event, user_id, user, text)
        if handled:
            return

    @filter.llm_tool(name="pc_reality_touch_reminder")
    async def pc_reality_touch_reminder(self, event: AstrMessageEvent, text: str) -> str:
        """执行当前 AstrBot 官方任务绑定的现实触及提醒。

        Args:
            text(string): 根据官方任务备注生成的一到两句最终提醒文本。
        """
        delivered, detail = await self._execute_official_reality_touch_reminder(event, text)
        prefix = "Reality touch reminder delivered: " if delivered else "Reality touch reminder failed: "
        return prefix + (_single_line(detail, 180) or "unknown result")

    @filter.llm_tool(name="pc_reality_touch_action")
    async def pc_reality_touch_action(self, event: AstrMessageEvent, request: str) -> str:
        """将自然语言家居/健康请求交给现实触及能力规划器。"""
        user_id = self._private_user_id_for_event(event)
        user = self._user(user_id, create=False)
        if not isinstance(user, dict) or not self._reality_touch_audio_consented(user):
            return "现实触及尚未完成本机音频授权。"
        result = await self.extension_api.resolve_external_reality_request(user_id, request)
        if result.get("ok") is not True:
            return "现实触及暂时无法完成该请求，请稍后再试。"
        if result.get("handled") is False:
            return "这句话暂时没有明确的家居或健康操作，我先不替你操作设备。"
        if result.get("operation") == "run_scene":
            if result.get("reason") == "explicit_confirmation_required":
                return "这个家居场景需要你明确确认后才能执行。"
            if result.get("ok") is True:
                return f"已执行米家场景：{_single_line(result.get('scene_name'), 80) or '指定场景'}。"
            return "米家场景执行失败，现实触及没有声称已经完成。"
        if result.get("operation") == "control_device":
            if result.get("reason") == "explicit_confirmation_required":
                return "这个设备控制需要你明确确认后才能执行。"
            if result.get("ok") is True:
                return "设备控制已执行。"
            return "设备控制失败，现实触及没有声称已经完成。"
        return "已获取相关现实状态，具体结果将在当前回复中继续说明。"

    @filter.llm_tool(name="pc_reality_touch_camera_snapshot")
    async def pc_reality_touch_camera_snapshot(self, event: AstrMessageEvent, purpose: str) -> str:
        """按明确目的读取当前用户已单独授权的摄像头单帧。

        仅在用户本轮明确请求，或主插件已标记为授权主动回合时调用。不得用于身份识别、持续观察、
        情绪读脸或读取屏幕文字；失败时必须转述真实原因，不得猜测画面内容。

        Args:
            purpose(string): 本次单帧读取的具体目的。
        """
        def failure(status: str, message: str) -> str:
            return json.dumps(
                {
                    "status": status,
                    "message": message,
                    "captured": False,
                    "must_not_claim_observed": True,
                    "same_turn_retry_allowed": False,
                    "final_response_instruction": (
                        "本次摄像头工具没有获得任何可用画面。必须如实说明失败原因；"
                        "不得声称画面黑、镜头被挡、又没看到、看到了人物或物品，也不得猜测用户当前状态；"
                        "不要撒娇逼问、催促用户交代，也不要指责用户欺骗或拿失败结果开玩笑。"
                    ),
                },
                ensure_ascii=False,
            )

        user_id = self._private_user_id_for_event(event)
        if not self._reality_touch_camera_user_eligible(user_id):
            return failure("forbidden", "当前发言者没有主机摄像头使用资格")
        purpose_text = _single_line(purpose, 120)
        if not purpose_text:
            return failure("error", "必须说明本次摄像头单帧读取目的")
        is_private = self._safe_event_is_private(event)
        request_text = str(getattr(event, "message_str", "") or "")
        proactive = bool(getattr(event, "private_companion_proactive_framework", False))
        explicit = self._reality_touch_camera_request_matches(
            request_text,
            allow_implicit_self_observation=is_private,
        )
        session_key = _single_line(getattr(event, "unified_msg_origin", ""), 180)
        followup = None
        if is_private and not explicit and self._reality_touch_camera_followup_request_matches(request_text):
            followup = self._reality_touch_camera_followup_context(
                session_key=session_key,
                user_id=user_id,
                consume=not proactive,
            )
        if not proactive and not explicit and followup is None:
            return failure("forbidden", "本轮没有明确的本人摄像头请求")
        food_requested = bool(
            self._reality_touch_camera_food_request_matches(request_text)
            or (isinstance(followup, dict) and followup.get("food_requested"))
        )
        if isinstance(followup, dict):
            purpose_text = _single_line(followup.get("purpose"), 120) or purpose_text
        if food_requested and not self._reality_touch_camera_food_request_matches(purpose_text):
            purpose_text = _single_line(f"{purpose_text}；判断画面中正在吃或喝什么", 120)
        source = (
            "proactive_curiosity"
            if proactive
            else "assistant_tool_private" if is_private else "assistant_tool_group"
        )
        result = await self._reality_touch_camera_snapshot_for_user(
            user_id,
            purpose_text,
            source=source,
        )
        result_status = str(result.get("status") or "").lower() if isinstance(result, dict) else ""
        if is_private and explicit and result_status not in {"disabled", "forbidden"}:
            self._remember_reality_touch_camera_request(
                session_key=session_key,
                user_id=user_id,
                purpose=purpose_text,
                food_requested=food_requested,
            )
        if isinstance(result, dict) and result_status != "success":
            result = dict(result)
            captured = bool(result.get("captured"))
            result["captured"] = captured
            result["must_not_claim_observed"] = True
            result["same_turn_retry_allowed"] = False
            if not result.get("final_response_instruction"):
                result["final_response_instruction"] = (
                    "摄像头已经取得一帧，但视觉模型没有可靠回答本次问题。只能说明无法判断；"
                    "不得把不确定改写成画面黑、镜头被挡，也不得补充 observation 中没有的细节；"
                    "不要撒娇逼问、催促用户交代，也不要指责用户欺骗或拿失败结果开玩笑。"
                    if captured
                    else (
                        "本次摄像头工具没有获得任何可用画面。必须如实转述工具返回的失败原因；"
                        "不得声称画面黑、镜头被挡、又没看到、看到了人物或物品，也不得猜测用户当前状态；"
                        "不要撒娇逼问、催促用户交代，也不要指责用户欺骗或拿失败结果开玩笑。"
                    )
                )
        return json.dumps(result, ensure_ascii=False)

    @filter.on_llm_request(priority=-20900)
    async def append_camera_request_guidance(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if not self.enable_experimental_bluetooth_wakeup or not self.enable_reality_touch_camera:
            return
        user_id = self._private_user_id_for_event(event)
        user_getter = getattr(self, "_user", None)
        if callable(user_getter):
            user = user_getter(user_id, create=False)
        else:
            users = self.data.get("users", {}) if isinstance(getattr(self, "data", None), dict) else {}
            user = users.get(str(user_id)) if isinstance(users, dict) else None
        if not user or not self._reality_touch_camera_user_eligible(user_id):
            return
        if not self._reality_touch_camera_consented(user) or not self._reality_touch_camera_policy(user).get("enabled"):
            return
        is_private = self._safe_event_is_private(event)
        request_text = str(getattr(event, "message_str", "") or "")
        explicit = self._reality_touch_camera_request_matches(
            request_text,
            allow_implicit_self_observation=is_private,
        )
        followup = None
        if is_private and not explicit and self._reality_touch_camera_followup_request_matches(request_text):
            followup = self._reality_touch_camera_followup_context(
                session_key=getattr(event, "unified_msg_origin", ""),
                user_id=user_id,
                consume=False,
            )
        if not explicit and followup is None:
            return
        food_requested = bool(
            self._reality_touch_camera_food_request_matches(request_text)
            or (isinstance(followup, dict) and followup.get("food_requested"))
        )
        marker = "<!-- private_companion_camera_request_v1 -->"
        current = str(getattr(req, "system_prompt", "") or "")
        if marker in current:
            return
        guidance = (
            "【摄像头请求】当前发言者是已完成独立知情确认的 AstrBot 管理员或主要用户，"
            + (
                "本轮是同一私聊中上一条明确单帧视觉请求的短时重试。"
                if followup is not None
                else "本轮消息也构成明确的单帧视觉请求。"
            )
            + (
                "用户在问画面中正在吃或喝什么；purpose 应准确写明该问题，工具会返回完整视觉摘要、直接答案和可见证据。"
                if food_requested
                else "purpose 应准确复述本轮问题，工具会先完整识图，再把与目的有关的视觉语义交回本模型。"
            )
            + "应先调用 pc_reality_touch_camera_snapshot，再优先依据 observation.purpose_answer、scene_description 和 visible_evidence 自然回答。"
            + "unknown/uncertain 只表示无法判断，不能解释成画面黑或镜头被挡。"
            + "最终回复只能陈述 observation 明确出现的内容；不得从当前时间、侧卧/抬手等姿势动作或房间物品推断"
            + "睡不着、锻炼、情绪、意图或其他未出现事实。工具失败或无法判断时，只简短说明本次结果；"
            + "当工具返回 observation_uncertain 或 answer_available=false 时，最终只回复一句‘本次未能可靠判断’，"
            + "不解释原因、不换话题猜食物或其他内容，也不追问用户。"
            + "不要在工具失败时猜测画面或说成‘又没看到’，也不要撒娇逼问、催促用户交代或指责用户欺骗。"
        )
        req.system_prompt = f"{current}\n\n{marker}\n{guidance}".strip()
        recorder = getattr(self, "_record_request_prompt_fragment", None)
        if callable(recorder):
            await recorder(
                event,
                title="摄像头请求",
                key="tools.camera_request",
                text=guidance,
                source="tool_guidance",
                mode="private" if is_private else "group",
                metadata={"food_requested": food_requested},
            )

    @filter.on_agent_begin()
    async def acknowledge_reality_touch_job(self, event: AstrMessageEvent, run_context: Any, *args: Any, **kwargs: Any) -> None:
        await self._acknowledge_official_reality_touch_trigger(event)

    @filter.on_llm_tool_respond()
    async def record_reality_touch_tool_result(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict[str, Any] | None,
        tool_result: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        await self._record_official_reality_touch_tool_result(event, tool, tool_result)
        if _reality_touch_tool_name(tool) != "pc_reality_touch_camera_snapshot":
            return
        payload = _reality_touch_camera_tool_payload(tool_result)
        if not isinstance(payload, dict):
            return
        status = _single_line(payload.get("status"), 40).lower()
        answer_available = payload.get("answer_available")
        uncertain = status != "success" or answer_available is False
        setattr(event, "_reality_touch_camera_answer_uncertain", uncertain)
        if uncertain:
            logger.info(
                "[RealityCompanion] 摄像头工具未给出可靠答案，已标记发送前事实校验: status=%s answer_available=%s",
                status or "unknown",
                answer_available,
            )

    @filter.on_decorating_result(priority=400)
    async def enforce_uncertain_camera_reply_contract(
        self,
        event: AstrMessageEvent,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Prevent a reply model from inventing a camera explanation after an uncertain result."""
        if not bool(getattr(event, "_reality_touch_camera_answer_uncertain", False)):
            return
        try:
            result = event.get_result()
        except Exception:
            return
        chain = list(getattr(result, "chain", []) or []) if result is not None else []
        text = "".join(
            str(getattr(component, "text", "") or "")
            for component in chain
            if isinstance(component, Plain)
        ).strip()
        if not text or not _reality_touch_camera_reply_needs_uncertain_fallback(text):
            return
        replacement = "这次单帧没能可靠判断你在给我看什么，我先不乱猜。"
        try:
            result.chain = [Plain(replacement)]
        except Exception:
            event.set_result(event.plain_result(replacement))
        logger.warning(
            "[RealityCompanion] 已替换不确定摄像头结果后的无依据回复: session=%s",
            _single_line(getattr(event, "unified_msg_origin", ""), 120) or "unknown",
        )

    @filter.on_agent_done()
    async def complete_reality_touch_job(
        self,
        event: AstrMessageEvent,
        run_context: Any,
        response: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        await self._complete_official_reality_touch_reminder(event)

    def _register_page_api(self) -> None:
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            logger.warning("[RealityCompanion] 当前 AstrBot 不支持插件拓展页 API")
            return
        register_api(f"{PAGE_API_PREFIX}/status", self.page_status, ["GET"], "Reality Companion status")
        register_api(f"{PAGE_API_PREFIX}/action", self.page_action, ["POST"], "Reality Companion action")

    async def page_status(self) -> dict[str, Any]:
        return {"ok": True, "data": self._reality_touch_page_snapshot(), "integration": self.integration_status()}

    async def page_action(self) -> dict[str, Any]:
        if self._private_companion_api() is not None:
            return {
                "ok": False,
                "status": "managed_by_private_companion",
                "message": MANAGED_PAGE_MESSAGE,
            }
        if request is None:
            return {"ok": False, "message": "AstrBot 页面请求上下文不可用"}
        payload = await request.json(default={}) or {}
        return await self._perform_page_action(payload)

    async def _perform_page_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = _single_line(payload.get("action"), 40).lower()
        try:
            if action in {"scan_camera", "scan_cameras"}:
                result = self._reality_touch_scan_camera_devices()
            elif action in {"save_global_config", "save_config"}:
                result = await self._reality_touch_save_global_config(payload)
            elif action in {"select_audio", "select_output"}:
                result = self._reality_touch_select_audio_device(
                    _single_line(payload.get("device_id"), 96),
                    payload.get("playback_volume"),
                )
                self._save_data_sync()
            elif action == "save_camera_config":
                self.enable_reality_touch_camera = bool(payload.get("camera_enabled"))
                self.reality_touch_camera_index = _safe_int(payload.get("camera_index"), 0, 0, 100000)
                self.reality_touch_camera_min_interval_seconds = _safe_int(payload.get("min_interval_seconds"), 60, 10, 3600)
                self.reality_touch_camera_capture_timeout_seconds = _safe_int(payload.get("capture_timeout_seconds"), 5, 2, 20)
                self.reality_touch_camera_analysis_timeout_seconds = _safe_int(payload.get("analysis_timeout_seconds"), 25, 5, 90)
                self.enable_reality_touch_camera_proactive_curiosity = bool(payload.get("proactive_curiosity_enabled"))
                self.reality_touch_camera_proactive_min_tier = _safe_int(payload.get("proactive_min_tier"), 4, 1, 5)
                self.reality_touch_camera_proactive_max_daily = _safe_int(payload.get("proactive_max_daily"), 1, 0, 10)
                self.reality_touch_camera_proactive_cooldown_minutes = _safe_int(payload.get("proactive_cooldown_minutes"), 240, 10, 1440)
                result = {"saved": self._save_runtime_camera_config()}
            elif action in {"test_audio", "test"} and (
                action == "test_audio"
                or _single_line(payload.get("test_kind"), 24).lower() == "device"
            ):
                result = {"played": await self._play_reality_touch_test_audio(payload.get("playback_volume"))}
            elif action == "test_camera":
                user_id = _single_line(payload.get("user_id"), 120)
                result = await self._reality_touch_camera_snapshot_for_user(
                    user_id,
                    _single_line(payload.get("purpose"), 120) or "管理员从现实触及页面测试摄像头单帧",
                    include_preview=True,
                    source="page_test",
                )
            elif action in {"update_audio_policy", "save_policy"}:
                user = self._user(payload.get("user_id"), create=False)
                if not user:
                    raise ValueError("没有找到该用户")
                result = self._reality_touch_update_policy(user, payload)
                self._save_data_sync()
            elif action in {"update_camera_policy", "save_camera_policy"}:
                user = self._user(payload.get("user_id"), create=False)
                if not user:
                    raise ValueError("没有找到该用户")
                result = self._reality_touch_update_camera_policy(
                    user,
                    payload,
                    user_id=_single_line(payload.get("user_id"), 120),
                )
                self._save_data_sync()
            elif action == "save":
                user = self._user(payload.get("user_id"), create=False)
                if not user:
                    raise ValueError("没有找到该用户")
                result = self._reality_touch_update_alarm(user, payload)
                self._save_data_sync()
            elif action in {"disable", "stop_session"}:
                user = self._user(payload.get("user_id"), create=False)
                if not user:
                    raise ValueError("没有找到该用户")
                if action == "disable":
                    self._wakeup_alarm_for_user(user)["enabled"] = False
                self._stop_wakeup_contact_session(user)
                self._save_data_sync()
                result = {"stopped": True}
            elif action == "cancel_reminder":
                result = {
                    "cancelled": await self._cancel_reality_touch_official_reminder(
                        _single_line(payload.get("user_id"), 120),
                        reminder_id=_single_line(payload.get("reminder_id"), 40),
                    )
                }
            elif action == "test":
                user = self._user(payload.get("user_id"), create=False)
                if not user:
                    raise ValueError("没有找到该用户")
                alarm = copy.deepcopy(self._wakeup_alarm_for_user(user))
                if "message" in payload:
                    alarm["message"] = _single_line(payload.get("message"), 240)
                result = {
                    "played": await self._play_wakeup_alarm(
                        copy.deepcopy(user),
                        alarm,
                        test=True,
                        volume=payload.get("playback_volume"),
                    )
                }
            else:
                return {"ok": False, "message": "不支持的操作"}
        except Exception as exc:
            return {"ok": False, "message": _single_line(exc, 240)}
        snapshot = self._reality_touch_page_snapshot()
        if action == "test_camera" and isinstance(result, dict):
            preview = str(result.get("preview_data_url") or "")
            if preview.startswith("data:image/jpeg;base64,"):
                snapshot["camera_preview"] = {
                    "user_id": _single_line(payload.get("user_id"), 120),
                    "captured_at": int(time.time()),
                    "data_url": preview,
                    "ephemeral": True,
                }
        return {"ok": True, "result": result, "data": snapshot}

    def _reality_touch_configuration_snapshot(self) -> dict[str, Any]:
        """Return editable settings without exposing the mobile pairing secret."""
        return {
            "enabled": bool(self.enable_experimental_bluetooth_wakeup),
            "vision_provider_id": _single_line(self.plugin_vision_provider_id, 160),
            "timezone": _single_line(self.environment_perception_timezone, 80) or "Asia/Shanghai",
            "authorized_user_ids": sorted(self.authorized_user_ids),
            "audio_default_playback_volume": _safe_int(self.tts_local_playback_volume, 35, 0, 100),
            "mobile": {
                "gateway_version": PLUGIN_VERSION,
                "api_version": MOBILE_API_VERSION,
                "enabled": self._mobile_enabled(),
                "host": self._mobile_host(),
                "port": self._mobile_port(),
                "allowed_user_id": self._mobile_allowed_user_id(),
                "session_ttl_hours": self._cfg_int("mobile.session_ttl_hours", 168, 1, 720),
                "location_ttl_seconds": self._mobile_location_ttl(),
                "amap_reverse_geocode_enabled": self._mobile_amap_enabled(),
                "amap_api_key_configured": bool(self._mobile_amap_api_key()),
                "amap_cache_ttl_seconds": self._mobile_amap_cache_ttl(),
                "amap_request_timeout_seconds": self._mobile_amap_timeout_seconds(),
                "telemetry_enabled": self._mobile_telemetry_enabled(),
                "telemetry_ttl_seconds": self._mobile_telemetry_ttl(),
                "activity_enabled": self._mobile_activity_enabled(),
                "activity_ttl_seconds": self._mobile_activity_ttl(),
                "proxy_rooms": self._cfg_bool("mobile.proxy_rooms", True),
                "screen_upload_enabled": self._mobile_screen_upload_enabled(),
                "pairing_token_configured": bool(self._mobile_pairing_token()),
                "running": self._mobile_server_runner is not None,
                "bound_port": _safe_int(self._mobile_server_bound_port, 0, 0, 65535),
                "active_sessions": len(self._mobile_sessions),
            },
        }

    async def _reality_touch_save_global_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = self._coerce_config_bool(payload.get("enabled"), False)
        vision_provider_id = _single_line(payload.get("vision_provider_id"), 160)
        timezone = _single_line(payload.get("timezone"), 80) or "Asia/Shanghai"
        raw_ids = payload.get("authorized_user_ids", [])
        if isinstance(raw_ids, str):
            raw_ids = re.split(r"[,\n，；;]+", raw_ids)
        authorized_user_ids = sorted({
            _single_line(item, 120)
            for item in (raw_ids if isinstance(raw_ids, (list, tuple, set)) else [])
            if _single_line(item, 120)
        })

        self.config["enabled"] = enabled
        self.config["vision_provider_id"] = vision_provider_id
        self.config["timezone"] = timezone
        self.config["authorized_user_ids"] = authorized_user_ids
        self._set_group_config_field(
            "audio",
            "default_playback_volume",
            _safe_int(payload.get("audio_default_playback_volume"), 35, 0, 100),
        )

        mobile = payload.get("mobile") if isinstance(payload.get("mobile"), dict) else {}
        mobile_values = {
            "enabled": self._coerce_config_bool(mobile.get("enabled"), False),
            "host": _single_line(mobile.get("host"), 120) or "0.0.0.0",
            "port": _safe_int(mobile.get("port"), 6322, 1, 65535),
            "allowed_user_id": _single_line(mobile.get("allowed_user_id"), 120),
            "session_ttl_hours": _safe_int(mobile.get("session_ttl_hours"), 168, 1, 720),
            "location_ttl_seconds": _safe_int(mobile.get("location_ttl_seconds"), 900, 60, 86400),
            "amap_reverse_geocode_enabled": self._coerce_config_bool(mobile.get("amap_reverse_geocode_enabled"), False),
            "amap_cache_ttl_seconds": _safe_int(mobile.get("amap_cache_ttl_seconds"), 1800, 60, 604800),
            "amap_request_timeout_seconds": _safe_int(mobile.get("amap_request_timeout_seconds"), 5, 1, 20),
            "telemetry_enabled": self._coerce_config_bool(mobile.get("telemetry_enabled"), False),
            "telemetry_ttl_seconds": _safe_int(mobile.get("telemetry_ttl_seconds"), 3600, 60, 604800),
            "activity_enabled": self._coerce_config_bool(mobile.get("activity_enabled"), False),
            "activity_ttl_seconds": _safe_int(mobile.get("activity_ttl_seconds"), 900, 60, 86400),
            "proxy_rooms": self._coerce_config_bool(mobile.get("proxy_rooms"), True),
            "screen_upload_enabled": self._coerce_config_bool(mobile.get("screen_upload_enabled"), True),
        }
        for key, value in mobile_values.items():
            self._set_group_config_field("mobile", key, value)
        pairing_token = _single_line(mobile.get("pairing_token"), 240)
        if pairing_token:
            self._set_group_config_field("mobile", "pairing_token", pairing_token)
        amap_api_key = _single_line(mobile.get("amap_api_key"), 240)
        if amap_api_key:
            self._set_group_config_field("mobile", "amap_api_key", amap_api_key)

        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            saver()
        self._sync_runtime_config()
        self.plugin_vision_provider_id = vision_provider_id
        self.environment_perception_timezone = timezone
        self.authorized_user_ids = set(authorized_user_ids)
        await self._stop_mobile_server()
        await self._start_mobile_server()
        return {"saved": True, "mobile_running": self._mobile_server_runner is not None}

    def _save_runtime_camera_config(self) -> bool:
        camera = self.config.get("camera") if isinstance(self.config, dict) else None
        if not isinstance(camera, dict):
            camera = {}
            self.config["camera"] = camera
        camera.update(
            {
                "enabled": self.enable_reality_touch_camera,
                "index": self.reality_touch_camera_index,
                "min_interval_seconds": self.reality_touch_camera_min_interval_seconds,
                "capture_timeout_seconds": self.reality_touch_camera_capture_timeout_seconds,
                "analysis_timeout_seconds": self.reality_touch_camera_analysis_timeout_seconds,
                "proactive_curiosity_enabled": self.enable_reality_touch_camera_proactive_curiosity,
                "proactive_min_tier": self.reality_touch_camera_proactive_min_tier,
                "proactive_max_daily": self.reality_touch_camera_proactive_max_daily,
                "proactive_cooldown_minutes": self.reality_touch_camera_proactive_cooldown_minutes,
            }
        )
        saver = getattr(self.config, "save_config", None)
        try:
            if callable(saver):
                saver()
            return True
        except Exception as exc:
            logger.warning("[RealityCompanion] 保存摄像头配置失败: %s", _single_line(exc, 160))
            return False
