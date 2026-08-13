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
    for dependency in ("sounddevice", "soundfile"):
        assert dependency in reality_requirements
        assert dependency not in private_requirements
    assert "cv2-enumerate-cameras" not in reality_requirements
    assert "opencv-python-headless" not in reality_requirements


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


def test_page_api_uses_astrbot_request_context() -> None:
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "from astrbot.api.web import request" in main
    assert "await request.json(default={})" in main
    assert "from quart import request" not in main


def test_page_frontend_uses_astrbot_plugin_bridge() -> None:
    app = (ROOT / "pages" / "reality-companion" / "app.js").read_text(encoding="utf-8")
    assert "window.AstrBotPluginPage" in app
    assert 'bridge.apiGet(endpoint)' in app
    assert 'bridge.apiPost(endpoint, body)' in app
    assert 'const API = "/api/plug/astrbot_plugin_reality_companion/page"' in app
    assert 'const API = "/astrbot_plugin_reality_companion/page"' not in app


def test_page_supports_local_theme_and_glass_preferences() -> None:
    page = (ROOT / "pages" / "reality-companion" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "pages" / "reality-companion" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "pages" / "reality-companion" / "style.css").read_text(encoding="utf-8")
    assert 'id="theme-panel"' in page
    assert "data-theme-mode=\"system\"" in page
    assert "localStorage" in app
    assert "--glass-alpha" in app
    assert ':root[data-theme="dark"]' in style
    assert "backdrop-filter" in style


def test_page_keeps_system_default_audio_output_when_catalog_is_missing() -> None:
    app = (ROOT / "pages" / "reality-companion" / "app.js").read_text(encoding="utf-8")
    assert 'id: "system_default"' in app
    assert "function normalizedAudioDevices(audio)" in app
    assert "已保留系统默认输出" in app


def test_page_distinguishes_opencv_failure_from_plugin_backend_failure() -> None:
    app = (ROOT / "pages" / "reality-companion" / "app.js").read_text(encoding="utf-8")
    assert "OpenCV 加载异常" in app
    assert 'camera.backend?.error || "OpenCV 摄像头模块未加载"' in app


def test_private_command_can_show_and_rotate_mobile_pairing_token() -> None:
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '"配对令牌", "查看配对令牌", "输出配对令牌", "生成配对令牌"' in main
    assert '"重置配对令牌", "重新生成配对令牌", "刷新配对令牌"' in main
    assert "secrets.token_urlsafe(32)" in main
    assert "await self._stop_mobile_server()" in main
    assert "await self._start_mobile_server()" in main
