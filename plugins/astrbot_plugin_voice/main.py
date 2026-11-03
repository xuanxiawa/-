"""风铃的嗓子：GPT-SoVITS 克隆音色（才羽桃井）+ 心情驱动的发语音策略。

移植来源：Lbcheng1145/gpt-sovits-voice-plugin（CGl123 原版的优化版），
源码留在 D:\\大肥鱼\\插件\\gpt-sovits\\vendor\\gpt-sovits-voice-plugin 供对照。

和第一版最大的区别：**不再掷骰子**。发不发语音完全看 heart 插件算出来的心情，
声音状态（语速）也随心情走：

  | 心情 | 声音 |
  |---|---|
  | 兴奋 | 必发语音，语速 1.08；冷却到点会「突然唱首雨爱」 |
  | 开心 | 必发语音，语速 1.03 |
  | 黏人 | 必发语音，语速 0.97（撒娇） |
  | 委屈 / 有点难过 / 闷闷的 | 发语音，语速 0.88~0.92（气短、软） |
  | 无聊 | 偶尔一句，语速 0.95 |
  | 平静 | 不发 |
  | 生气 | 不发（冷处理） |
  | 困 | 不发——太累了不想说话 |

心情从 heart 插件的 state.json 读（同一个文件，heart 写、这里读，不重复算）。
另外每会话有最短间隔，避免连着几条都是语音。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import re
import struct
import tempfile
import time
import urllib.parse
import wave
from array import array

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, File, Image, Json, Plain, Record, Video
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# ══════════════════════════════════════════════════════════════════════
# 文本工具
# ══════════════════════════════════════════════════════════════════════

_KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u30fc]")
_KOREAN = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
_HAN = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-zA-Z]")
_YUE_CHARS = frozenset("唔咁咗哋佢乜嘢咩嘅嗰嚟攰冇睇喺啱㗎冚嬲啲嗮嘞喎囉係傾俾搵揾掟攞瞓")
_SCRIPT = {
    "ru": re.compile(r"[\u0400-\u04ff]"),
    "th": re.compile(r"[\u0e00-\u0e7f]"),
    "ar": re.compile(r"[\u0600-\u06ff]"),
    "hi": re.compile(r"[\u0900-\u097f]"),
    "el": re.compile(r"[\u0370-\u03ff]"),
}

# 零宽字符 / BOM / 方向标记 / 控制符：GPT-SoVITS 的音素化会把这些拿去 GBK 编码，直接 400
_INVISIBLE = re.compile(
    r"[\u200b\u200c\u200e\u200f\u202a-\u202e\ufeff\u00ad\u2060-\u206f"
    r"\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]"
)
_SPACES = re.compile(r"[ \t\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+")
_SENTENCE_END = frozenset("。！？!?…,.、；;:：~～）)】」』》〉\"'”’")

_SKIP = tuple(
    re.compile(p, re.MULTILINE)
    for p in (
        r"```",
        r"https?://\S+",
        r"\|\s*[-\w]+\s*\|",
        r"^\s*[-*+]\s+\S",
        r"^\s*\d+\.\s+\S",
        r"</?[a-zA-Z][^>]*>",
        r"\[[^\]]*\]\([^)]*\)",
    )
)

SUPPORTED_LANGS = {"zh", "en", "ja", "yue"}
MIN_AUDIO_SECONDS = 0.5
MIN_AUDIO_RMS = 100.0
MAX_AUDIO_BYTES = 32 * 1024 * 1024

_BLOCKING = (Image, Record, File, Video, Json)
_TEMP_DIR = os.path.join(tempfile.gettempdir(), "fengling_voice")

# 念唱曲库的兜底（配置里 song.songs 为空时用这套）
DEFAULT_SONGS: tuple[dict, ...] = (
    {"title": "雨爱（司凤版）", "lyrics": "邦吧邦吧邦吧～ 雨爱～ 帮吧帮吧帮吧～"},
    {"title": "学猫叫", "lyrics": "我们一起学猫叫，一起喵喵喵喵喵～"},
    {"title": "虫儿飞", "lyrics": "黑黑的天空低垂，亮亮的繁星相随～"},
    {"title": "小星星", "lyrics": "一闪一闪亮晶晶，满天都是小星星～"},
)

# ── 学歌（网易云公开接口，不需要 cookie）─────────────────────────────
_NETEASE = "https://music.163.com"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
# 热门榜单：热歌榜 / 飙升榜。学歌时随机挑一个榜的前 50 首
_CHARTS = (("热歌榜", 3778678), ("飙升榜", 19723756))
_LRC_TAG = re.compile(r"\[[^\]]{0,24}\]")
# 制作名单行，不能当歌词念
_CREDIT = re.compile(
    r"(作词|作\s*词|作曲|编曲|制作人|监制|出品|录音|混音|母带|和声|吉他|贝斯|鼓|键盘|弦乐|"
    r"统筹|企划|发行|词[:：]|曲[:：]|OP[:：]|SP[:：]|音乐总监|人声编辑)"
)
_BAD_WORDS = ("毒品", "自杀", "强奸", "嫖", "裸聊", "约炮", "吸毒")


def _pick_hot_track(tracks: list[dict], known_titles: set[str]) -> dict | None:
    """从榜单里挑一首没学过的（前 50 首里随机，避免总是那几首）。"""
    fresh = [t for t in tracks[:50] if t.get("title") and t["title"] not in known_titles]
    return random.choice(fresh) if fresh else None


def clean_lyric(lrc: str, max_chars: int = 40) -> str:
    """把 LRC 洗成两句能念的歌词：去时间轴、去制作名单、去重，取中后段（一般是副歌）。"""
    lines: list[str] = []
    seen: set[str] = set()
    for raw in (lrc or "").splitlines():
        line = _LRC_TAG.sub("", raw).strip().strip("　")
        if not line or line == "//":
            continue
        if _CREDIT.search(line):
            continue
        if any(word in line for word in _BAD_WORDS):
            continue
        if not _HAN.search(line) and not _LATIN.search(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line[:30])
    if len(lines) < 2:
        return ""
    start = max(0, int(len(lines) * 0.55) - 1)
    picked = [x.rstrip("，。！？、；：~～ ").strip()[:16] for x in lines[start : start + 2]]
    picked = [x for x in picked if x]
    if len(picked) < 2:
        return ""
    return ("～".join(picked))[:max_chars]


class SongLibrary:
    """学来的歌单独存一个 JSON，不往插件配置里写（配置是 dashboard 管的）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._data: dict = {"learned": [], "last_learn": 0.0}
        self._mtime = 0.0

    def _dir(self) -> str:
        return os.path.dirname(self.path) or "."

    def load(self) -> dict:
        try:
            mtime = os.path.getmtime(self.path)
            if mtime == self._mtime:
                return self._data
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._data = {
                    "learned": [x for x in data.get("learned", []) if isinstance(x, dict)],
                    "last_learn": float(data.get("last_learn") or 0.0),
                }
            self._mtime = mtime
        except (OSError, ValueError):
            pass
        return self._data

    def save(self) -> None:
        try:
            os.makedirs(self._dir(), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._mtime = os.path.getmtime(self.path)
        except OSError as exc:
            logger.warning(f"[voice] 歌库写入失败：{exc}")

    def add(self, title: str, lyrics: str, song_id: int) -> bool:
        data = self.load()
        if any(x.get("title") == title for x in data["learned"]):
            return False
        data["learned"].append(
            {"title": title, "lyrics": lyrics, "song_id": song_id, "ts": time.time()}
        )
        data["last_learn"] = time.time()
        self.save()
        return True

    @property
    def last_learn(self) -> float:
        return float(self.load().get("last_learn") or 0.0)

    def songs(self) -> list[dict]:
        return [
            {"title": str(x.get("title") or ""), "lyrics": str(x.get("lyrics") or "")}
            for x in self.load().get("learned", [])
            if x.get("lyrics")
        ]


def clean_text(text: str) -> str:
    if not text:
        return ""
    out = _INVISIBLE.sub("", text)
    out = _SPACES.sub(" ", out)
    return out.strip()


def ensure_sentence_end(text: str) -> str:
    s = (text or "").rstrip()
    if not s:
        return ""
    if s[-1] in _SENTENCE_END:
        return s
    return s + "。"


def detect_language(text: str, fallback: str = "zh") -> str:
    s = text or ""
    if _KANA.search(s):
        return "ja"
    if _KOREAN.search(s):
        return "ko"
    if any(ch in _YUE_CHARS for ch in s):
        return "yue"
    for lang, pat in _SCRIPT.items():
        if pat.search(s):
            return lang
    han = len(_HAN.findall(s))
    latin = len(_LATIN.findall(s))
    if han == 0 and latin > 0:
        return "en"
    if han > 0 and han / max(1, han + latin) > 0.3:
        return "zh"
    if latin > 0:
        return "en"
    return fallback


def is_readable_as_zh(text: str) -> bool:
    s = text or ""
    if _KOREAN.search(s):
        return False
    return not any(pat.search(s) for pat in _SCRIPT.values())


def text_is_speakable(text: str, min_chars: int, max_chars: int) -> tuple[bool, str]:
    content = (text or "").strip()
    if not content:
        return False, "空文本"
    effective = len(re.sub(r"\s+", "", content))
    if effective < min_chars:
        return False, f"太短（{effective} < {min_chars}）"
    if effective > max_chars:
        return False, f"太长（{effective} > {max_chars}）"
    for pat in _SKIP:
        if pat.search(content):
            return False, f"命中不念规则 {pat.pattern!r}"
    return True, "ok"


_ONSET_RATIO = 0.15
_LEAD_PAD_SECONDS = 0.5
_LEAD_FADE_SECONDS = 0.003
_MAX_LEAD_TRIM_SECONDS = 1.5


def trim_leading_silence(data: bytes) -> bytes:
    """裁掉开头 80~140ms 前导静音和紧随的弱起音，再补 500ms 静音防吞字。"""
    if not data:
        return data
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            params = source.getparams()
            raw = source.readframes(source.getnframes())
    except (wave.Error, EOFError, struct.error):
        return data

    width = params.sampwidth
    channels = params.nchannels or 1
    rate = params.framerate or 1
    if not raw or width not in (2, 4):
        return data

    samples = array("h" if width == 2 else "i")
    samples.frombytes(raw[: len(raw) // width * width])
    if not samples:
        return data

    peak = max(abs(v) for v in samples) or 1
    threshold = peak * _ONSET_RATIO
    frame_count = len(samples) // channels
    onset = frame_count
    for index in range(frame_count):
        if abs(samples[index * channels]) > threshold:
            onset = index
            break

    if onset >= frame_count or onset / rate > _MAX_LEAD_TRIM_SECONDS:
        return data

    body = array(samples.typecode, samples[onset * channels :])
    fade = min(int(rate * _LEAD_FADE_SECONDS), len(body) // channels)
    for index in range(fade):
        gain = index / fade if fade else 1.0
        base = index * channels
        for channel in range(channels):
            body[base + channel] = int(body[base + channel] * gain)

    head = array(samples.typecode, [0] * (int(rate * _LEAD_PAD_SECONDS) * channels))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(params.nchannels)
        target.setsampwidth(params.sampwidth)
        target.setframerate(params.framerate)
        target.writeframes((head + body).tobytes())
    return buffer.getvalue()


def is_valid_audio(data: bytes) -> tuple[bool, str]:
    if not data or len(data) < 100:
        return False, f"数据过短（{len(data or b'')} 字节）"
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            frames = source.getnframes()
            rate = source.getframerate() or 24000
            channels = source.getnchannels() or 1
            width = source.getsampwidth()
            raw = source.readframes(frames)
    except (wave.Error, EOFError, struct.error) as exc:
        return False, f"不是合法 wav：{exc}"

    seconds = frames / rate if rate else 0
    if seconds < MIN_AUDIO_SECONDS:
        return False, f"时长过短（{seconds:.2f}s）"
    if width not in (2, 4):
        return True, "ok"

    samples = array("h" if width == 2 else "i")
    samples.frombytes(raw[: len(raw) // width * width])
    if not samples:
        return False, "读不出采样"
    step = max(1, channels)
    total = 0
    count = 0
    for index in range(0, len(samples), step):
        value = samples[index]
        total += value * value
        count += 1
    level = (total / count) ** 0.5 if count else 0.0
    if level < MIN_AUDIO_RMS:
        return False, f"几乎静音（RMS {level:.1f}）"
    return True, "ok"


# ══════════════════════════════════════════════════════════════════════
# 心情 → 声音策略
# ══════════════════════════════════════════════════════════════════════

# heart 插件的心情取值：兴奋/开心/平静/无聊/闷闷的/有点难过/委屈/生气/困/黏人
# voice: 这种心情下要不要出声；speed: 语速（>1 更快，<1 更黏）；cooldown: 同会话两条语音最短间隔
MOOD_POLICY: dict[str, dict] = {
    "兴奋": {"voice": True, "speed": 1.08, "cooldown": 0, "song": True},
    "开心": {"voice": True, "speed": 1.03, "cooldown": 0, "song": False},
    "黏人": {"voice": True, "speed": 0.97, "cooldown": 30, "song": False},
    "无聊": {"voice": True, "speed": 0.95, "cooldown": 600, "song": False},
    "闷闷的": {"voice": True, "speed": 0.92, "cooldown": 300, "song": False},
    "有点难过": {"voice": True, "speed": 0.90, "cooldown": 240, "song": False},
    "委屈": {"voice": True, "speed": 0.88, "cooldown": 180, "song": False},
    "平静": {"voice": False, "speed": 1.00, "cooldown": 0, "song": False},
    "生气": {"voice": False, "speed": 1.00, "cooldown": 0, "song": False},
    "困": {"voice": False, "speed": 1.00, "cooldown": 0, "song": False},
}
_UNKNOWN_POLICY = {"voice": False, "speed": 1.0, "cooldown": 0, "song": False}


class MoodState:
    """读 heart 的 state.json（只读，不写），带 mtime 缓存。"""

    def __init__(self) -> None:
        self.path = ""
        self._mtime = 0.0
        self._data: dict = {}
        self._warned = False

    def configure(self, path: str) -> None:
        if path != self.path:
            self.path = path
            self._mtime = 0.0
            self._data = {}

    def read(self) -> dict:
        if not self.path:
            return {}
        try:
            stat = os.stat(self.path)
        except OSError:
            if not self._warned:
                self._warned = True
                logger.warning(f"[voice] 读不到 heart 的心情文件：{self.path}")
            return {}
        if stat.st_mtime != self._mtime:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    self._data = json.load(fh)
                self._mtime = stat.st_mtime
            except (OSError, ValueError):
                return self._data
        return self._data


# ══════════════════════════════════════════════════════════════════════
# 合成后端
# ══════════════════════════════════════════════════════════════════════


class TTSError(RuntimeError):
    pass


class GSVEngine:
    """GPT-SoVITS api_v2（默认 http://127.0.0.1:9880）。"""

    def __init__(self, conf: dict) -> None:
        self.conf = conf or {}
        self.api_base = str(self.conf.get("api_base") or "http://127.0.0.1:9880").rstrip("/")
        self.timeout = float(self.conf.get("timeout") or 300)
        self._session: aiohttp.ClientSession | None = None
        self._gpt_loaded: str | None = None
        self._sovits_loaded: str | None = None
        self._lock = asyncio.Lock()

    def missing(self) -> list[str]:
        miss: list[str] = []
        ref = str(self.conf.get("refer_wav") or "").strip()
        if not ref:
            miss.append("gsv.refer_wav（参考音频）")
        if not str(self.conf.get("prompt_text") or "").strip():
            miss.append("gsv.prompt_text（参考音频里的原话，要逐字一致）")
        is_local = any(h in self.api_base for h in ("127.0.0.1", "localhost", "0.0.0.0"))
        if ref and is_local and not os.path.exists(ref):
            miss.append(f"gsv.refer_wav 不存在：{ref}")
        return miss

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _switch_weights(self, session: aiohttp.ClientSession) -> None:
        gpt = str(self.conf.get("gpt_weights") or "").strip()
        sovits = str(self.conf.get("sovits_weights") or "").strip()
        if gpt and gpt != self._gpt_loaded:
            url = f"{self.api_base}/set_gpt_weights?" + urllib.parse.urlencode({"weights_path": gpt})
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise TTSError(f"切 GPT 权重失败 {resp.status}")
            self._gpt_loaded = gpt
        if sovits and sovits != self._sovits_loaded:
            url = f"{self.api_base}/set_sovits_weights?" + urllib.parse.urlencode({"weights_path": sovits})
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise TTSError(f"切 SoVITS 权重失败 {resp.status}")
            self._sovits_loaded = sovits

    async def synthesize(self, text: str, speed: float | None = None) -> bytes:
        miss = self.missing()
        if miss:
            raise TTSError("语音还没配好：" + "；".join(miss))

        lang = str(self.conf.get("text_lang") or "auto").strip() or "auto"
        if lang == "auto":
            lang = detect_language(text)
        if lang not in SUPPORTED_LANGS:
            if is_readable_as_zh(text):
                logger.warning(f"[voice] 语言 {lang} 底模不支持，改用 zh 硬读")
                lang = "zh"
            else:
                raise TTSError(f"底模不支持语言 {lang}，中文也读不出来，跳过")

        payload = {
            "text": text,
            "text_lang": lang,
            "ref_audio_path": str(self.conf.get("refer_wav") or ""),
            "prompt_text": str(self.conf.get("prompt_text") or ""),
            "prompt_lang": str(self.conf.get("prompt_lang") or "zh"),
            "speed_factor": float(speed if speed is not None else (self.conf.get("speed") or 1.0)),
            "fragment_interval": float(self.conf.get("fragment_interval") or 0.0),
            "text_split_method": str(self.conf.get("split_method") or "cut0"),
            "min_chunk_length": int(self.conf.get("min_chunk_length") or 16),
            "overlap_length": int(self.conf.get("overlap_length") or 0),
            "top_k": int(self.conf.get("top_k") or 15),
            "top_p": float(self.conf.get("top_p") or 1.0),
            "temperature": float(self.conf.get("temperature") or 1.0),
            "media_type": "wav",
        }

        session = await self._get_session()
        async with self._lock:
            await self._switch_weights(session)
            logger.info(f"[voice] GPT-SoVITS 合成 lang={lang} speed={payload['speed_factor']} len={len(text)}")
            try:
                async with session.post(f"{self.api_base}/tts", json=payload) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:200]
                        raise TTSError(f"GPT-SoVITS 返回 {resp.status}: {body}")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > MAX_AUDIO_BYTES:
                            raise TTSError("音频响应过大，已中断")
                        chunks.append(chunk)
            except aiohttp.ClientConnectorError as exc:
                raise TTSError(f"连不上 GPT-SoVITS（{self.api_base}）：{exc}") from exc
            except asyncio.TimeoutError as exc:
                raise TTSError(f"GPT-SoVITS 合成超时（{self.timeout}s）") from exc

        data = b"".join(chunks)
        ok, detail = is_valid_audio(data)
        if not ok:
            raise TTSError(f"合成结果异常：{detail}")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, trim_leading_silence, data)


class EdgeEngine:
    """edge-tts：免费、快，但音色不是克隆的。留作兜底。"""

    def __init__(self, conf: dict) -> None:
        self.conf = conf or {}
        self.voice = str(self.conf.get("voice") or "zh-CN-XiaoyiNeural")
        self.rate = str(self.conf.get("rate") or "")
        self.volume = str(self.conf.get("volume") or "")
        self.pitch = str(self.conf.get("pitch") or "")
        self.ffmpeg = str(self.conf.get("ffmpeg") or "ffmpeg")

    async def close(self) -> None:
        return None

    async def synthesize(self, text: str, speed: float | None = None) -> bytes:
        import edge_tts

        kwargs: dict = {"voice": self.voice}
        rate = self.rate
        if speed is not None and abs(speed - 1.0) > 1e-3:
            rate = f"{(speed - 1.0) * 100:+.0f}%"
        if rate:
            kwargs["rate"] = rate
        if self.volume:
            kwargs["volume"] = self.volume
        if self.pitch:
            kwargs["pitch"] = self.pitch

        stamp = f"{os.getpid()}_{int(time.time() * 1000)}_{random.randint(0, 999)}"
        mp3 = os.path.join(tempfile.gettempdir(), f"fengling_edge_{stamp}.mp3")
        wav = mp3[:-4] + ".wav"
        try:
            await edge_tts.Communicate(text, **kwargs).save(mp3)
            proc = await asyncio.create_subprocess_exec(
                self.ffmpeg, "-y", "-i", mp3, "-ar", "24000", "-ac", "1", wav,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            code = await proc.wait()
            if code != 0 or not os.path.exists(wav):
                raise TTSError(f"ffmpeg 转码失败（exit {code}）")
            with open(wav, "rb") as fh:
                return fh.read()
        except TTSError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"edge-tts 合成失败：{exc}") from exc
        finally:
            for path in (mp3, wav):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass


# ══════════════════════════════════════════════════════════════════════
# 插件本体
# ══════════════════════════════════════════════════════════════════════


@register(
    "voice",
    "dafeiyu",
    "风铃的嗓子：才羽桃井音色克隆 + 心情驱动的语音（兴奋会突然唱雨爱，困了不出声）",
    "2.2.0",
)
class VoicePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._engine = None
        self._engine_sig = ""
        self._windows: dict[str, float] = {}
        self._last_voice: dict[str, float] = {}
        self._last_song: dict[str, float] = {}
        self._warned: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._synth_gate = asyncio.Lock()
        self._synth_count = 0
        self._fails = 0
        self._blocked_until = 0.0
        self._last_song_title = ""
        self._song_lib: SongLibrary | None = None
        self._learn_block_until = 0.0
        self._mood = MoodState()
        os.makedirs(_TEMP_DIR, exist_ok=True)

    # ── 配置 ────────────────────────────────────────────────────────
    def _sub(self, key: str) -> dict:
        value = self.config.get(key) if hasattr(self.config, "get") else None
        return value if isinstance(value, dict) else {}

    def _flag(self, section: str, key: str, default: bool) -> bool:
        value = self._sub(section).get(key, default)
        return bool(default if value is None else value)

    def _num(self, section: str, key: str, default: float) -> float:
        try:
            return float(self._sub(section).get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _str(self, section: str, key: str, default: str) -> str:
        value = self._sub(section).get(key, default)
        return str(default if value is None else value)

    # ── 引擎 ────────────────────────────────────────────────────────
    def _get_engine(self):
        conf = {
            "engine": self.config.get("engine", "gsv"),
            "gsv": self._sub("gsv"),
            "edge": self._sub("edge"),
        }
        sig = repr(sorted((k, repr(v)) for k, v in conf.items()))
        if self._engine is None or sig != self._engine_sig:
            if self._engine is not None:
                self._close_later(self._engine)
            kind = str(conf["engine"] or "gsv").strip().lower()
            self._engine = EdgeEngine(conf["edge"]) if kind == "edge" else GSVEngine(conf["gsv"])
            self._engine_sig = sig
            logger.info(f"[voice] 合成后端：{type(self._engine).__name__}")
        return self._engine

    def _close_later(self, engine) -> None:
        try:
            asyncio.get_running_loop().create_task(engine.close())
        except RuntimeError:
            pass

    async def terminate(self) -> None:
        if self._engine is not None:
            await self._engine.close()
            self._engine = None

    # ── 心情 ────────────────────────────────────────────────────────
    def _mood_data(self) -> dict:
        self._mood.configure(self._str("mood", "state_file", r"D:\AstrBot\bot\data\plugin_data\heart\state.json"))
        return self._mood.read()

    def _policy(self) -> tuple[str, dict, dict]:
        data = self._mood_data()
        mood = str(data.get("mood") or "").strip() or "平静"
        policy = MOOD_POLICY.get(mood, _UNKNOWN_POLICY)
        return mood, policy, data

    # ── 合成 + 落盘 ─────────────────────────────────────────────────
    async def _synthesize(
        self, text: str, speed: float | None = None, timeout: float | None = None
    ) -> str:
        engine = self._get_engine()
        if isinstance(engine, GSVEngine):
            miss = engine.missing()
            if miss:
                if "missing" not in self._warned:
                    self._warned.add("missing")
                    logger.warning("[voice] " + "；".join(miss))
                raise TTSError("语音还没配好：" + "；".join(miss))

        payload = ensure_sentence_end(clean_text(text))
        limit = max(5.0, float(timeout or self._num("mood", "synth_timeout", 60)))
        try:
            audio = await asyncio.wait_for(engine.synthesize(payload, speed=speed), timeout=limit)
        except asyncio.TimeoutError as exc:
            raise TTSError(f"合成超过 {limit:.0f} 秒，放弃") from exc

        path = os.path.join(
            _TEMP_DIR, f"fy_{int(time.time() * 1000)}_{random.randint(0, 999)}.wav"
        )
        with open(path, "wb") as fh:
            fh.write(audio)
        self._synth_count += 1
        if self._synth_count % 50 == 0:
            self._prune_temp()
        return path

    def _prune_temp(self, keep_seconds: float = 86400) -> None:
        """清掉一天前的合成残留（唱歌的音频不走临时文件追踪，只能自己收拾）。"""
        cutoff = time.time() - keep_seconds
        try:
            for name in os.listdir(_TEMP_DIR):
                if not name.startswith("fy_"):
                    continue
                full = os.path.join(_TEMP_DIR, name)
                try:
                    if os.path.getmtime(full) < cutoff:
                        os.remove(full)
                except OSError:
                    pass
        except OSError:
            pass

    async def _record(
        self,
        text: str,
        event: AstrMessageEvent,
        speed: float | None = None,
        track: bool = True,
        timeout: float | None = None,
    ) -> Record | None:
        try:
            path = await self._synthesize(text, speed=speed, timeout=timeout)
        except TTSError as exc:
            self._note_failure()
            logger.warning(f"[voice] 合成失败，保留文字：{exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            self._note_failure()
            logger.error(f"[voice] 合成异常，保留文字：{exc}", exc_info=True)
            return None
        self._fails = 0
        if track:
            try:
                event.track_temporary_local_file(path)
            except Exception:  # noqa: BLE001
                pass
        return Record.fromFileSystem(path)

    def _note_failure(self) -> None:
        """连续失败就冷静 5 分钟，别让每条消息都卡在合成上。"""
        self._fails += 1
        if self._fails >= 3:
            self._blocked_until = time.time() + 300
            self._fails = 0
            logger.warning("[voice] 连续 3 次合成失败，5 分钟内只用文字")

    # ── 对讲窗口 ────────────────────────────────────────────────────
    def _hold_seconds(self) -> float:
        return max(10.0, self._num("intercom", "hold_seconds", 180))

    def _open_window(self, event: AstrMessageEvent) -> None:
        key = event.unified_msg_origin
        self._windows[key] = time.time() + self._hold_seconds()
        logger.info(f"[voice] 对讲窗口开启 {self._hold_seconds():.0f}s：{key}")

    def _window_active(self, event: AstrMessageEvent) -> bool:
        if not self._flag("intercom", "enable", True):
            return False
        key = event.unified_msg_origin
        expire = self._windows.get(key)
        if not expire:
            return False
        if time.time() >= expire:
            self._windows.pop(key, None)
            return False
        return True

    def _refresh_window(self, event: AstrMessageEvent) -> None:
        if not self._flag("intercom", "enable", True):
            return
        key = event.unified_msg_origin
        if key in self._windows:
            self._windows[key] = time.time() + self._hold_seconds()

    # ── 钩子 1：收到语音 → 开窗 ─────────────────────────────────────
    @filter.on_llm_request(priority=50)
    async def _on_inbound(self, event: AstrMessageEvent, req) -> None:
        try:
            if not self._flag("intercom", "enable", True):
                return
            messages = event.get_messages() or []
            if not any(isinstance(m, Record) for m in messages):
                return
            if self._flag("intercom", "private_only", True) and not event.is_private_chat():
                return
            text = (event.message_str or "").strip()
            min_chars = int(self._num("intercom", "min_chars", 1))
            if len(text) < max(1, min_chars):
                logger.debug("[voice] 收到语音但转写太短，不开窗")
                return
            self._open_window(event)
        except Exception:  # noqa: BLE001
            logger.debug("[voice] 入站语音判断失败", exc_info=True)

    # ── 钩子 2：发出去之前把文字换成语音 ────────────────────────────
    @filter.on_decorating_result(priority=-2000)
    async def _on_decorating(self, event: AstrMessageEvent) -> None:
        try:
            await self._maybe_convert(event)
        except Exception:  # noqa: BLE001
            logger.error("[voice] 出站转换异常", exc_info=True)

    async def _maybe_convert(self, event: AstrMessageEvent) -> None:
        if not self._flag("mood", "enable", True):
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        if event.get_extra("_voice_done"):
            return

        chain = list(result.chain)
        if any(isinstance(seg, _BLOCKING) for seg in chain):
            return

        text = "".join(seg.text for seg in chain if isinstance(seg, Plain)).strip()
        if not text:
            return

        key = event.unified_msg_origin
        now = time.time()
        forced = bool(event.get_extra("_voice_force"))
        in_window = self._window_active(event)
        mood, policy, data = self._policy()

        if not forced:
            if now < self._blocked_until:
                logger.info("[voice] 合成连续失败，冷却中，只用文字")
                return
            if (event.message_str or "").strip().startswith("/"):
                return
            if not policy["voice"]:
                logger.info(f"[voice] 心情「{mood}」，这次只打字")
                return
            gap = max(self._num("mood", "min_interval", 90), float(policy.get("cooldown") or 0))
            if now - self._last_voice.get(key, 0.0) < gap and not in_window:
                logger.info(f"[voice] 心情「{mood}」，但刚说过话，隔一会儿")
                return

        min_chars = int(self._num("mood", "min_chars", 2))
        max_chars = int(self._num("mood", "max_chars", 60))
        ok, why = text_is_speakable(text, min_chars, max_chars)
        if not ok:
            logger.info(f"[voice] 不转语音：{why}")
            return

        # 同一时间只跑一个合成：CPU 上一句要 10~30 秒，排队会拖垮整条对话
        if not forced:
            try:
                await asyncio.wait_for(self._synth_gate.acquire(), timeout=0.05)
            except asyncio.TimeoutError:
                logger.info("[voice] 上一条还在合成，这次先打字")
                return
        try:
            # 兴奋到一定程度，隔够久了就「突然唱两句」：
            # 正常回复照发（不吞内容），过两秒再补一条念唱——像说着话突然哼起来。
            song_ready = (
                not forced
                and policy.get("song")
                and len(text) <= int(self._num("song", "max_reply_chars", 20))
                and now - self._last_song.get(key, 0.0) >= self._num("song", "cooldown", 1200)
            )
            # 学新歌只在「真要唱」这一刻做，而且失败要冷静 6 小时——
            # 否则网断了会每条语音消息都卡 20 秒去网易云重试。
            if song_ready and now >= self._learn_block_until and self._can_learn():
                try:
                    learned, msg, _title = await asyncio.wait_for(self._learn_song(), timeout=20)
                    logger.info(f"[voice] 周更学歌：{msg}")
                    if not learned:
                        self._learn_block_until = now + 6 * 3600
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[voice] 周更学歌失败：{exc}")
                    self._learn_block_until = now + 6 * 3600
            song = self._pick_song() if self._flag("song", "enable", True) else None
            if song_ready and song:
                song_record = await self._record(
                    song["lyrics"],
                    event,
                    speed=self._num("song", "speed", 0.88),
                    track=False,
                    timeout=self._num("song", "timeout", 180),
                )
                if song_record is not None:
                    self._last_song[key] = now
                    self._last_voice[key] = now
                    event.set_extra("_voice_done", True)
                    self._spawn(self._sing_later(event, song_record))
                    logger.info(f"[voice] 心情「{mood}」→ 突然唱《{song['title']}》：{song['lyrics']}")
                    return

            record = await self._record(text, event, speed=float(policy.get("speed") or 1.0))
            if record is None:
                return

            keep = [seg for seg in chain if isinstance(seg, At)] + [record]
            self._last_voice[key] = now
            event.set_extra("_voice_done", True)
            event.set_result(event.chain_result(keep))
            logger.info(f"[voice] 心情「{mood}」→ 语音（{len(text)} 字，语速 {policy.get('speed')}）")
        finally:
            if not forced and self._synth_gate.locked():
                self._synth_gate.release()

    # ── 念唱曲库 ────────────────────────────────────────────────────
    def _songs(self) -> list[dict]:
        """曲库 = 配置里写的 + 她自己学来的（学来的单独存 JSON，不碰配置）。"""
        raw = self._str("song", "lines", "").strip()
        songs: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                title, lyrics = line.split("|", 1)
            else:
                title, lyrics = "无名小曲", line
            lyrics = lyrics.strip()
            if lyrics:
                songs.append({"title": title.strip() or "无名小曲", "lyrics": lyrics})
        if not songs:
            songs = [dict(s) for s in DEFAULT_SONGS]

        known = {s["title"] for s in songs}
        for song in self._library().songs():
            if song["title"] and song["title"] not in known and song["lyrics"]:
                songs.append(song)
                known.add(song["title"])
        return songs

    def _pick_song(self, topic: str = "") -> dict | None:
        """从曲库里挑一首：给了歌名就按标题找，否则随机但避免连着唱同一首。"""
        songs = self._songs()
        if not songs:
            return None
        topic = (topic or "").strip()
        if topic:
            for song in songs:
                if topic in song["title"] or song["title"] in topic:
                    return song
        pool = [s for s in songs if s["title"] != self._last_song_title] or songs
        song = random.choice(pool)
        self._last_song_title = song["title"]
        return song

    # ── 学歌 ────────────────────────────────────────────────────────
    def _library(self) -> SongLibrary:
        if getattr(self, "_song_lib", None) is None:
            path = self._str("song", "library_file", "").strip()
            if not path:
                path = os.path.join(get_astrbot_plugin_data_path(), "voice", "learned_songs.json")
            self._song_lib = SongLibrary(path)
        return self._song_lib

    def _can_learn(self) -> bool:
        if not self._flag("song", "learn_enable", True):
            return False
        lib = self._library()
        days = max(0.0, self._num("song", "learn_interval_days", 7))
        cap = int(self._num("song", "max_songs", 40))
        if len(lib.songs()) >= cap:
            return False
        return time.time() - lib.last_learn >= days * 86400

    async def _netease_json(self, path: str, params: dict) -> dict:
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {"User-Agent": _UA, "Referer": "https://music.163.com/"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(_NETEASE + path, params=params) as resp:
                if resp.status != 200:
                    raise TTSError(f"网易云接口 {resp.status}")
                return await resp.json(content_type=None)

    async def _chart_tracks(self) -> list[dict]:
        _name, chart_id = random.choice(_CHARTS)
        data = await self._netease_json("/api/playlist/detail", {"id": chart_id})
        tracks = (data.get("result") or {}).get("tracks") or []
        out = []
        for t in tracks:
            if not t.get("id"):
                continue
            artists = t.get("artists") or [{}]
            out.append(
                {
                    "id": t["id"],
                    "title": str(t.get("name") or "").strip(),
                    "artist": str((artists[0] or {}).get("name") or "").strip(),
                }
            )
        return out

    async def _search_tracks(self, keyword: str) -> list[dict]:
        data = await self._netease_json(
            "/api/search/get/web", {"s": keyword, "type": 1, "limit": 5}
        )
        songs = (data.get("result") or {}).get("songs") or []
        out = []
        for t in songs:
            artists = t.get("artists") or [{}]
            out.append(
                {
                    "id": t.get("id"),
                    "title": str(t.get("name") or "").strip(),
                    "artist": str((artists[0] or {}).get("name") or "").strip(),
                }
            )
        return [t for t in out if t["id"] and t["title"]]

    async def _fetch_lyric(self, song_id: int) -> str:
        data = await self._netease_json("/api/song/lyric", {"id": song_id, "lv": 1, "kv": 1, "tv": -1})
        return str((data.get("lrc") or {}).get("lyric") or "")

    async def _learn_song(self, topic: str = "") -> tuple[bool, str, str]:
        """学一首：给了关键词就搜，没给就从热歌榜/飙升榜前 50 里挑一首没学过的。"""
        lib = self._library()
        known = {s["title"] for s in self._songs()}
        try:
            if topic:
                tracks = await self._search_tracks(topic)
            else:
                tracks = await self._chart_tracks()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[voice] 取歌单失败：{exc}")
            return False, "歌单拿不到，先不学了", ""

        for track in tracks:
            if track["title"] in known:
                continue
            try:
                lrc = await self._fetch_lyric(track["id"])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[voice] 取歌词失败《{track['title']}》：{exc}")
                continue
            lyric = clean_lyric(lrc, int(self._num("song", "max_lyric_chars", 40)))
            if not lyric:
                continue
            if not lib.add(track["title"], lyric, int(track["id"])):
                continue
            artist = f" / {track['artist']}" if track["artist"] else ""
            logger.info(f"[voice] 学会新歌《{track['title']}》{artist}：{lyric}")
            return True, f"学会了《{track['title']}》{artist}", track["title"]

        return False, "这几首都学过了，没新的", ""

    def _spawn(self, coro) -> None:
        """后台跑一个协程，同时留个引用防止被 GC 回收。"""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sing_later(self, event: AstrMessageEvent, record: Record, delay: float = 3.0) -> None:
        """等文字的回复先发出去，再补一条唱歌的语音。"""
        await asyncio.sleep(delay)
        try:
            await event.send(event.chain_result([record]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[voice] 补发唱歌失败：{exc}")

    # ── /嗓子：一眼看出她现在什么心情、会不会出声 ───────────────────
    async def _ping_gsv(self, api_base: str) -> bool:
        """服务在不在：先试 /docs，不行再退回 TCP 连端口。

        冷启动（要加载 1 分钟模型）时端口还没起来，这里会判成「连不上」；
        端口一旦 listen 就算在线，不用等 /docs 可访问。
        """
        base = api_base.rstrip("/")
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(base + "/docs") as resp:
                    if resp.status < 500:
                        return True
        except Exception:  # noqa: BLE001
            pass
        try:
            from urllib.parse import urlparse

            u = urlparse(base)
            host = u.hostname or "127.0.0.1"
            port = u.port or (443 if u.scheme == "https" else 80)
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=3
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

    @filter.command("嗓子")
    async def cmd_status(self, event: AstrMessageEvent):
        """看风铃现在的嗓子和心情状态"""
        mood, policy, data = self._policy()
        engine = self._get_engine()
        score = data.get("mood_score")
        energy = data.get("energy")
        lines = [
            f"心情：{mood}"
            + (f"（分数 {float(score):.2f}，精力 {float(energy):.2f}）" if score is not None and energy is not None else ""),
            f"声音：{'自动语音开（语速 ' + str(policy['speed']) + '）' if policy['voice'] else '这种心情不主动发语音（/tts、点她念照样能出声）'}",
            f"后端：{type(engine).__name__}",
        ]
        if isinstance(engine, GSVEngine):
            miss = engine.missing()
            lines.append("配置：" + ("齐了" if not miss else "缺 " + "、".join(miss)))
            lines.append(
                "服务："
                + (
                    "在线"
                    if await self._ping_gsv(engine.api_base)
                    else "连不上（刚启动要等约 1 分钟；这期间只发文字）"
                )
            )
        last = self._last_voice.get(event.unified_msg_origin)
        if last:
            lines.append(f"上次语音：{int(time.time() - last)} 秒前")
        yield event.plain_result("\n".join(lines))

    # ── /学歌：学一首新歌进曲库 ─────────────────────────────────────
    @filter.command("学歌")
    async def cmd_learn(self, event: AstrMessageEvent):
        """学一首歌进曲库：/学歌 [歌名]，不给歌名就从热歌榜挑"""
        raw = (event.message_str or "").strip()
        for prefix in ("/学歌", "学歌"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        if not self._flag("song", "learn_enable", True):
            yield event.plain_result("学歌被关掉了呢")
            return
        ok, msg, title = await self._learn_song(raw)
        if not ok:
            yield event.plain_result(msg)
            return
        self._last_song_title = title
        yield event.plain_result(f"{msg}\n要听听吗？发 `/唱歌 {title}`")

    # ── /唱歌：念唱两句（不看心情，手动点） ─────────────────────────
    @filter.command("唱歌")
    async def cmd_sing(self, event: AstrMessageEvent):
        """让风铃念唱两句：/唱歌 [歌名]"""
        raw = (event.message_str or "").strip()
        for prefix in ("/唱歌", "唱歌"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        if not self._flag("song", "enable", True):
            yield event.plain_result("唱歌被关掉了呢")
            return
        song = self._pick_song(raw)
        if song is None:
            yield event.plain_result("曲库是空的……先在配置里放两首吧")
            return
        yield event.plain_result(f"♪ {song['title']}")
        record = await self._record(
            song["lyrics"],
            event,
            speed=self._num("song", "speed", 0.88),
            timeout=self._num("song", "timeout", 180),
        )
        if record is None:
            yield event.plain_result("嗓子哑了，没唱出来……看下日志吧")
            return
        self._last_song[event.unified_msg_origin] = time.time()
        yield event.chain_result([record])

    # ── /tts 点播 ───────────────────────────────────────────────────
    async def _cmd_tts(self, event: AstrMessageEvent, raw: str):
        text = clean_text(raw)
        if not text:
            yield event.plain_result("要念什么？例如：/tts 今天也想你")
            return
        if self._flag("command", "admin_only", False) and not event.is_admin():
            yield event.plain_result("这个只有主人能用哦")
            return
        _, policy, _ = self._policy()
        record = await self._record(text, event, speed=float(policy.get("speed") or 1.0))
        if record is None:
            yield event.plain_result("嗓子哑了，合成没成功……看下日志吧")
            return
        self._refresh_window(event)
        yield event.chain_result([record])

    @filter.command("tts")
    async def cmd_tts(self, event: AstrMessageEvent):
        """用风铃的声音念一段话：/tts 要念的内容"""
        raw = (event.message_str or "").strip()
        if raw.lower().startswith("/tts"):
            raw = raw[4:].strip()
        elif raw.lower().startswith("tts"):
            raw = raw[3:].strip()
        async for item in self._cmd_tts(event, raw):
            yield item

    @filter.command("语音")
    async def cmd_say(self, event: AstrMessageEvent):
        """同 /tts：用风铃的声音念一段话"""
        raw = (event.message_str or "").strip()
        for prefix in ("/语音", "语音"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        async for item in self._cmd_tts(event, raw):
            yield item

    # ── LLM 工具：她自己决定「这句话想用说的」 ──────────────────────
    @filter.llm_tool(name="tts_voice")
    async def tool_voice(self, event: AstrMessageEvent, text: str):
        """把一段话用风铃自己的声音念出来（发一条 QQ 语音）。

        只在「说出来比打字更自然」时用：撒娇、晚安、哄人、短句情绪。
        心情是「困」或「生气」时不要用——那时候她不想出声。
        念完就不要再把同样的内容打一遍。

        Args:
            text(string): 要念出来的内容，一到两句话，不超过 60 字
        """
        if not self._flag("tool", "enable", True):
            return "语音工具被关掉了，用文字回复"
        mood, policy, _ = self._policy()
        # 只有「困」「生气」是真的不想出声；平静/无聊/闷闷的只是「不主动说」，
        # 主人明确点她说话时该出声——不然会出现「让她念一句，她回不想出声」。
        if mood in ("困", "生气"):
            return f"现在心情是「{mood}」，不想出声，用文字回吧"
        record = await self._record(clean_text(text), event, speed=float(policy.get("speed") or 1.0))
        if record is None:
            return "语音合成失败了，这次请用文字回复"
        try:
            await event.send(event.chain_result([record]))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[voice] 工具发语音失败：{exc}", exc_info=True)
            return "语音发送失败，用文字回复"
        event.set_extra("_voice_done", True)
        self._last_voice[event.unified_msg_origin] = time.time()
        return "已经用语音说出来了，不要再重复这段内容"

    # ── LLM 工具：学歌 ──────────────────────────────────────────────
    @filter.llm_tool(name="learn_song")
    async def tool_learn(self, event: AstrMessageEvent, song: str):
        """学一首新歌进曲库（只存歌词，不下载音频）。

        用户说「学一首 XX」「你会唱 XX 吗」时用。不给歌名就留空，会从热歌榜里挑。
        学完不要马上唱，等心情到「兴奋」或者用户让你唱时再唱。

        Args:
            song(string): 歌名，可以为空；留空表示随便从热歌榜学一首
        """
        if not self._flag("song", "learn_enable", True):
            return "学歌被关掉了"
        ok, msg, title = await self._learn_song(song or "")
        if not ok:
            return msg
        return f"{msg}，想听就说「唱 {title}」"
