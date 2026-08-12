# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock

from astrbot_plugin_private_companion.main import PrivateCompanionExtensionAPI
from astrbot_plugin_reality_companion.main import RealityCompanionPlugin


CAMERA_DEFAULTS = {
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


class _Config(dict):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self) -> None:
        self.save_count += 1


class LegacyExportTests(TestCase):
    def test_export_reads_grouped_legacy_config_without_runtime_attributes(self) -> None:
        plugin = SimpleNamespace(
            data={"users": {}, "reality_touch": {}},
            config={
                "enable_experimental_bluetooth_wakeup": False,
                "enable_reality_touch_camera": False,
                "reality_touch_camera_index": 0,
                "experimental_motivation_config": {
                    "enable_experimental_bluetooth_wakeup": True,
                    "enable_reality_touch_camera": True,
                    "reality_touch_camera_index": 701,
                    "reality_touch_camera_min_interval_seconds": 120,
                    "reality_touch_camera_capture_timeout_seconds": 8,
                    "reality_touch_camera_analysis_timeout_seconds": 45,
                    "enable_reality_touch_camera_proactive_curiosity": True,
                    "reality_touch_camera_proactive_min_tier": 5,
                    "reality_touch_camera_proactive_max_daily": 3,
                    "reality_touch_camera_proactive_cooldown_minutes": 360,
                },
                "tts_behavior_config": {"tts_local_playback_volume": 68},
            },
        )

        exported = PrivateCompanionExtensionAPI(plugin).export_reality_touch_legacy_state()["config"]

        self.assertTrue(exported["enabled"])
        self.assertTrue(exported["camera_enabled"])
        self.assertEqual(701, exported["camera_index"])
        self.assertEqual(120, exported["camera_min_interval_seconds"])
        self.assertEqual(8, exported["camera_capture_timeout_seconds"])
        self.assertEqual(45, exported["camera_analysis_timeout_seconds"])
        self.assertTrue(exported["camera_proactive_curiosity_enabled"])
        self.assertEqual(5, exported["camera_proactive_min_tier"])
        self.assertEqual(3, exported["camera_proactive_max_daily"])
        self.assertEqual(360, exported["camera_proactive_cooldown_minutes"])
        self.assertEqual(68, exported["audio_default_playback_volume"])

    def test_export_falls_back_to_flat_legacy_config(self) -> None:
        plugin = SimpleNamespace(
            data={"users": {}, "reality_touch": {}},
            config={
                "enable_experimental_bluetooth_wakeup": "true",
                "enable_reality_touch_camera": "true",
                "reality_touch_camera_index": "1400",
                "tts_local_playback_volume": "52",
            },
        )

        exported = PrivateCompanionExtensionAPI(plugin).export_reality_touch_legacy_state()["config"]

        self.assertTrue(exported["enabled"])
        self.assertTrue(exported["camera_enabled"])
        self.assertEqual(1400, exported["camera_index"])
        self.assertEqual(52, exported["audio_default_playback_volume"])


class LegacyImportTests(IsolatedAsyncioTestCase):
    @staticmethod
    def _plugin(config: _Config, legacy_config: dict) -> RealityCompanionPlugin:
        plugin = object.__new__(RealityCompanionPlugin)
        plugin.config = config
        plugin.data = {"version": 1, "users": {}, "reality_touch": {}}
        plugin._save_data_sync = Mock()
        plugin._private_companion_api = lambda: SimpleNamespace(
            export_reality_touch_legacy_state=lambda: {
                "version": 1,
                "users": {},
                "reality_touch": {},
                "config": copy.deepcopy(legacy_config),
            }
        )
        return plugin

    async def test_schema_defaults_are_replaced_and_runtime_is_synchronized(self) -> None:
        config = _Config(
            {
                "enabled": False,
                "camera": copy.deepcopy(CAMERA_DEFAULTS),
                "audio": {"default_playback_volume": 35},
            }
        )
        plugin = self._plugin(
            config,
            {
                "enabled": True,
                "camera_enabled": True,
                "camera_index": 700,
                "camera_min_interval_seconds": 90,
                "camera_capture_timeout_seconds": 7,
                "camera_analysis_timeout_seconds": 40,
                "camera_proactive_curiosity_enabled": True,
                "camera_proactive_min_tier": 5,
                "camera_proactive_max_daily": 4,
                "camera_proactive_cooldown_minutes": 300,
                "audio_default_playback_volume": 64,
            },
        )

        self.assertTrue(await plugin._try_legacy_migration())

        self.assertTrue(config["enabled"])
        self.assertEqual(700, config["camera"]["index"])
        self.assertEqual(90, config["camera"]["min_interval_seconds"])
        self.assertEqual(64, config["audio"]["default_playback_volume"])
        self.assertTrue(plugin.enable_experimental_bluetooth_wakeup)
        self.assertTrue(plugin.enable_reality_touch_camera)
        self.assertEqual(700, plugin.reality_touch_camera_index)
        self.assertEqual(90, plugin.reality_touch_camera_min_interval_seconds)
        self.assertEqual(64, plugin.tts_local_playback_volume)
        self.assertEqual(1, config.save_count)
        self.assertTrue(plugin.data["legacy_migration_completed"])
        plugin._save_data_sync.assert_called_once_with()

    async def test_nondefault_new_group_is_preserved_while_missing_field_is_filled(self) -> None:
        camera = copy.deepcopy(CAMERA_DEFAULTS)
        camera["index"] = 9
        camera.pop("analysis_timeout_seconds")
        config = _Config(
            {
                "enabled": True,
                "camera": camera,
                "audio": {"default_playback_volume": 73},
            }
        )
        plugin = self._plugin(
            config,
            {
                "enabled": False,
                "camera_enabled": True,
                "camera_index": 701,
                "camera_min_interval_seconds": 180,
                "camera_capture_timeout_seconds": 11,
                "camera_analysis_timeout_seconds": 50,
                "camera_proactive_curiosity_enabled": True,
                "camera_proactive_min_tier": 5,
                "camera_proactive_max_daily": 5,
                "camera_proactive_cooldown_minutes": 480,
                "audio_default_playback_volume": 48,
            },
        )

        self.assertTrue(await plugin._try_legacy_migration())

        self.assertTrue(config["enabled"])
        self.assertFalse(config["camera"]["enabled"])
        self.assertEqual(9, config["camera"]["index"])
        self.assertEqual(60, config["camera"]["min_interval_seconds"])
        self.assertEqual(50, config["camera"]["analysis_timeout_seconds"])
        self.assertEqual(73, config["audio"]["default_playback_volume"])
        self.assertTrue(plugin.enable_experimental_bluetooth_wakeup)
        self.assertFalse(plugin.enable_reality_touch_camera)
        self.assertEqual(9, plugin.reality_touch_camera_index)
        self.assertEqual(50, plugin.reality_touch_camera_analysis_timeout_seconds)
        self.assertEqual(73, plugin.tts_local_playback_volume)

    async def test_complete_nondefault_new_config_is_not_overwritten(self) -> None:
        camera = copy.deepcopy(CAMERA_DEFAULTS)
        camera.update({"enabled": True, "index": 9, "min_interval_seconds": 120})
        config = _Config(
            {
                "enabled": True,
                "camera": camera,
                "audio": {"default_playback_volume": 73},
            }
        )
        before = copy.deepcopy(config)
        plugin = self._plugin(
            config,
            {
                "enabled": False,
                "camera_enabled": False,
                "camera_index": 701,
                "camera_min_interval_seconds": 180,
                "camera_capture_timeout_seconds": 11,
                "camera_analysis_timeout_seconds": 50,
                "camera_proactive_curiosity_enabled": True,
                "camera_proactive_min_tier": 5,
                "camera_proactive_max_daily": 5,
                "camera_proactive_cooldown_minutes": 480,
                "audio_default_playback_volume": 48,
            },
        )

        self.assertTrue(await plugin._try_legacy_migration())

        self.assertEqual(before, config)
        self.assertEqual(0, config.save_count)
        self.assertTrue(plugin.enable_experimental_bluetooth_wakeup)
        self.assertTrue(plugin.enable_reality_touch_camera)
        self.assertEqual(9, plugin.reality_touch_camera_index)
        self.assertEqual(120, plugin.reality_touch_camera_min_interval_seconds)
        self.assertEqual(73, plugin.tts_local_playback_volume)
