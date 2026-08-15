# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

from astrbot_plugin_reality_companion.main import RealityCompanionPlugin


def test_integration_status_exposes_runtime_takeover() -> None:
    plugin = RealityCompanionPlugin.__new__(RealityCompanionPlugin)
    plugin._mobile_cleanup_sessions = lambda: None
    plugin._private_companion_api = lambda: object()
    plugin.enable_experimental_bluetooth_wakeup = False
    plugin.enable_reality_touch_camera = False
    plugin.data = {"users": {}}
    plugin._reality_touch_audio_snapshot = lambda: {}
    plugin._reality_touch_camera_page_snapshot = lambda: {}
    plugin._mobile_enabled = lambda: False
    plugin._mobile_server_runner = None
    plugin._mobile_host = lambda: "127.0.0.1"
    plugin._mobile_server_bound_port = 0
    plugin._mobile_port = lambda: 6322
    plugin._mobile_pairing_token = lambda: ""
    plugin._mobile_session_ttl = lambda: 3600
    plugin._mobile_location_ttl = lambda: 900
    plugin._mobile_sessions = {}
    plugin._mobile_screen_upload_enabled = lambda: False

    status = plugin.integration_status()

    assert status["private_companion_linked"] is True
    assert status["managed_by_private_companion"] is True


def test_page_action_is_locked_during_runtime_takeover() -> None:
    async def run() -> None:
        plugin = RealityCompanionPlugin.__new__(RealityCompanionPlugin)
        plugin._private_companion_api = lambda: object()

        result = await plugin.page_action()

        assert result["ok"] is False
        assert result["status"] == "managed_by_private_companion"

    asyncio.run(run())
