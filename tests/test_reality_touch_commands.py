# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import sys
import types
import unittest
from unittest.mock import AsyncMock

from astrbot.api.message_components import Image, Plain
from astrbot_plugin_reality_companion.main import RealityCompanionPlugin


class CommandEvent:
    def __init__(self, text: str, *, private: bool = True) -> None:
        self.message_str = text
        self.private = private
        self.unified_msg_origin = "default:FriendMessage:u" if private else "default:GroupMessage:10001"

    def plain_result(self, text: str):
        return types.SimpleNamespace(chain=[Plain(text)])

    def chain_result(self, chain):
        return types.SimpleNamespace(chain=list(chain))


def command_plugin() -> RealityCompanionPlugin:
    plugin = RealityCompanionPlugin.__new__(RealityCompanionPlugin)
    plugin.data = {"users": {"u": {"user_id": "u"}}}
    plugin.enable_experimental_bluetooth_wakeup = True
    plugin.environment_perception_timezone = "Asia/Shanghai"
    plugin._safe_event_is_private = lambda event: bool(event.private)
    plugin._private_user_id_for_event = lambda _event: "u"
    plugin._reality_touch_camera_user_eligible = lambda _user_id: True
    return plugin


async def collect_command(plugin: RealityCompanionPlugin, text: str, *, private: bool = True):
    event = CommandEvent(text, private=private)
    return [item async for item in plugin.reality_touch_command(event)]


class RealityTouchDirectCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_private_companion_discovery_does_not_trigger_lazy_module_import(self) -> None:
        class LazyModule(types.ModuleType):
            def __getattr__(self, name: str):
                raise ModuleNotFoundError("No module named 'torchvision'", name="torchvision")

        module_name = "transformers.lazy_astrbot_plugin_private_companion.main"
        module = LazyModule(module_name)
        previous = sys.modules.get(module_name)
        sys.modules[module_name] = module
        try:
            plugin = RealityCompanionPlugin.__new__(RealityCompanionPlugin)
            plugin.context = types.SimpleNamespace(get_registered_star=lambda _name: None)
            self.assertIsNone(plugin._private_companion_api())
        finally:
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous

    async def test_camera_frame_returns_ephemeral_image_and_summary(self) -> None:
        plugin = command_plugin()
        preview = base64.b64encode(b"jpeg-frame").decode("ascii")
        plugin._reality_touch_camera_snapshot_for_user = AsyncMock(return_value={
            "status": "success",
            "captured": True,
            "message": "已完成一次单帧完整视觉理解",
            "preview_data_url": f"data:image/jpeg;base64,{preview}",
            "observation": {"summary": "画面读取正常"},
        })

        results = await collect_command(plugin, "/现实触及 输出摄像头单帧")

        self.assertEqual(1, len(results))
        self.assertIsInstance(results[0].chain[0], Plain)
        self.assertIn("画面读取正常", results[0].chain[0].text)
        self.assertIsInstance(results[0].chain[1], Image)
        self.assertTrue(results[0].chain[1].file.startswith("base64://"))
        plugin._reality_touch_camera_snapshot_for_user.assert_awaited_once_with(
            "u",
            "用户通过现实触及指令明确请求输出当前摄像头单帧",
            include_preview=True,
            source="manual_command",
        )

    async def test_camera_failure_returns_existing_policy_message_without_image(self) -> None:
        plugin = command_plugin()
        plugin._reality_touch_camera_snapshot_for_user = AsyncMock(return_value={
            "status": "forbidden",
            "message": "该用户尚未完成摄像头独立知情确认",
        })

        results = await collect_command(plugin, "现实触及 摄像头单帧")

        self.assertEqual(1, len(results[0].chain))
        self.assertIn("独立知情确认", results[0].chain[0].text)

    async def test_audio_preview_requires_consent(self) -> None:
        plugin = command_plugin()
        plugin._reality_touch_audio_consented = lambda _user: False
        plugin._play_reality_touch_test_audio = AsyncMock(return_value=True)

        results = await collect_command(plugin, "现实触及 语音试听")

        self.assertIn("音频知情确认", results[0].chain[0].text)
        plugin._play_reality_touch_test_audio.assert_not_awaited()

    async def test_audio_preview_reports_selected_route_and_volume(self) -> None:
        plugin = command_plugin()
        plugin._reality_touch_audio_consented = lambda _user: True
        plugin._reality_touch_audio_snapshot = lambda: {
            "label": "USB 扬声器",
            "playback_volume": 35,
        }
        plugin.data["users"]["u"]["reality_touch_policy"] = {"playback_volume": 42}
        plugin._play_reality_touch_test_audio = AsyncMock(return_value=True)

        results = await collect_command(plugin, "来到身边 试听语音")

        reply = results[0].chain[0].text
        self.assertIn("USB 扬声器", reply)
        self.assertIn("42%", reply)
        plugin._play_reality_touch_test_audio.assert_awaited_once_with(42)

    async def test_location_check_reports_place_age_and_collection_time(self) -> None:
        plugin = command_plugin()
        plugin.mobile_context = lambda _user_id: {
            "available": True,
            "location": {
                "available": True,
                "latitude": 31.231,
                "longitude": 121.474,
                "accuracy_m": 18.6,
                "captured_at": 1_775_772_645,
                "age_seconds": 23,
                "place": {"matched": True, "name": "家"},
            },
            "privacy": {"expires_after_seconds": 900},
        }

        results = await collect_command(plugin, "现实触及 位置检查")

        reply = results[0].chain[0].text
        self.assertIn("标记地点：家", reply)
        self.assertIn("31.231, 121.474", reply)
        self.assertIn("精度约 19 米", reply)
        self.assertIn("距采集约 23 秒", reply)

    async def test_location_check_explains_missing_or_expired_location(self) -> None:
        plugin = command_plugin()
        plugin.mobile_context = lambda _user_id: {
            "available": False,
            "location": {"available": False, "reason": "no_recent_location"},
            "privacy": {"expires_after_seconds": 600},
        }

        results = await collect_command(plugin, "现实触及 检查位置")

        self.assertIn("尚未上报、已撤销或已超过 600 秒有效期", results[0].chain[0].text)

    async def test_direct_checks_remain_private_only(self) -> None:
        plugin = command_plugin()

        results = await collect_command(plugin, "现实触及 位置检查", private=False)

        self.assertIn("只在私聊窗口", results[0].chain[0].text)


if __name__ == "__main__":
    unittest.main()
