# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_reality_companion.main import RealityCompanionPlugin
from astrbot_plugin_reality_companion.wakeup_alarm import WakeupAlarmMixin


CAMERA_TOOL_IMPL = RealityCompanionPlugin.pc_reality_touch_camera_snapshot
CAMERA_GUIDANCE_IMPL = RealityCompanionPlugin.append_camera_request_guidance


class CameraEvent:
    def __init__(self, text: str, *, private: bool) -> None:
        self.message_str = text
        self.private = private
        self.unified_msg_origin = (
            "default:FriendMessage:u" if private else "default:GroupMessage:10001"
        )

    def is_private_chat(self) -> bool:
        return self.private

    @staticmethod
    def get_sender_id() -> str:
        return "u"


class CameraHarness(WakeupAlarmMixin):
    def __init__(self) -> None:
        self.data = {"users": {"u": {}}}
        self.owner_user_ids = {"u"}
        self.admin_user_ids: set[str] = set()
        self.enable_experimental_bluetooth_wakeup = True
        self.enable_reality_touch_camera = True
        self.reality_touch_camera_index = 0
        self.reality_touch_camera_min_interval_seconds = 60
        self.reality_touch_camera_capture_timeout_seconds = 5
        self.enable_reality_touch_camera_proactive_curiosity = False
        self.reality_touch_camera_proactive_min_tier = 4
        self.reality_touch_camera_proactive_max_daily = 1
        self.reality_touch_camera_proactive_cooldown_minutes = 240
        self.proactive_tier = 4
        self.plugin_vision_provider_id = ""
        self.context = types.SimpleNamespace(get_provider_by_id=lambda _provider_id: None)
        self.save_count = 0

    def _save_data_sync(self) -> None:
        self.save_count += 1

    def _permission_identity_id(self, user_id) -> str:
        value = str(user_id or "").strip()
        return value if value in self.data["users"] else ""

    def _is_configured_admin_user_id(self, user_id) -> bool:
        return self._permission_identity_id(user_id) in self.admin_user_ids

    def _relationship_owner_user_ids(self) -> set[str]:
        return set(self.owner_user_ids)

    def _proactive_quota_policy(self, _user) -> dict:
        return {"tier": self.proactive_tier, "label": f"L{self.proactive_tier}"}

    @staticmethod
    def _environment_today_key() -> str:
        return "2026-08-11"

    @staticmethod
    def _safe_event_is_private(event) -> bool:
        return bool(event.is_private_chat())

    @staticmethod
    def _private_user_id_for_event(_event) -> str:
        return "u"


class RealityTouchCameraConsentTests(unittest.TestCase):
    def test_camera_eligibility_does_not_inherit_target_or_proactive_permission(self) -> None:
        harness = CameraHarness()
        harness.owner_user_ids.clear()
        harness.target_user_ids = ["u"]
        harness.data["users"]["u"]["proactive_private_enabled"] = True
        self.assertFalse(harness._reality_touch_camera_user_eligible("u"))

    def test_camera_eligibility_accepts_admin_or_explicit_owner(self) -> None:
        harness = CameraHarness()
        self.assertTrue(harness._reality_touch_camera_user_eligible("u"))
        harness.owner_user_ids.clear()
        harness.admin_user_ids.add("u")
        self.assertTrue(harness._reality_touch_camera_user_eligible("u"))

    def test_ineligible_user_cannot_enable_camera_policy(self) -> None:
        harness = CameraHarness()
        harness.owner_user_ids.clear()
        user = harness.data["users"]["u"]
        user["reality_touch_camera_consent"] = {
            "confirmed": True,
            "version": 1,
            "granted_capabilities": ["camera_single_frame"],
        }
        with self.assertRaisesRegex(ValueError, "只允许 AstrBot 管理员或主要用户"):
            harness._reality_touch_update_camera_policy(user, {"camera_enabled": True}, user_id="u")

    def test_audio_consent_does_not_grant_camera(self) -> None:
        harness = CameraHarness()
        user = {"reality_touch_consent": {"confirmed": True, "version": 1, "granted_capabilities": ["local_audio"]}}
        self.assertFalse(harness._reality_touch_camera_consented(user))

    def test_camera_requires_complete_manual_confirmation(self) -> None:
        harness = CameraHarness()
        user: dict = {}
        reply, requested = harness._reality_touch_camera_command(user, "摄像头确认 我同意使用摄像头")
        self.assertFalse(requested)
        self.assertIn("确认口令不正确", reply)
        self.assertNotIn("reality_touch_camera_consent", user)
        reply, requested = harness._reality_touch_camera_command(
            user, "摄像头确认 " + harness._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT
        )
        self.assertFalse(requested)
        self.assertIn("独立授权已记录", reply)
        self.assertTrue(harness._reality_touch_camera_consented(user))
        self.assertEqual(["camera_single_frame"], user["reality_touch_camera_consent"]["granted_capabilities"])

    def test_camera_risk_prompt_then_bare_phrase_grants_pending_capability(self) -> None:
        harness = CameraHarness()
        user: dict = {}
        prompt, requested = harness._reality_touch_camera_command(user, "摄像头确认")
        self.assertFalse(requested)
        self.assertIn("我理解风险并确认授权", prompt)
        self.assertEqual("camera_single_frame", user["reality_touch_pending_consent"]["capability"])
        reply = harness._reality_touch_apply_pending_confirmation(user, "我理解风险并确认授权")
        self.assertIn("摄像头独立授权已记录", reply)
        self.assertTrue(harness._reality_touch_camera_consented(user))
        self.assertNotIn("reality_touch_pending_consent", user)

    def test_revoking_camera_preserves_audio_consent(self) -> None:
        harness = CameraHarness()
        user = {"reality_touch_consent": {"confirmed": True, "version": 1, "granted_capabilities": ["local_audio"]}}
        harness._reality_touch_camera_command(user, "摄像头确认 " + harness._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT)
        reply, _ = harness._reality_touch_camera_command(user, "撤销摄像头授权")
        self.assertIn("本机音频授权不受影响", reply)
        self.assertIn("reality_touch_consent", user)
        self.assertNotIn("reality_touch_camera_consent", user)

    def test_sanitizer_drops_identity_and_free_text_fields(self) -> None:
        harness = CameraHarness()
        observation = harness._sanitize_reality_touch_camera_observation(
            {
                "presence": "present",
                "activity": "reading_screen_text",
                "interruptibility": "high",
                "brightness": "normal",
                "confidence": 4,
                "identity": "某位具体用户",
                "face": "可识别人脸",
                "reason": "房间与屏幕里的敏感细节",
            },
            local_brightness="normal",
            width=640,
            height=480,
            analyzed=True,
        )
        self.assertEqual("unknown", observation["activity"])
        self.assertEqual(1.0, observation["confidence"])
        self.assertNotIn("identity", observation)
        self.assertNotIn("face", observation)
        self.assertNotIn("reason", observation)

    def test_camera_request_matching_requires_explicit_group_language(self) -> None:
        harness = CameraHarness()

        self.assertTrue(
            harness._reality_touch_camera_request_matches(
                "可以通过摄像头看看我在不在家里",
                allow_implicit_self_observation=False,
            )
        )
        self.assertFalse(
            harness._reality_touch_camera_request_matches(
                "看看我在吃什么",
                allow_implicit_self_observation=False,
            )
        )
        self.assertTrue(
            harness._reality_touch_camera_request_matches(
                "好哦，看看我在吃什么",
                allow_implicit_self_observation=True,
            )
        )
        self.assertTrue(harness._reality_touch_camera_followup_request_matches("再试试"))
        self.assertTrue(harness._reality_touch_camera_followup_request_matches("这次重新看一下"))
        self.assertFalse(harness._reality_touch_camera_followup_request_matches("再试试别的功能"))

    def test_cv2_loader_prefers_bundled_runtime_site_packages(self) -> None:
        harness = CameraHarness()
        runtime_site = str(Path(sys.executable).resolve().parent / "Lib" / "site-packages")
        observed_path = ""
        incomplete_cv2 = types.SimpleNamespace(__version__="broken")

        def import_module(name: str):
            nonlocal observed_path
            self.assertEqual("cv2", name)
            self.assertFalse(hasattr(sys, "OpenCV_LOADER"))
            self.assertNotIn("cv2", sys.modules)
            observed_path = sys.path[0]
            return types.SimpleNamespace(__version__="test")

        original_path = list(sys.path)
        sys.OpenCV_LOADER = True
        try:
            with patch.dict(sys.modules, {"cv2": incomplete_cv2}, clear=False):
                with patch("astrbot_plugin_reality_companion.reality_touch_camera.importlib.import_module", import_module):
                    module = harness._reality_touch_import_cv2()
        finally:
            if hasattr(sys, "OpenCV_LOADER"):
                del sys.OpenCV_LOADER

        self.assertEqual("test", module.__version__)
        self.assertEqual(os.path.normcase(runtime_site), os.path.normcase(observed_path))
        self.assertEqual(original_path, sys.path)

    def test_food_observation_is_only_exposed_for_matching_purpose(self) -> None:
        harness = CameraHarness()
        raw = {
            "presence": "present",
            "activity": "eating",
            "food_visibility": "visible",
            "visible_food": "一碗面和一杯饮料",
            "confidence": 0.8,
        }

        food = harness._sanitize_reality_touch_camera_observation(
            raw,
            local_brightness="normal",
            width=640,
            height=480,
            analyzed=True,
            purpose="看看我在吃什么",
        )
        generic = harness._sanitize_reality_touch_camera_observation(
            raw,
            local_brightness="normal",
            width=640,
            height=480,
            analyzed=True,
            purpose="看看我在不在家",
        )

        self.assertEqual("一碗面和一杯饮料", food["visible_food"])
        self.assertIn("可见食物=一碗面和一杯饮料", food["summary"])
        self.assertNotIn("visible_food", generic)
        self.assertNotIn("可见食物", generic["summary"])

    def test_complete_visual_observation_is_preserved_for_reply_model(self) -> None:
        harness = CameraHarness()
        observation = harness._sanitize_reality_touch_camera_observation(
            {
                "scene_description": "一张桌子位于画面中央，桌上放着餐盒和一杯饮料，人物坐在桌边。",
                "purpose_answer": "画面中能看到一份盒饭和一杯饮料。",
                "visible_evidence": ["桌面中央有打开的餐盒", "餐盒旁有透明饮料杯"],
                "uncertainty": "无法确认饮料的具体种类",
                "answer_status": "answered",
                "presence": "present",
                "activity": "eating",
                "activity_detail": "坐在桌边用餐",
                "food_visibility": "visible",
                "visible_food": "盒饭和一杯饮料",
                "brightness": "normal",
                "confidence": 0.86,
            },
            local_brightness="normal",
            width=640,
            height=480,
            analyzed=True,
            purpose="看看我在吃什么",
        )

        self.assertTrue(observation["answer_available"])
        self.assertIn("桌子位于画面中央", observation["scene_description"])
        self.assertEqual("画面中能看到一份盒饭和一杯饮料。", observation["purpose_answer"])
        self.assertEqual(2, len(observation["visible_evidence"]))
        self.assertIn("完整视觉摘要", observation["summary"])


class RealityTouchCameraToolScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_private_food_request_injects_tool_guidance(self) -> None:
        harness = CameraHarness()
        harness._reality_touch_camera_command(
            harness.data["users"]["u"],
            "摄像头确认 " + harness._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT,
        )
        harness._tool_set_has_named_tool = lambda _tool_set, name: name == "pc_reality_touch_camera_snapshot"
        harness._record_request_prompt_fragment = AsyncMock()
        request = types.SimpleNamespace(func_tool=object(), system_prompt="基础提示")

        await CAMERA_GUIDANCE_IMPL(
            harness,
            CameraEvent("好哦，看看我在吃什么", private=True),
            request,
        )

        self.assertIn("private_companion_camera_request_v1", request.system_prompt)
        self.assertIn("应先调用 pc_reality_touch_camera_snapshot", request.system_prompt)
        self.assertIn("完整视觉摘要、直接答案和可见证据", request.system_prompt)
        self.assertIn("不得从当前时间、侧卧/抬手等姿势动作", request.system_prompt)
        harness._record_request_prompt_fragment.assert_awaited_once()

    async def test_authorized_owner_can_request_explicit_group_snapshot(self) -> None:
        harness = CameraHarness()
        harness._reality_touch_camera_snapshot_for_user = AsyncMock(
            return_value={"status": "success", "observation": {"presence": "present"}}
        )

        payload = json.loads(
            await CAMERA_TOOL_IMPL(
                harness,
                CameraEvent("可以通过摄像头看看我在不在家里", private=False),
                "判断当前是否有人在场",
            )
        )

        self.assertEqual("success", payload["status"])
        harness._reality_touch_camera_snapshot_for_user.assert_awaited_once_with(
            "u",
            "判断当前是否有人在场",
            source="assistant_tool_group",
        )

    async def test_group_snapshot_still_requires_camera_to_be_named(self) -> None:
        harness = CameraHarness()
        harness._reality_touch_camera_snapshot_for_user = AsyncMock(
            return_value={"status": "success"}
        )

        payload = json.loads(
            await CAMERA_TOOL_IMPL(
                harness,
                CameraEvent("看看我在吃什么", private=False),
                "识别可见食物",
            )
        )

        self.assertEqual("forbidden", payload["status"])
        harness._reality_touch_camera_snapshot_for_user.assert_not_awaited()

    async def test_private_self_observation_can_use_implicit_camera_wording(self) -> None:
        harness = CameraHarness()
        harness._reality_touch_camera_snapshot_for_user = AsyncMock(
            return_value={"status": "success", "observation": {"visible_food": "面条"}}
        )

        payload = json.loads(
            await CAMERA_TOOL_IMPL(
                harness,
                CameraEvent("好哦，看看我在吃什么", private=True),
                "查看当前状态",
            )
        )

        self.assertEqual("success", payload["status"])
        harness._reality_touch_camera_snapshot_for_user.assert_awaited_once_with(
            "u",
            "查看当前状态；判断画面中正在吃或喝什么",
            source="assistant_tool_private",
        )

    async def test_private_retry_inherits_one_recent_explicit_camera_request(self) -> None:
        harness = CameraHarness()
        harness._reality_touch_camera_snapshot_for_user = AsyncMock(
            side_effect=[
                {"status": "error", "message": "摄像头设备被占用"},
                {"status": "success", "observation": {"visible_food": "面条"}},
            ]
        )

        first = json.loads(
            await CAMERA_TOOL_IMPL(
                harness,
                CameraEvent("看看我在吃什么", private=True),
                "查看当前状态",
            )
        )
        second = json.loads(
            await CAMERA_TOOL_IMPL(
                harness,
                CameraEvent("再试试", private=True),
                "重新检查",
            )
        )

        self.assertEqual("error", first["status"])
        self.assertEqual("success", second["status"])
        self.assertEqual(
            harness._reality_touch_camera_snapshot_for_user.await_args_list[1].args,
            ("u", "查看当前状态；判断画面中正在吃或喝什么"),
        )
        self.assertEqual(
            harness._reality_touch_camera_snapshot_for_user.await_args_list[1].kwargs,
            {"source": "assistant_tool_private"},
        )

    async def test_retry_without_recent_camera_request_is_forbidden_without_fabrication(self) -> None:
        harness = CameraHarness()
        harness._reality_touch_camera_snapshot_for_user = AsyncMock(return_value={"status": "success"})

        payload = json.loads(
            await CAMERA_TOOL_IMPL(
                harness,
                CameraEvent("再试试", private=True),
                "检查当前状态",
            )
        )

        self.assertEqual("forbidden", payload["status"])
        self.assertFalse(payload["captured"])
        self.assertTrue(payload["must_not_claim_observed"])
        self.assertFalse(payload["same_turn_retry_allowed"])
        self.assertIn("不得声称", payload["final_response_instruction"])
        harness._reality_touch_camera_snapshot_for_user.assert_not_awaited()

    async def test_group_retry_never_inherits_private_camera_request(self) -> None:
        harness = CameraHarness()
        harness._reality_touch_camera_snapshot_for_user = AsyncMock(
            return_value={"status": "success", "observation": {"presence": "present"}}
        )

        await CAMERA_TOOL_IMPL(
            harness,
            CameraEvent("用摄像头看看我在不在家", private=True),
            "判断当前是否有人在场",
        )
        group_payload = json.loads(
            await CAMERA_TOOL_IMPL(
                harness,
                CameraEvent("再试试", private=False),
                "判断当前是否有人在场",
            )
        )

        self.assertEqual("forbidden", group_payload["status"])
        self.assertEqual(1, harness._reality_touch_camera_snapshot_for_user.await_count)

    async def test_private_retry_injects_camera_guidance_from_recent_request(self) -> None:
        harness = CameraHarness()
        harness._reality_touch_camera_command(
            harness.data["users"]["u"],
            "摄像头确认 " + harness._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT,
        )
        harness._reality_touch_camera_snapshot_for_user = AsyncMock(
            return_value={"status": "error", "message": "摄像头设备被占用"}
        )
        await CAMERA_TOOL_IMPL(
            harness,
            CameraEvent("看看我在吃什么", private=True),
            "查看当前状态",
        )
        harness._tool_set_has_named_tool = lambda _tool_set, name: name == "pc_reality_touch_camera_snapshot"
        harness._record_request_prompt_fragment = AsyncMock()
        request = types.SimpleNamespace(func_tool=object(), system_prompt="基础提示")

        await CAMERA_GUIDANCE_IMPL(
            harness,
            CameraEvent("再试试", private=True),
            request,
        )

        self.assertIn("上一条明确单帧视觉请求的短时重试", request.system_prompt)
        self.assertIn("完整视觉摘要、直接答案和可见证据", request.system_prompt)
        self.assertIn("不要在工具失败时猜测画面或说成‘又没看到’", request.system_prompt)

    async def test_camera_failure_receipt_forbids_fabricated_observation(self) -> None:
        harness = CameraHarness()
        harness._reality_touch_camera_snapshot_for_user = AsyncMock(
            return_value={"status": "error", "message": "摄像头设备被占用"}
        )

        payload = json.loads(
            await CAMERA_TOOL_IMPL(
                harness,
                CameraEvent("用摄像头看看我在不在家", private=True),
                "判断当前是否有人在场",
            )
        )

        self.assertEqual("error", payload["status"])
        self.assertFalse(payload["captured"])
        self.assertTrue(payload["must_not_claim_observed"])
        self.assertFalse(payload["same_turn_retry_allowed"])
        self.assertIn("没有获得任何可用画面", payload["final_response_instruction"])
        self.assertIn("摄像头设备被占用", payload["message"])

    async def test_uncertain_visual_result_preserves_successful_capture(self) -> None:
        harness = CameraHarness()
        harness._reality_touch_camera_snapshot_for_user = AsyncMock(
            return_value={
                "status": "observation_uncertain",
                "message": "已成功读取单帧，但视觉模型未能可靠回答本次观察目的",
                "captured": True,
                "answer_available": False,
                "observation": {
                    "brightness": "normal",
                    "confidence": 0.0,
                    "answer_status": "uncertain",
                },
                "final_response_instruction": "只能说明无法判断，不得声称画面黑或镜头被挡。",
            }
        )

        payload = json.loads(
            await CAMERA_TOOL_IMPL(
                harness,
                CameraEvent("用摄像头看看我在吃什么", private=True),
                "判断画面中正在吃或喝什么",
            )
        )

        self.assertEqual("observation_uncertain", payload["status"])
        self.assertTrue(payload["captured"])
        self.assertFalse(payload["answer_available"])
        self.assertIn("不得声称画面黑", payload["final_response_instruction"])


class RealityTouchCameraCaptureTests(unittest.TestCase):
    def test_device_catalog_is_only_enumerated_on_explicit_refresh(self) -> None:
        harness = CameraHarness()
        probed_indexes: list[int] = []
        released_indexes: list[int] = []

        class Capture:
            def __init__(self, index: int) -> None:
                self.index = index

            def isOpened(self) -> bool:
                return self.index == 2

            def getBackendName(self) -> str:
                return "MSMF"

            def release(self) -> None:
                released_indexes.append(self.index)

        def open_capture(index: int) -> Capture:
            probed_indexes.append(index)
            return Capture(index)

        fake_cv2 = types.SimpleNamespace(VideoCapture=open_capture)
        with patch.dict(sys.modules, {"cv2": fake_cv2}):
            self.assertEqual([], harness._reality_touch_camera_devices(refresh=False)["devices"])
            self.assertEqual([], probed_indexes)
            catalog = harness._reality_touch_camera_devices(refresh=True)
        self.assertEqual(list(range(8)), probed_indexes)
        self.assertEqual(list(range(8)), released_indexes)
        self.assertEqual(2, catalog["devices"][0]["index"])
        self.assertEqual("摄像头 2", catalog["devices"][0]["name"])
        self.assertEqual("MSMF", catalog["devices"][0]["backend"])

    def test_capture_reads_one_frame_and_always_releases_device(self) -> None:
        harness = CameraHarness()

        class Frame:
            shape = (480, 640, 3)
            size = 480 * 640 * 3

            @staticmethod
            def mean() -> float:
                return 100.0

        class Capture:
            read_count = 0
            released = False

            @staticmethod
            def isOpened() -> bool:
                return True

            def read(self):
                self.read_count += 1
                return True, Frame()

            def release(self) -> None:
                self.released = True

        capture = Capture()
        fake_cv2 = types.SimpleNamespace(
            VideoCapture=lambda _index: capture,
            imencode=lambda *_args, **_kwargs: (True, bytearray(b"jpeg")),
            IMWRITE_JPEG_QUALITY=1,
        )
        with patch.dict(sys.modules, {"cv2": fake_cv2}):
            result = harness._capture_reality_touch_camera_frame()
        self.assertEqual(1, capture.read_count)
        self.assertTrue(capture.released)
        self.assertEqual(b"jpeg", result["jpeg_bytes"])

    def test_capture_failure_still_releases_device(self) -> None:
        harness = CameraHarness()

        class Capture:
            released = False

            @staticmethod
            def isOpened() -> bool:
                return True

            @staticmethod
            def read():
                return False, None

            def release(self) -> None:
                self.released = True

        capture = Capture()
        fake_cv2 = types.SimpleNamespace(VideoCapture=lambda _index: capture)
        with patch.dict(sys.modules, {"cv2": fake_cv2}):
            with self.assertRaisesRegex(RuntimeError, "未返回画面"):
                harness._capture_reality_touch_camera_frame()
        self.assertTrue(capture.released)


class RealityTouchCameraSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = CameraHarness()
        self.user = self.harness.data["users"]["u"]

    def grant(self) -> None:
        self.harness._reality_touch_camera_command(
            self.user, "摄像头确认 " + self.harness._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT
        )

    async def test_global_switch_and_user_consent_are_both_required(self) -> None:
        self.harness.enable_reality_touch_camera = False
        result = await self.harness._reality_touch_camera_snapshot_for_user("u", "判断是否适合主动问候")
        self.assertEqual("disabled", result["status"])
        self.harness.enable_reality_touch_camera = True
        result = await self.harness._reality_touch_camera_snapshot_for_user("u", "判断是否适合主动问候")
        self.assertEqual("forbidden", result["status"])

    async def test_legacy_consent_cannot_bypass_current_camera_eligibility(self) -> None:
        self.grant()
        self.harness.owner_user_ids.clear()
        self.user["proactive_private_enabled"] = True
        snapshot = self.harness._reality_touch_camera_user_snapshot(self.user, user_id="u")
        self.assertFalse(snapshot["eligible"])
        self.assertFalse(snapshot["consented"])
        self.assertFalse(snapshot["enabled"])
        result = await self.harness._reality_touch_camera_snapshot_for_user("u", "测试历史授权边界")
        self.assertEqual("forbidden", result["status"])

    async def test_snapshot_returns_only_limited_state_and_enforces_cooldown(self) -> None:
        self.grant()
        self.harness._capture_reality_touch_camera_frame = lambda: {
            "jpeg_bytes": b"temporary-in-memory-frame", "width": 640, "height": 480, "brightness": "normal"
        }
        self.harness._analyze_reality_touch_camera_frame = AsyncMock(return_value={
            "presence": "present", "activity": "at_desk", "interruptibility": "medium",
            "brightness": "normal", "confidence": 0.8, "analyzed": True, "width": 640, "height": 480,
            "answer_available": True,
            "summary": "在场=present，活动=at_desk，可打扰性=medium，光线=normal",
        })
        result = await self.harness._reality_touch_camera_snapshot_for_user("u", "判断是否适合主动问候")
        self.assertEqual("success", result["status"])
        self.assertIn("不得从时间、姿势、动作", result["final_response_instruction"])
        serialized = json.dumps(result, ensure_ascii=False)
        for forbidden in ("jpeg", "path", "identity", "face"):
            self.assertNotIn(forbidden, serialized.lower())
        second = await self.harness._reality_touch_camera_snapshot_for_user("u", "再次判断")
        self.assertEqual("cooldown", second["status"])
        self.assertGreater(second["retry_after"], 0)

    async def test_snapshot_marks_captured_frame_uncertain_when_vision_has_no_answer(self) -> None:
        self.grant()
        self.harness._capture_reality_touch_camera_frame = lambda: {
            "jpeg_bytes": b"captured-frame",
            "width": 640,
            "height": 480,
            "brightness": "normal",
        }
        self.harness._analyze_reality_touch_camera_frame = AsyncMock(return_value={
            "presence": "present",
            "activity": "unknown",
            "interruptibility": "unknown",
            "brightness": "normal",
            "confidence": 0.0,
            "analyzed": True,
            "answer_status": "uncertain",
            "answer_available": False,
            "width": 640,
            "height": 480,
            "summary": "当前帧无法可靠判断食物或饮品",
        })

        result = await self.harness._reality_touch_camera_snapshot_for_user(
            "u",
            "判断画面中正在吃或喝什么",
        )

        self.assertEqual("observation_uncertain", result["status"])
        self.assertTrue(result["captured"])
        self.assertFalse(result["answer_available"])
        self.assertIn("不得改写成画面很黑", result["final_response_instruction"])

    async def test_analyzer_returns_complete_visual_semantics_for_dialogue_model(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.prompt = ""
                self.image_urls = []
                self.max_tokens = 0

            async def text_chat(self, *, prompt, image_urls, max_tokens):
                self.prompt = prompt
                self.image_urls = image_urls
                self.max_tokens = max_tokens
                return types.SimpleNamespace(completion_text=json.dumps({
                    "scene_description": "桌面上摆着打开的餐盒和饮料，人物坐在桌边。",
                    "purpose_answer": "能看到餐盒里的米饭和配菜，以及一杯饮料。",
                    "visible_evidence": ["打开的餐盒", "餐盒旁的饮料杯"],
                    "uncertainty": "无法确认饮料种类",
                    "answer_status": "answered",
                    "presence": "present",
                    "activity": "eating",
                    "activity_detail": "坐在桌边吃饭",
                    "interruptibility": "medium",
                    "brightness": "normal",
                    "food_visibility": "visible",
                    "visible_food": "米饭、配菜和一杯饮料",
                    "confidence": 0.88,
                }, ensure_ascii=False))

        provider = Provider()
        self.harness.plugin_vision_provider_id = "vision"
        self.harness.context = types.SimpleNamespace(get_provider_by_id=lambda _provider_id: provider)
        observation = await self.harness._analyze_reality_touch_camera_frame(
            {
                "jpeg_bytes": b"camera-jpeg",
                "width": 640,
                "height": 480,
                "brightness": "normal",
            },
            "看看我在吃什么",
        )

        self.assertTrue(observation["answer_available"])
        self.assertIn("完整理解整幅画面", provider.prompt)
        self.assertIn("scene_description", provider.prompt)
        self.assertTrue(provider.image_urls[0].startswith("data:image/jpeg;base64,"))
        self.assertGreaterEqual(provider.max_tokens, 400)
        self.assertIn("桌面上摆着打开的餐盒", observation["scene_description"])
        self.assertIn("米饭和配菜", observation["purpose_answer"])

    async def test_failed_capture_audit_contains_no_raw_frame(self) -> None:
        self.grant()

        def fail():
            raise RuntimeError("设备被占用")

        self.harness._capture_reality_touch_camera_frame = fail
        result = await self.harness._reality_touch_camera_snapshot_for_user("u", "手动检查设备")
        self.assertEqual("error", result["status"])
        latest = self.user["reality_touch_camera_policy"]["last_observation"]
        self.assertFalse(latest["success"])
        self.assertNotIn("jpeg_bytes", latest)
        self.assertNotIn("path", latest)

    async def test_page_preview_is_opt_in_and_not_written_to_user_data(self) -> None:
        self.grant()
        self.harness._capture_reality_touch_camera_frame = lambda: {
            "jpeg_bytes": b"one-frame-preview",
            "width": 320,
            "height": 240,
            "brightness": "normal",
        }
        self.harness._analyze_reality_touch_camera_frame = AsyncMock(return_value={
            "presence": "uncertain", "activity": "unknown", "interruptibility": "unknown",
            "brightness": "normal", "confidence": 0.0, "analyzed": False,
            "width": 320, "height": 240, "summary": "有限状态不可确定",
        })
        result = await self.harness._reality_touch_camera_snapshot_for_user(
            "u",
            "管理员页面手动预览",
            include_preview=True,
        )
        self.assertTrue(result["preview_data_url"].startswith("data:image/jpeg;base64,"))
        persisted = json.dumps(self.user, ensure_ascii=False)
        self.assertNotIn("preview_data_url", persisted)
        self.assertNotIn("one-frame-preview", persisted)


class RealityTouchCameraProactiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = CameraHarness()
        self.user = self.harness.data["users"]["u"]
        self.user["user_id"] = "u"
        self.harness._reality_touch_camera_command(
            self.user,
            "摄像头确认 " + self.harness._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT,
        )
        self.harness.enable_reality_touch_camera_proactive_curiosity = True

    def test_auto_mode_downgrades_to_ask_below_minimum_tier(self) -> None:
        self.user["reality_touch_camera_policy"]["proactive_mode"] = "auto"
        self.harness.proactive_tier = 3

        state = self.harness._reality_touch_camera_proactive_state(self.user, user_id="u")
        prompt = self.harness._reality_touch_camera_proactive_prompt(self.user, user_id="u")

        self.assertEqual("ask", state["effective_mode"])
        self.assertFalse(state["direct_allowed"])
        self.assertTrue(state["ask_allowed"])
        self.assertIn("不能调用摄像头工具", prompt)

    def test_auto_mode_allows_optional_direct_glance_at_matching_tier(self) -> None:
        self.user["reality_touch_camera_policy"]["proactive_mode"] = "auto"

        state = self.harness._reality_touch_camera_proactive_state(self.user, user_id="u")
        prompt = self.harness._reality_touch_camera_proactive_prompt(self.user, user_id="u")

        self.assertTrue(state["direct_allowed"])
        self.assertEqual(1, state["remaining_today"])
        self.assertIn("独立、低频的可选能力", prompt)
        self.assertIn("pc_reality_touch_camera_snapshot", prompt)
        self.assertIn("普通问候", prompt)

    def test_silence_disables_chain_but_zero_override_keeps_ask_mode(self) -> None:
        policy = self.user["reality_touch_camera_policy"]
        policy["proactive_mode"] = "auto"
        self.user["ignored_streak"] = 1
        state = self.harness._reality_touch_camera_proactive_state(self.user, user_id="u")
        self.assertFalse(state["ask_allowed"])
        self.assertIn("沉默", state["reason"])

        self.user["ignored_streak"] = 0
        policy["proactive_max_daily"] = 0
        state = self.harness._reality_touch_camera_proactive_state(self.user, user_id="u")
        self.assertFalse(state["direct_allowed"])
        self.assertTrue(state["ask_allowed"])
        self.assertIn("日额度", state["direct_reason"])
        self.assertIn("不能调用摄像头工具", self.harness._reality_touch_camera_proactive_prompt(self.user, user_id="u"))

    async def test_proactive_snapshot_uses_independent_daily_counter(self) -> None:
        policy = self.user["reality_touch_camera_policy"]
        policy["proactive_mode"] = "auto"
        self.harness._capture_reality_touch_camera_frame = lambda: {
            "jpeg_bytes": b"one-frame",
            "width": 320,
            "height": 240,
            "brightness": "normal",
        }
        self.harness._analyze_reality_touch_camera_frame = AsyncMock(return_value={
            "presence": "present",
            "activity": "eating",
            "interruptibility": "medium",
            "brightness": "normal",
            "confidence": 0.8,
            "analyzed": True,
            "answer_available": True,
            "width": 320,
            "height": 240,
            "summary": "在场，正在进行日常活动",
        })

        result = await self.harness._reality_touch_camera_snapshot_for_user(
            "u",
            "看看用户刚提到的现实活动，决定如何自然接话",
            source="proactive_curiosity",
        )

        self.assertEqual("success", result["status"])
        self.assertEqual(1, policy["proactive_used_today"])
        self.assertEqual("2026-08-11", policy["proactive_used_day"])
        self.assertEqual("proactive_curiosity", policy["last_observation"]["source"])

        second = await self.harness._reality_touch_camera_snapshot_for_user(
            "u",
            "再次主动查看",
            source="proactive_curiosity",
        )
        self.assertEqual("forbidden", second["status"])
        self.assertIn("额度", second["message"])

    def test_policy_update_normalizes_mode_and_user_quota(self) -> None:
        policy = self.harness._reality_touch_update_camera_policy(
            self.user,
            {
                "camera_enabled": True,
                "proactive_mode": "authorized",
                "proactive_max_daily": 99,
            },
            user_id="u",
        )
        self.assertEqual("auto", policy["proactive_mode"])
        self.assertEqual(10, policy["proactive_max_daily"])


if __name__ == "__main__":
    unittest.main()
