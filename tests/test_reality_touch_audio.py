# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np


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

from astrbot_plugin_reality_companion.reality_touch_audio import RealityTouchAudioMixin


class _FakeBlock:
    shape = (8, 2)

    def __len__(self):
        return 8

    def __mul__(self, value):
        return self


class _FakeAudio:
    channels = 2
    samplerate = 48000

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def blocks(self, **kwargs):
        return [_FakeBlock()]


class _FakeAudio44100(_FakeAudio):
    samplerate = 44100

    def read(self, **kwargs):
        return np.zeros((441, 2), dtype=np.float32)


class _FakeStream:
    def __init__(self, owner, **kwargs):
        self.owner = owner
        self.kwargs = kwargs

    def __enter__(self):
        self.owner.last_stream = self
        return self

    def __exit__(self, *args):
        return None

    def write(self, block):
        self.owner.writes += 1


class _FakeSoundDevice(types.SimpleNamespace):
    def __init__(self):
        super().__init__()
        self.default = types.SimpleNamespace(device=(0, 1))
        self.devices = [
            {"name": "Microphone", "hostapi": 0, "max_output_channels": 0, "default_samplerate": 48000},
            {"name": "Bluetooth Speaker", "hostapi": 0, "max_output_channels": 2, "default_samplerate": 48000},
            {"name": "Monitor", "hostapi": 0, "max_output_channels": 2, "default_samplerate": 48000},
        ]
        self.last_stream = None
        self.writes = 0

    def query_devices(self):
        return self.devices

    def query_hostapis(self):
        return [{"name": "Windows WASAPI"}]

    def OutputStream(self, **kwargs):
        return _FakeStream(self, **kwargs)


class AudioHarness(RealityTouchAudioMixin):
    def __init__(self) -> None:
        self.data = {}
        self.enable_experimental_bluetooth_wakeup = True
        self.tts_local_playback_volume = 50
        self.default_plays = 0
        self.default_volume = None

    def _open_tts_audio_file_local(self, path: str, *, volume=None, fade_in_ms=0) -> None:
        self.default_plays += 1
        self.default_volume = volume

    @staticmethod
    def _reality_touch_audio_consented(user) -> bool:
        return bool(user.get("consented"))


class RealityTouchAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sd = _FakeSoundDevice()
        self.sf = types.SimpleNamespace(SoundFile=lambda path: _FakeAudio())
        self.modules = patch.dict(sys.modules, {"sounddevice": self.sd, "soundfile": self.sf})
        self.modules.start()

    def tearDown(self) -> None:
        self.modules.stop()

    def test_enumerates_and_persists_specific_output_device(self) -> None:
        harness = AudioHarness()
        catalog = harness._reality_touch_audio_devices()
        self.assertTrue(catalog["backend_available"])
        self.assertEqual(3, len(catalog["devices"]))
        speaker = next(item for item in catalog["devices"] if item["name"] == "Bluetooth Speaker")
        harness._reality_touch_select_audio_device(speaker["id"])
        snapshot = harness._reality_touch_audio_snapshot()
        self.assertEqual("Bluetooth Speaker", snapshot["label"])
        self.assertEqual("selected_device", snapshot["mode"])

    def test_selected_device_is_used_for_direct_playback(self) -> None:
        harness = AudioHarness()
        speaker = next(
            item for item in harness._reality_touch_audio_devices()["devices"]
            if item["name"] == "Bluetooth Speaker"
        )
        harness._reality_touch_select_audio_device(speaker["id"])
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            harness._open_reality_touch_audio_file(audio.name)
        self.assertEqual(1, self.sd.last_stream.kwargs["device"])
        self.assertEqual(1, self.sd.writes)
        self.assertEqual(0, harness.default_plays)

    def test_reality_touch_volume_is_persisted_and_applied_to_default_output(self) -> None:
        harness = AudioHarness()
        self.assertEqual(0, harness._reality_touch_playback_volume(-10))
        self.assertEqual(100, harness._reality_touch_playback_volume(120))
        harness._reality_touch_select_audio_device("system_default", playback_volume=12)
        snapshot = harness._reality_touch_audio_snapshot()
        self.assertEqual(12, snapshot["playback_volume"])

        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            harness._open_reality_touch_audio_file(audio.name)

        self.assertEqual(1, harness.default_plays)
        self.assertEqual(12, harness.default_volume)

    def test_audio_is_resampled_to_selected_device_default_rate(self) -> None:
        harness = AudioHarness()
        speaker = next(
            item for item in harness._reality_touch_audio_devices()["devices"]
            if item["name"] == "Bluetooth Speaker"
        )
        harness._reality_touch_select_audio_device(speaker["id"])
        self.sf.SoundFile = lambda path: _FakeAudio44100()
        with tempfile.NamedTemporaryFile(suffix=".mp3") as audio:
            harness._open_reality_touch_audio_file(audio.name)
        self.assertEqual(48000, self.sd.last_stream.kwargs["samplerate"])
        self.assertEqual(1, self.sd.writes)

    def test_proactive_voice_policy_requires_consent(self) -> None:
        harness = AudioHarness()
        user = {"consented": False}
        with self.assertRaisesRegex(ValueError, "知情确认"):
            harness._reality_touch_update_policy(user, {"proactive_voice_enabled": True})
        user["consented"] = True
        harness._reality_touch_update_policy(user, {"proactive_voice_enabled": True})
        self.assertTrue(harness._reality_touch_proactive_voice_allowed(user))

    def test_fixed_device_test_audio_uses_selected_route(self) -> None:
        harness = AudioHarness()
        harness._open_reality_touch_audio_file = Mock()
        self.assertTrue(asyncio.run(harness._play_reality_touch_test_audio()))
        harness._open_reality_touch_audio_file.assert_called_once_with(
            str(harness._REALITY_TOUCH_TEST_AUDIO_PATH),
            volume=None,
        )

    def test_missing_selected_device_falls_back_to_system_default(self) -> None:
        harness = AudioHarness()
        harness.data["reality_touch"] = {"audio_output_device_id": "sd:missing", "playback_volume": 22}
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            route = harness._open_reality_touch_audio_file(audio.name)
        self.assertEqual("system_default", route["id"])
        self.assertEqual("sd:missing", route["fallback_from"])
        self.assertEqual(1, harness.default_plays)
        self.assertEqual(22, harness.default_volume)

    def test_proactive_voice_uses_its_independent_volume(self) -> None:
        harness = AudioHarness()
        user = {"consented": True}
        harness._reality_touch_update_policy(
            user,
            {"proactive_voice_enabled": True, "playback_volume": 18},
        )
        self.assertEqual(18, user["reality_touch_policy"]["playback_volume"])


if __name__ == "__main__":
    unittest.main()
