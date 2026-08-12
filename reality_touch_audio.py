# -*- coding: utf-8 -*-
"""Reality Companion audio routing and proactive local playback."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .helpers import _now_ts, _safe_int, _single_line


class RealityTouchAudioMixin:
    _REALITY_TOUCH_DEFAULT_DEVICE_ID = "system_default"
    _REALITY_TOUCH_DEFAULT_VOLUME = 35
    _REALITY_TOUCH_TEST_AUDIO_PATH = Path(__file__).resolve().parent / "assets" / "reality_touch_device_test.mp3"

    def _reality_touch_store(self) -> dict[str, Any]:
        data = getattr(self, "data", None)
        if not isinstance(data, dict):
            return {}
        store = data.get("reality_touch")
        if not isinstance(store, dict):
            store = {}
            data["reality_touch"] = store
        return store

    @staticmethod
    def _reality_touch_audio_module_error() -> str:
        try:
            import sounddevice  # noqa: F401
            import soundfile  # noqa: F401
        except Exception as exc:
            return _single_line(exc, 160) or exc.__class__.__name__
        return ""

    def _reality_touch_audio_devices(self) -> dict[str, Any]:
        default_row = {
            "id": self._REALITY_TOUCH_DEFAULT_DEVICE_ID,
            "name": "跟随系统默认输出",
            "host_api": "系统",
            "is_default": True,
            "max_output_channels": 0,
            "sample_rate": 0,
        }
        module_error = self._reality_touch_audio_module_error()
        if module_error:
            return {
                "backend_available": False,
                "backend": "system_default_only",
                "error": "缺少 sounddevice/soundfile，当前只能跟随系统默认输出",
                "devices": [default_row],
            }
        try:
            import sounddevice as sd

            raw_devices = list(sd.query_devices())
            raw_host_apis = list(sd.query_hostapis())
            default_pair = getattr(sd.default, "device", (-1, -1))
            default_output = int(default_pair[1]) if isinstance(default_pair, (list, tuple)) and len(default_pair) > 1 else -1
            rows = [default_row]
            seen_ids: dict[str, int] = {}
            for index, raw in enumerate(raw_devices):
                info = dict(raw)
                output_channels = _safe_int(info.get("max_output_channels"), 0, 0)
                if output_channels <= 0:
                    continue
                host_index = _safe_int(info.get("hostapi"), -1, -1)
                host_name = ""
                if 0 <= host_index < len(raw_host_apis):
                    host_name = _single_line(dict(raw_host_apis[host_index]).get("name"), 80)
                name = _single_line(info.get("name"), 160) or f"音频输出 {index}"
                fingerprint = hashlib.sha256(
                    f"{host_name}|{name}|{output_channels}|{info.get('default_samplerate', '')}".encode("utf-8")
                ).hexdigest()[:16]
                occurrence = seen_ids.get(fingerprint, 0)
                seen_ids[fingerprint] = occurrence + 1
                device_id = f"sd:{fingerprint}" + (f":{occurrence}" if occurrence else "")
                rows.append(
                    {
                        "id": device_id,
                        "name": name,
                        "host_api": host_name or "系统音频",
                        "is_default": index == default_output,
                        "max_output_channels": output_channels,
                        "sample_rate": round(float(info.get("default_samplerate") or 0)),
                        "runtime_index": index,
                    }
                )
            return {
                "backend_available": True,
                "backend": "sounddevice",
                "error": "",
                "devices": rows,
            }
        except Exception as exc:
            logger.warning("[PrivateCompanion] 枚举现实触及音频设备失败: %s", _single_line(exc, 160))
            return {
                "backend_available": False,
                "backend": "system_default_only",
                "error": f"音频设备枚举失败：{_single_line(exc, 120)}",
                "devices": [default_row],
            }

    def _reality_touch_audio_snapshot(self) -> dict[str, Any]:
        catalog = self._reality_touch_audio_devices()
        store = self._reality_touch_store()
        selected_id = _single_line(
            store.get("audio_output_device_id"),
            96,
        ) or self._REALITY_TOUCH_DEFAULT_DEVICE_ID
        devices = catalog.get("devices") if isinstance(catalog.get("devices"), list) else []
        selected = next((item for item in devices if item.get("id") == selected_id), None)
        missing = selected is None and selected_id != self._REALITY_TOUCH_DEFAULT_DEVICE_ID
        if selected is None:
            selected = devices[0] if devices else {
                "id": self._REALITY_TOUCH_DEFAULT_DEVICE_ID,
                "name": "跟随系统默认输出",
            }
        return {
            **catalog,
            "mode": "selected_device" if selected.get("id") != self._REALITY_TOUCH_DEFAULT_DEVICE_ID else "system_default",
            "label": _single_line(selected.get("name"), 160) or "跟随系统默认输出",
            "selected_device_id": selected_id,
            "selected_device_name": _single_line(selected.get("name"), 160),
            "selected_device_missing": missing,
            "playback_volume": self._reality_touch_playback_volume(),
            "automatic_fallback": True,
            "last_playback": dict(store.get("last_playback")) if isinstance(store.get("last_playback"), dict) else {},
            "device_management": bool(catalog.get("backend_available")),
            "camera_granted": False,
        }

    def _reality_touch_playback_volume(self, override: Any = None) -> int:
        store = self._reality_touch_store()
        value = override if override is not None else store.get("playback_volume")
        if value is None:
            value = getattr(self, "tts_local_playback_volume", self._REALITY_TOUCH_DEFAULT_VOLUME)
        return _safe_int(value, self._REALITY_TOUCH_DEFAULT_VOLUME, 0, 100)

    def _reality_touch_select_audio_device(
        self,
        device_id: str,
        playback_volume: Any = None,
    ) -> dict[str, Any]:
        requested = _single_line(device_id, 96) or self._REALITY_TOUCH_DEFAULT_DEVICE_ID
        catalog = self._reality_touch_audio_devices()
        devices = catalog.get("devices") if isinstance(catalog.get("devices"), list) else []
        selected = next((item for item in devices if item.get("id") == requested), None)
        if selected is None:
            raise ValueError("没有找到所选音频输出设备，请刷新设备列表")
        if requested != self._REALITY_TOUCH_DEFAULT_DEVICE_ID and not catalog.get("backend_available"):
            raise ValueError("当前音频后端不可用，暂时只能跟随系统默认输出")
        store = self._reality_touch_store()
        store["audio_output_device_id"] = requested
        store["audio_output_device_name"] = _single_line(selected.get("name"), 160)
        if playback_volume is not None:
            store["playback_volume"] = self._reality_touch_playback_volume(playback_volume)
        store["audio_output_updated_at"] = _now_ts()
        return selected

    def _reality_touch_resolve_audio_device(self, device_id: str = "") -> dict[str, Any]:
        selected_id = _single_line(device_id, 96) or _single_line(
            self._reality_touch_store().get("audio_output_device_id"),
            96,
        ) or self._REALITY_TOUCH_DEFAULT_DEVICE_ID
        catalog = self._reality_touch_audio_devices()
        devices = catalog.get("devices") if isinstance(catalog.get("devices"), list) else []
        selected = next((item for item in devices if item.get("id") == selected_id), None)
        if selected is None:
            default = next(
                (item for item in devices if item.get("id") == self._REALITY_TOUCH_DEFAULT_DEVICE_ID),
                None,
            )
            if default is None:
                raise RuntimeError("已选择的音频输出设备当前不可用，且系统默认输出不可用")
            selected = dict(default)
            selected["fallback_from"] = selected_id
            logger.warning("[PrivateCompanion] 所选现实触及设备离线，回退系统默认输出: device=%s", selected_id)
        return selected

    def _record_reality_touch_playback(
        self,
        *,
        source: str,
        success: bool,
        volume: Any = None,
        route: dict[str, Any] | None = None,
        error: Any = None,
    ) -> None:
        store = self._reality_touch_store()
        route = route if isinstance(route, dict) else {}
        store["last_playback"] = {
            "source": _single_line(source, 40) or "reality_touch",
            "success": bool(success),
            "at": _now_ts(),
            "volume": self._reality_touch_playback_volume(volume),
            "device_id": _single_line(route.get("id"), 96),
            "device_name": _single_line(route.get("name"), 160),
            "fallback_from": _single_line(route.get("fallback_from"), 96),
            "error": _single_line(error, 200),
        }
        saver = getattr(self, "_schedule_data_save", None)
        if callable(saver):
            saver(delay=0.2)

    @staticmethod
    def _resample_reality_touch_audio(frames: Any, source_rate: int, target_rate: int) -> Any:
        import numpy as np

        samples = np.asarray(frames, dtype=np.float32)
        if source_rate <= 0 or target_rate <= 0 or source_rate == target_rate or len(samples) < 2:
            return samples
        target_count = max(1, round(len(samples) * target_rate / source_rate))
        source_positions = np.arange(len(samples), dtype=np.float64)
        target_positions = np.arange(target_count, dtype=np.float64) * source_rate / target_rate
        target_positions = np.minimum(target_positions, len(samples) - 1)
        if samples.ndim == 1:
            return np.interp(target_positions, source_positions, samples).astype(np.float32)
        channels = [
            np.interp(target_positions, source_positions, samples[:, index])
            for index in range(samples.shape[1])
        ]
        return np.column_stack(channels).astype(np.float32)

    def _open_reality_touch_audio_file(
        self,
        audio_path: str,
        *,
        device_id: str = "",
        volume: Any = None,
        fade_in_ms: Any = 0,
    ) -> dict[str, Any]:
        path = str(audio_path or "").strip()
        if not path or not Path(path).is_file():
            raise RuntimeError("待播放音频文件不存在")
        selected = self._reality_touch_resolve_audio_device(device_id)
        if selected.get("id") == self._REALITY_TOUCH_DEFAULT_DEVICE_ID:
            fallback = getattr(self, "_open_tts_audio_file_local", None)
            if not callable(fallback):
                raise RuntimeError("当前插件实例没有系统默认音频播放能力")
            playback_volume = self._reality_touch_playback_volume(volume)
            try:
                fallback(
                    path,
                    volume=playback_volume,
                    fade_in_ms=_safe_int(fade_in_ms, 0, 0, 5000),
                )
            except TypeError:
                # Keep compatibility with older host mixins exposing the old signature.
                try:
                    fallback(path, volume=playback_volume)
                except TypeError:
                    fallback(path)
            return selected

        try:
            import sounddevice as sd
            import soundfile as sf
        except Exception as exc:
            raise RuntimeError("指定设备播放需要 sounddevice 和 soundfile") from exc

        playback_gain = self._reality_touch_playback_volume(volume) / 100.0
        fade_ms = _safe_int(fade_in_ms, 0, 0, 5000)
        max_channels = max(1, _safe_int(selected.get("max_output_channels"), 2, 1))
        try:
            with sf.SoundFile(path) as audio:
                channels = max(1, min(int(audio.channels), max_channels))
                source_rate = max(1, int(audio.samplerate))
                target_rate = max(1, _safe_int(selected.get("sample_rate"), source_rate, 1))
                fade_samples = round(target_rate * fade_ms / 1000)
                written = 0

                def scaled(block: Any) -> Any:
                    nonlocal written
                    import numpy as np

                    gain = playback_gain
                    if fade_samples > 0 and written < fade_samples:
                        positions = np.arange(written, written + len(block), dtype=np.float32)
                        ramp = np.minimum(1.0, positions / max(1, fade_samples)).reshape(-1, 1)
                        written += len(block)
                        return block * ramp * gain
                    written += len(block)
                    return block * gain

                with sd.OutputStream(
                    device=int(selected.get("runtime_index")),
                    samplerate=target_rate,
                    channels=channels,
                    dtype="float32",
                ) as stream:
                    if target_rate != source_rate:
                        frames = audio.read(dtype="float32", always_2d=True)
                        if frames.shape[1] > channels:
                            frames = frames.mean(axis=1, keepdims=True) if channels == 1 else frames[:, :channels]
                        frames = self._resample_reality_touch_audio(frames, source_rate, target_rate)
                        for start in range(0, len(frames), 4096):
                            stream.write(scaled(frames[start:start + 4096]))
                        return selected
                    for block in audio.blocks(blocksize=4096, dtype="float32", always_2d=True):
                        if block.shape[1] > channels:
                            block = block.mean(axis=1, keepdims=True) if channels == 1 else block[:, :channels]
                        stream.write(scaled(block))
            return selected
        except Exception as exc:
            fallback = getattr(self, "_open_tts_audio_file_local", None)
            if not callable(fallback):
                raise
            logger.warning(
                "[PrivateCompanion] 指定音频设备播放失败，回退系统默认输出: device=%s error=%s",
                _single_line(selected.get("name"), 120),
                _single_line(exc, 160),
            )
            playback_volume = self._reality_touch_playback_volume(volume)
            try:
                fallback(path, volume=playback_volume, fade_in_ms=fade_ms)
            except TypeError:
                fallback(path, volume=playback_volume)
            route = {
                "id": self._REALITY_TOUCH_DEFAULT_DEVICE_ID,
                "name": "跟随系统默认输出",
                "fallback_from": _single_line(selected.get("id"), 96),
            }
            return route

    def _reality_touch_policy(self, user: dict[str, Any]) -> dict[str, Any]:
        policy = user.get("reality_touch_policy")
        if not isinstance(policy, dict):
            policy = {}
            user["reality_touch_policy"] = policy
        return policy

    def _reality_touch_proactive_voice_allowed(self, user: dict[str, Any]) -> bool:
        consented = getattr(self, "_reality_touch_audio_consented", lambda _: False)(user)
        policy = self._reality_touch_policy(user)
        return bool(
            getattr(self, "enable_experimental_bluetooth_wakeup", False)
            and consented
            and policy.get("proactive_voice_enabled")
        )

    def _reality_touch_update_policy(self, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(payload.get("proactive_voice_enabled"))
        consented = getattr(self, "_reality_touch_audio_consented", lambda _: False)(user)
        if enabled and not consented:
            raise ValueError("该用户尚未在私聊中完成现实触及知情确认")
        policy = self._reality_touch_policy(user)
        policy["proactive_voice_enabled"] = enabled
        policy["playback_volume"] = self._reality_touch_playback_volume(payload.get("playback_volume"))
        policy["updated_at"] = _now_ts()
        return policy

    async def _play_reality_touch_text(
        self,
        text: str,
        *,
        repeat: int = 1,
        interval: int = 20,
        volume: Any = None,
        fade_in_ms: Any = 0,
        source: str = "reality_touch",
    ) -> bool:
        synthesizer = getattr(self, "_synthesize_realtime_voice", None)
        if not callable(synthesizer):
            logger.warning("[PrivateCompanion] 现实触及缺少 TTS 合成能力")
            self._record_reality_touch_playback(source=source, success=False, volume=volume, error="缺少 TTS 合成能力")
            return False
        route: dict[str, Any] = {}
        try:
            result = await synthesizer(text, source="reality_touch", play_local=False)
            audio_path = str(result.get("audio_path") or "") if isinstance(result, dict) else ""
            if not audio_path:
                self._record_reality_touch_playback(source=source, success=False, volume=volume, error="TTS 未返回音频文件")
                return False
            for index in range(max(1, min(6, int(repeat)))):
                route = await asyncio.to_thread(
                    self._open_reality_touch_audio_file,
                    audio_path,
                    volume=volume,
                    fade_in_ms=fade_in_ms,
                )
                if index + 1 < repeat:
                    await asyncio.sleep(max(5, min(300, int(interval))))
            self._record_reality_touch_playback(source=source, success=True, volume=volume, route=route)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[PrivateCompanion] 现实触及音频播放失败: %s", _single_line(exc, 160))
            self._record_reality_touch_playback(source=source, success=False, volume=volume, route=route, error=exc)
            return False

    async def _play_reality_touch_test_audio(self, volume: Any = None) -> bool:
        path = self._REALITY_TOUCH_TEST_AUDIO_PATH
        if not path.is_file():
            logger.warning("[PrivateCompanion] 现实触及固定测试音频不存在: %s", path.name)
            return False
        try:
            route = await asyncio.to_thread(self._open_reality_touch_audio_file, str(path), volume=volume)
            self._record_reality_touch_playback(source="device_test", success=True, volume=volume, route=route)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[PrivateCompanion] 现实触及固定测试音频播放失败: %s", _single_line(exc, 160))
            self._record_reality_touch_playback(source="device_test", success=False, volume=volume, error=exc)
            return False

    async def _mirror_reality_touch_proactive_voice(
        self,
        user: dict[str, Any],
        audio_path: str,
    ) -> bool:
        if not self._reality_touch_proactive_voice_allowed(user):
            return False
        path = str(audio_path or "").strip()
        if not path or not Path(path).is_file():
            logger.warning("[PrivateCompanion] 主动语音现实触及缺少本地音频文件")
            return False
        volume: Any = None
        try:
            policy = self._reality_touch_policy(user)
            volume = policy.get("playback_volume")
            route = await asyncio.to_thread(self._open_reality_touch_audio_file, path, volume=volume)
            self._record_reality_touch_playback(source="proactive_voice", success=True, volume=volume, route=route)
            return True
        except Exception as exc:
            logger.warning("[PrivateCompanion] 主动语音同步到现实设备失败: %s", _single_line(exc, 160))
            self._record_reality_touch_playback(source="proactive_voice", success=False, volume=volume, error=exc)
            return False
