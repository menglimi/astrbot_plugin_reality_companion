# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import sys
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


PACKAGE_NAME = "astrbot_plugin_reality_companion"
ROOT = Path(__file__).resolve().parents[1]
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package

try:
    import astrbot.api  # noqa: F401
except ImportError:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        warning=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
    )
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from astrbot_plugin_reality_companion.wakeup_alarm import WakeupAlarmMixin


class AlarmHarness(WakeupAlarmMixin):
    def __init__(self) -> None:
        self.enable_experimental_bluetooth_wakeup = True
        self.environment_perception_timezone = "Asia/Shanghai"
        self.data = {"users": {"u": {"umo": "bot:FriendMessage:u"}}}
        self.played = 0
        self.replies: list[str] = []

    def _save_data_sync(self) -> None:
        return None

    def _schedule_data_save(self, **kwargs) -> None:
        return None

    def _launch_wakeup_contact_session(self, user_id, session_id):
        self.played += 1
        session = self.data["users"][str(user_id)]["wakeup_alarm"]["contact_session"]
        if session.get("status") == "pending":
            session["next_attempt_at"] = 4_000_000_000
        return asyncio.current_task()

    async def _reply(self, event, text):
        self.replies.append(text)


class DynamicAlarmHarness(WakeupAlarmMixin):
    def __init__(self, llm_result: str | None = "早呀，今天也想用我的方式把你轻轻叫起来。") -> None:
        self.environment_perception_timezone = "Asia/Shanghai"
        self.llm_result = llm_result
        self.llm_calls: list[dict] = []
        self.audio_calls: list[dict] = []

    def _wakeup_now(self) -> datetime:
        return datetime(2026, 8, 10, 7, 30)

    async def _resolve_proactive_persona_prompt(self, _user, *, umo="") -> str:
        return "人格：说话温柔、有一点熟稔的玩笑。"

    def _format_proactive_relationship_fact(self, _user) -> str:
        return "长期阶段=亲密，语气=warm"

    async def _recent_private_conversation_for_proactive_review(self, _user, *, limit=8) -> str:
        return "用户：明早九点有课。\nBot：那我到时候叫你。"

    @staticmethod
    def _task_provider(*provider_ids: str) -> str:
        return next((item for item in provider_ids if item), "")

    async def _llm_call(self, prompt: str, **kwargs):
        self.llm_calls.append({"prompt": prompt, **kwargs})
        return self.llm_result

    async def _play_reality_touch_text(self, text: str, *, repeat: int, interval: int, **kwargs) -> bool:
        self.audio_calls.append({"text": text, "repeat": repeat, "interval": interval, **kwargs})
        return True


class ContactSessionHarness(DynamicAlarmHarness):
    def __init__(self) -> None:
        super().__init__()
        self.data = {
            "users": {
                "u": {
                    "umo": "bot:FriendMessage:u",
                    "wakeup_alarm": {
                        "enabled": True,
                        "repeat_count": 1,
                        "repeat_interval_seconds": 5,
                        "require_acknowledgement": True,
                        "playback_volume": 28,
                        "volume_step": 10,
                        "max_volume": 60,
                        "fade_in_ms": 600,
                        "delivery_mode": "audio_only",
                        "contact_session": {
                            "id": "u:session",
                            "status": "pending",
                            "attempt": 0,
                            "max_attempts": 1,
                            "next_attempt_at": 0,
                            "messages": [],
                        },
                    },
                }
            }
        }

    def _schedule_data_save(self, **kwargs) -> None:
        return None


class _FeedbackEvent:
    def __init__(self) -> None:
        self.stopped = False

    def stop_event(self) -> None:
        self.stopped = True


class _ReminderCron:
    def __init__(self) -> None:
        self.jobs: dict[str, SimpleNamespace] = {}
        self.created = 0
        self.pause_next_add = False
        self.add_started = asyncio.Event()
        self.add_release = asyncio.Event()

    async def add_active_job(self, **kwargs):
        if self.pause_next_add:
            self.pause_next_add = False
            self.add_started.set()
            await self.add_release.wait()
        self.created += 1
        job_id = f"reality-job-{self.created}"
        job = SimpleNamespace(job_id=job_id, payload=dict(kwargs.get("payload") or {}))
        self.jobs[job_id] = job
        return job

    async def delete_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


class _ReminderEvent:
    def __init__(self, reminder: dict) -> None:
        self.extras = {
            "cron_payload": {
                "origin": "private_companion_reality_touch",
                "sender_id": "u",
                "private_companion": {"reminder_id": reminder["id"]},
            },
            "cron_job": {"id": reminder["job_id"]},
        }

    def get_extra(self, key=None, default=None):
        if key is None:
            return self.extras
        return self.extras.get(key, default)


class ReminderHarness(WakeupAlarmMixin):
    def __init__(self) -> None:
        self.enable_experimental_bluetooth_wakeup = True
        self.environment_perception_timezone = "Asia/Shanghai"
        self._data_lock = asyncio.Lock()
        self.cron = _ReminderCron()
        self.data = {
            "users": {
                "u": {
                    "umo": "bot:FriendMessage:u",
                    "reality_touch_consent": {
                        "confirmed": True,
                        "version": 1,
                        "granted_capabilities": ["local_audio"],
                    },
                    "wakeup_alarm": {
                        "delivery_mode": "audio_only",
                        "playback_volume": 36,
                        "fade_in_ms": 500,
                    },
                }
            }
        }
        self.audio_calls: list[dict] = []

    def _save_data_sync(self) -> None:
        return None

    def _official_cron_manager(self):
        return self.cron

    def _llm_timer_timezone_name(self) -> str:
        return "Asia/Shanghai"

    @staticmethod
    def _llm_timer_run_at(scheduled_ts: float) -> datetime:
        return datetime.fromtimestamp(scheduled_ts)

    @staticmethod
    def _environment_fromtimestamp(scheduled_ts: float) -> datetime:
        return datetime.fromtimestamp(scheduled_ts)

    async def _delete_official_llm_timer_job(self, job_id: str):
        await self.cron.delete_job(job_id)
        return True, ""

    async def _play_reality_touch_text(self, text: str, **kwargs) -> bool:
        self.audio_calls.append({"text": text, **kwargs})
        return True

    async def _send_wakeup_chat_copy(self, _user, _message) -> bool:
        return False

    async def schedule(self, topic: str) -> bool:
        return await self._schedule_reality_touch_official_reminder(
            "u",
            {"scheduled_ts": int(time.time()) + 3600, "topic": topic},
            source_text=f"提醒我{topic}",
            trigger_umo="bot:FriendMessage:u",
        )


class WakeupAlarmTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_during_official_registration_reclaims_created_job(self) -> None:
        harness = ReminderHarness()
        harness.cron.pause_next_add = True
        schedule_task = asyncio.create_task(harness.schedule("关窗"))
        await harness.cron.add_started.wait()
        reminder = next(iter(harness.data["users"]["u"]["reality_touch_reminders"].values()))

        self.assertTrue(
            await asyncio.wait_for(
                harness._cancel_reality_touch_official_reminder("u", reminder_id=reminder["id"]),
                timeout=1,
            )
        )
        harness.cron.add_release.set()

        self.assertFalse(await schedule_task)
        self.assertEqual("cancelled", reminder["status"])
        self.assertEqual({}, harness.cron.jobs)

    async def test_official_reality_touch_reminders_are_independent_and_idempotent(self) -> None:
        harness = ReminderHarness()
        self.assertTrue(await harness.schedule("喝水"))
        self.assertTrue(await harness.schedule("拿快递"))

        reminders = harness.data["users"]["u"]["reality_touch_reminders"]
        self.assertEqual(2, len(reminders))
        self.assertEqual(2, len(harness.cron.jobs))
        first = next(item for item in reminders.values() if item["topic"] == "喝水")
        second = next(item for item in reminders.values() if item["topic"] == "拿快递")
        event = _ReminderEvent(first)

        self.assertTrue(await harness._acknowledge_official_reality_touch_trigger(event))
        self.assertEqual("triggered", first["status"])
        delivered, _ = await harness._execute_official_reality_touch_reminder(event, "该喝水了。")
        self.assertTrue(delivered)
        delivered_again, detail = await harness._execute_official_reality_touch_reminder(event, "重复提醒")
        self.assertTrue(delivered_again)
        self.assertIn("跳过重复调用", detail)
        self.assertEqual(1, len(harness.audio_calls))
        self.assertTrue(await harness._complete_official_reality_touch_reminder(event))
        self.assertEqual("completed", first["status"])
        self.assertEqual("scheduled", second["status"])

        self.assertTrue(
            await harness._cancel_reality_touch_official_reminder(
                "u",
                reminder_id=second["id"],
            )
        )
        self.assertEqual("cancelled", second["status"])
        self.assertNotIn(second["job_id"], harness.cron.jobs)
        self.assertIn(first["job_id"], harness.cron.jobs)

    def test_command_and_day_normalization(self) -> None:
        harness = AlarmHarness()
        user = harness.data["users"]["u"]
        response, test = harness._wakeup_alarm_command(user, "07:30")
        self.assertFalse(test)
        self.assertIn("只需在 10 分钟内单独发送", response)
        self.assertFalse(user["wakeup_alarm"].get("enabled"))

        response = harness._reality_touch_apply_pending_confirmation(
            user,
            "我理解风险并确认授权",
        )
        self.assertIn("当前只授权本机音频能力", response)
        self.assertTrue(harness._reality_touch_audio_consented(user))

        confirmation = f"确认 {harness._REALITY_TOUCH_CONFIRMATION_TEXT}"
        response, test = harness._wakeup_alarm_command(user, confirmation)
        self.assertFalse(test)
        self.assertIn("未授权摄像头", response)
        self.assertEqual(["local_audio"], user["reality_touch_consent"]["granted_capabilities"])
        self.assertFalse(user["reality_touch_consent"]["camera_granted"])
        self.assertFalse(harness._reality_touch_capability_consented(user, "camera"))

        response, test = harness._wakeup_alarm_command(user, "07:30 周一")
        self.assertFalse(test)
        self.assertIn("07:30", response)
        self.assertEqual([0], user["wakeup_alarm"]["days"])
        self.assertEqual("", user["wakeup_alarm"]["message"])
        self.assertEqual("07:30", harness._wakeup_parse_time("7：30"))
        self.assertEqual(list(range(7)), harness._wakeup_days([0, 1, 2, 3, 4, 5, 6]))

    def test_page_console_snapshot_and_update_keep_consent_boundary(self) -> None:
        harness = AlarmHarness()
        harness._wakeup_now = lambda: datetime(2026, 8, 10, 7, 0)
        user = harness.data["users"]["u"]
        with self.assertRaisesRegex(ValueError, "知情确认"):
            harness._reality_touch_update_alarm(
                user,
                {"enabled": True, "time": "08:00", "days": [0], "message": "起床"},
            )

        harness._wakeup_alarm_command(user, f"确认 {harness._REALITY_TOUCH_CONFIRMATION_TEXT}")
        alarm = harness._reality_touch_update_alarm(
            user,
            {
                "enabled": True,
                "time": "08:00",
                "days": [0],
                "message": "起床，先喝水。",
                "repeat_count": 2,
                "repeat_interval_seconds": 15,
            },
        )
        self.assertEqual([0], alarm["days"])
        self.assertEqual(2, alarm["repeat_count"])

        snapshot = harness._reality_touch_page_snapshot()
        self.assertTrue(snapshot["global_enabled"])
        self.assertEqual(1, snapshot["counts"]["consented"])
        self.assertEqual(1, snapshot["counts"]["scheduled"])
        self.assertIn("陪伴 现实触及 确认", snapshot["confirmation_command"])
        row = snapshot["users"][0]
        self.assertTrue(row["consent"]["local_audio"])
        self.assertFalse(row["consent"]["camera"])
        self.assertEqual("08-10 08:00", row["alarm"]["next_trigger_text"])
        self.assertEqual("dynamic", row["alarm"]["message_mode"])

    async def test_each_playback_generates_one_contextual_message_then_repeats_it(self) -> None:
        harness = DynamicAlarmHarness()
        user = {
            "umo": "bot:FriendMessage:u",
            "nickname": "小林",
        }
        alarm = {
            "message": "温柔一点，并提醒我上午有课",
            "repeat_count": 3,
            "repeat_interval_seconds": 15,
        }

        played = await harness._play_wakeup_alarm(user, alarm)

        self.assertTrue(played)
        self.assertEqual(1, len(harness.llm_calls))
        self.assertEqual(
            [{"text": harness.llm_result, "repeat": 3, "interval": 15, "fade_in_ms": 800, "source": "wakeup_alarm"}],
            harness.audio_calls,
        )
        call = harness.llm_calls[0]
        self.assertIn("2026-08-10 07:30，周一", call["prompt"])
        self.assertIn("小林", call["prompt"])
        self.assertIn("长期阶段=亲密", call["prompt"])
        self.assertIn("明早九点有课", call["prompt"])
        self.assertIn("温柔一点，并提醒我上午有课", call["prompt"])
        self.assertIn("说话温柔", call["system_prompt"])

    async def test_model_empty_result_uses_fixed_text_only_as_final_fallback(self) -> None:
        harness = DynamicAlarmHarness(llm_result=None)

        await harness._play_wakeup_alarm({"umo": "bot:FriendMessage:u"}, {}, test=True)

        self.assertEqual(1, len(harness.llm_calls))
        self.assertEqual(harness._WAKEUP_DEFAULT_MESSAGE, harness.audio_calls[0]["text"])
        self.assertEqual(1, harness.audio_calls[0]["repeat"])

    async def test_contact_session_records_attempt_volume_and_completion(self) -> None:
        harness = ContactSessionHarness()

        await harness._run_wakeup_contact_session("u", "u:session")

        session = harness.data["users"]["u"]["wakeup_alarm"]["contact_session"]
        self.assertEqual("exhausted", session["status"])
        self.assertEqual(1, session["attempt"])
        self.assertEqual(28, session["last_volume"])
        self.assertTrue(session["last_playback_success"])
        self.assertEqual("wakeup_alarm", harness.audio_calls[0]["source"])
        self.assertEqual(600, harness.audio_calls[0]["fade_in_ms"])

    async def test_tick_is_idempotent_for_one_minute(self) -> None:
        harness = AlarmHarness()
        user = harness.data["users"]["u"]
        harness._wakeup_alarm_command(user, f"确认 {harness._REALITY_TOUCH_CONFIRMATION_TEXT}")
        harness._wakeup_alarm_command(user, "07:30")
        harness._wakeup_now = lambda: datetime(2026, 8, 10, 7, 30)
        await harness._run_wakeup_alarm_tick()
        await harness._run_wakeup_alarm_tick()
        self.assertEqual(1, harness.played)

    async def test_tick_requires_current_consent(self) -> None:
        harness = AlarmHarness()
        user = harness.data["users"]["u"]
        user["wakeup_alarm"] = {"enabled": True, "time": "07:30", "days": list(range(7))}
        harness._wakeup_now = lambda: datetime(2026, 8, 10, 7, 30)
        await harness._run_wakeup_alarm_tick()
        self.assertEqual(0, harness.played)

        harness._wakeup_alarm_command(user, f"确认 {harness._REALITY_TOUCH_CONFIRMATION_TEXT}")
        harness._wakeup_alarm_command(user, "撤销确认")
        self.assertFalse(user["wakeup_alarm"]["enabled"])
        self.assertNotIn("reality_touch_consent", user)

    async def test_awake_feedback_stops_pending_contact_session(self) -> None:
        harness = AlarmHarness()
        user = harness.data["users"]["u"]
        user["wakeup_alarm"] = {
            "enabled": True,
            "snooze_minutes": 10,
            "contact_session": {
                "id": "u:202608100730",
                "status": "pending",
                "attempt": 1,
                "max_attempts": 3,
            },
        }
        event = _FeedbackEvent()

        handled = await harness._maybe_handle_wakeup_feedback(event, "u", user, "我醒了")

        self.assertTrue(handled)
        self.assertTrue(event.stopped)
        self.assertEqual("acknowledged", user["wakeup_alarm"]["contact_session"]["status"])
        self.assertIn("已经醒来", harness.replies[-1])

    async def test_snooze_feedback_reschedules_without_disabling_alarm(self) -> None:
        harness = AlarmHarness()
        user = harness.data["users"]["u"]
        user["wakeup_alarm"] = {
            "enabled": True,
            "snooze_minutes": 10,
            "contact_session": {
                "id": "u:202608100730",
                "status": "pending",
                "attempt": 1,
                "max_attempts": 3,
            },
        }
        event = _FeedbackEvent()

        handled = await harness._maybe_handle_wakeup_feedback(event, "u", user, "15分钟后再叫我")

        self.assertTrue(handled)
        self.assertTrue(user["wakeup_alarm"]["enabled"])
        self.assertEqual("snoozed", user["wakeup_alarm"]["contact_session"]["status"])
        self.assertIn("15 分钟后", harness.replies[-1])

    def test_attempt_volume_increases_without_exceeding_cap(self) -> None:
        harness = AlarmHarness()
        alarm = {"playback_volume": 30, "volume_step": 12, "max_volume": 50}
        self.assertEqual(30, harness._wakeup_attempt_volume(alarm, 1))
        self.assertEqual(42, harness._wakeup_attempt_volume(alarm, 2))
        self.assertEqual(50, harness._wakeup_attempt_volume(alarm, 3))

    async def test_future_tense_awake_phrase_is_not_treated_as_confirmation(self) -> None:
        harness = AlarmHarness()
        intent, minutes = await harness._classify_wakeup_feedback("等我醒了以后再说", 10)
        self.assertEqual(("other", 0), (intent, minutes))


if __name__ == "__main__":
    unittest.main()
