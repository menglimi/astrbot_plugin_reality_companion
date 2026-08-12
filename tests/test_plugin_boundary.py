# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = ROOT.parent / "astrbot_plugin_private_companion"


def test_metadata_follows_companion_series_naming() -> None:
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    assert "name: astrbot_plugin_reality_companion" in metadata
    assert "display_name: 我会来到你身边" in metadata


def test_runtime_and_metadata_versions_match() -> None:
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    metadata_version = re.search(r"^version:\s*([^\s]+)\s*$", metadata, re.MULTILINE)
    runtime_version = re.search(r'^PLUGIN_VERSION\s*=\s*"([^"]+)"\s*$', main, re.MULTILINE)
    assert metadata_version is not None
    assert runtime_version is not None
    assert runtime_version.group(1) == metadata_version.group(1)


def test_heavy_device_dependencies_live_only_in_reality_plugin() -> None:
    reality_requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    private_requirements = (PRIVATE_ROOT / "requirements.txt").read_text(encoding="utf-8")
    for dependency in ("sounddevice", "soundfile", "opencv-python-headless", "cv2-enumerate-cameras"):
        assert dependency in reality_requirements
        assert dependency not in private_requirements


def test_schema_is_valid_utf8_json() -> None:
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["enabled"]["default"] is False
    assert schema["camera"]["items"]["enabled"]["default"] is False


def test_private_companion_uses_optional_bridge_instead_of_device_mixin() -> None:
    main = (PRIVATE_ROOT / "main.py").read_text(encoding="utf-8")
    bridge = (PRIVATE_ROOT / "reality_companion_bridge.py").read_text(encoding="utf-8")
    assert "from .reality_companion_bridge import RealityCompanionBridgeMixin" in main
    assert "from .wakeup_alarm import WakeupAlarmMixin" not in main
    assert "astrbot_plugin_reality_companion" in bridge
