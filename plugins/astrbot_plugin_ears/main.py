"""耳朵：录一段系统正在播放的声音（WASAPI 回环），识别歌曲或转写成文字。

- 音乐：Shazam 识别「歌名 - 歌手」
- 人声/视频：faster-whisper 本地转写（模型缺失时跳过）

只录系统输出，不碰麦克风。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import wave

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_SAMPLE_WAV = os.path.join(tempfile.gettempdir(), "astrbot_ears.wav")


def _record(seconds: int = 8) -> str:
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        device = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not device.get("isLoopbackDevice"):
            for loopback in p.get_loopback_device_info_generator():
                if device["name"] in loopback["name"]:
                    device = loopback
                    break
        rate = int(device["defaultSampleRate"])
        channels = min(2, device["maxInputChannels"])
        chunk = 1024
        frames = []
        stream = p.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            frames_per_buffer=chunk,
            input=True,
            input_device_index=device["index"],
        )
        for _ in range(int(rate / chunk * seconds)):
            frames.append(stream.read(chunk, exception_on_overflow=False))
        stream.stop_stream()
        stream.close()
        with wave.open(_SAMPLE_WAV, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
            wf.setframerate(rate)
            wf.writeframes(b"".join(frames))
        return _SAMPLE_WAV
    finally:
        p.terminate()


def _shazam(path: str) -> str | None:
    try:
        from shazamio import Shazam

        async def _run():
            return await Shazam().recognize(path)

        out = asyncio.run(_run())
        track = (out or {}).get("track") or {}
        title = track.get("title")
        artist = track.get("subtitle")
        if title:
            return f"{title} - {artist}" if artist else str(title)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Shazam 识别失败: {exc}")
    return None


_whisper_model = None


def _transcribe(path: str) -> str | None:
    global _whisper_model
    try:
        from faster_whisper import WhisperModel

        if _whisper_model is None:
            _whisper_model = WhisperModel(
                "tiny",
                device="cpu",
                compute_type="int8",
                download_root=os.path.join(tempfile.gettempdir(), "astrbot_whisper"),
            )
        segments, _info = _whisper_model.transcribe(path, language="zh", vad_filter=True)
        text = "".join(seg.text for seg in segments).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Whisper 转写失败: {exc}")
        return None


def _listen(seconds: int) -> str:
    path = _record(seconds)
    song = _shazam(path)
    if song:
        return f"电脑正在播放音乐：{song}"

    text = _transcribe(path)
    if text:
        return f"电脑里传来的声音（转写）：{text}"

    return "录到了声音，但没识别出歌名，也没有清晰的人声。"


@register(
    "ears",
    "dafeiyu",
    "录一段电脑正在播放的声音，识别歌曲或转写成文字",
    "1.0.0",
)
class Ears(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}

    @llm_tool(name="listen_to_pc")
    async def listen_to_pc(self, event: AstrMessageEvent) -> str:
        """听一段用户电脑正在播放的声音（音乐 / 视频），默认录 8 秒。

        当用户说「你在听什么」「我放的这首歌你听到了吗」「听听这个」
        「你听得见吗」这类需要知道电脑正在播放什么的时候调用。
        """
        try:
            return await asyncio.to_thread(_listen, 8)
        except Exception as exc:  # noqa: BLE001
            logger.exception("听声音失败")
            return f"听不到：{exc}"

    @filter.command("听")
    async def listen_command(self, event: AstrMessageEvent):
        """发 /听，录 8 秒电脑声音并告诉你听到了什么。"""
        try:
            result = await asyncio.to_thread(_listen, 8)
        except Exception as exc:  # noqa: BLE001
            result = f"听不到：{exc}"
        yield event.plain_result(result)
