"""泰拉瑞亚：让 AI 看画面、发按键。

思路：截图 → 视觉模型看懂 → 模型决定按什么键 → 通过 Win32 发送按键。
安全阀：游戏不在前台就拒绝发按键，绝不往别的窗口打字。
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import os
import time
from ctypes import wintypes

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

_SHOT_DIR = r"D:\AstrBot\terraria-shots"
_VISION_CFG = r"D:\AstrBot\bot\data\config\astrbot_plugin_screen_eye_config.json"

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

_VK = {
    "a": 0x41, "d": 0x44, "w": 0x57, "s": 0x53,
    "space": 0x20, "shift": 0x10, "ctrl": 0x11,
    "e": 0x45, "esc": 0x1B, "tab": 0x09,
    "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35,
    "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39, "0": 0x30,
    "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
}


def _find_terraria_window():
    """按进程名找泰拉瑞亚窗口，返回 (hwnd, rect) 或 (None, None)。"""
    try:
        import psutil  # 可用则更准，不行就退回 EnumWindows
    except Exception:  # noqa: BLE001
        psutil = None

    pids = set()
    if psutil:
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info["name"] or "").lower().startswith("terraria"):
                    pids.add(p.pid)
            except Exception:  # noqa: BLE001
                continue
    if not pids:
        return None, None

    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and _user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    _user32.EnumWindows(_cb, 0)
    if not found:
        return None, None

    hwnd = found[0]
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return hwnd, (rect.left, rect.top, rect.right, rect.bottom)


def _is_foreground(hwnd) -> bool:
    return _user32.GetForegroundWindow() == hwnd


def _focus(hwnd):
    try:
        _user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
    except Exception:  # noqa: BLE001
        pass


def _screenshot(rect):
    from PIL import ImageGrab

    left, top, right, bottom = rect
    img = ImageGrab.grab(bbox=(left, top, right, bottom))
    if img.width > 1280:
        ratio = 1280 / img.width
        img = img.resize((1280, int(img.height * ratio)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=75)
    return buf.getvalue()


def _describe(jpeg: bytes) -> str:
    try:
        with io.open(_VISION_CFG, encoding="utf-8-sig") as fh:
            key = (json.load(fh).get("api_key") or "").strip()
    except Exception:  # noqa: BLE001
        key = ""
    if not key:
        return "（没配视觉模型）"
    payload = {
        "model": "glm-4v-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
                    {"type": "text", "text": (
                        "这是《泰拉瑞亚》的游戏画面。用中文说清楚：玩家角色在地面还是半空、"
                        "左边还是右边有怪/矿、脚下有没有坑、血量和蓝量大概多少、快捷栏选中的是什么。"
                        "只描述看到的，不猜。120 字以内。"
                    )},
                ],
            }
        ],
        "temperature": 0.2,
        "max_tokens": 400,
    }
    import urllib.request

    req = urllib.request.Request(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        return f"（看画面失败：{exc}）"


def _press(key: str, hold_ms: int = 200) -> str:
    vk = _VK.get(str(key).lower())
    if not vk:
        return f"不支持的按键：{key}（可用：a d w s space e esc 1-0 left right up down）"
    hwnd, _ = _find_terraria_window()
    if not hwnd:
        return "泰拉瑞亚没在运行。"
    if not _is_foreground(hwnd):
        _focus(hwnd)
        if not _is_foreground(hwnd):
            return "游戏窗口不在前台，为了不误触别的窗口，这次不发按键。"
    _user32.keybd_event(vk, 0, 0, 0)
    time.sleep(max(0.05, min(hold_ms, 3000) / 1000.0))
    _user32.keybd_event(vk, 0, 2, 0)
    return f"已按下 {key}（{hold_ms}ms）"


def _click(rx: float, ry: float, button: str = "left") -> str:
    hwnd, rect = _find_terraria_window()
    if not hwnd:
        return "泰拉瑞亚没在运行。"
    if not _is_foreground(hwnd):
        _focus(hwnd)
        if not _is_foreground(hwnd):
            return "游戏窗口不在前台，这次不点击。"
    left, top, right, bottom = rect
    x = int(left + (right - left) * max(0.0, min(1.0, rx)))
    y = int(top + (bottom - top) * max(0.0, min(1.0, ry)))
    _user32.SetCursorPos(x, y)
    down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
    _user32.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.05)
    _user32.mouse_event(up, 0, 0, 0, 0)
    return f"已点击 ({x}, {y})"


@register(
    "terraria",
    "dafeiyu",
    "让 AI 看泰拉瑞亚画面并按键（前台才操作）",
    "1.0.0",
)
class Terraria(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}

    @llm_tool(name="terraria_look")
    async def terraria_look(self, event: AstrMessageEvent) -> str:
        """看一眼泰拉瑞亚当前画面，返回画面上有什么（角色位置、怪、矿、血量）。

        玩家让你玩游戏、问「现在游戏里什么情况」时调用。每次行动后建议再看一眼。
        """
        def _run():
            hwnd, rect = _find_terraria_window()
            if not hwnd:
                return "泰拉瑞亚没在运行，先打开游戏。"
            if not _is_foreground(hwnd):
                _focus(hwnd)
            jpeg = _screenshot(rect)
            return _describe(jpeg)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.exception("terraria_look failed")
            return f"看画面失败：{exc}"

    @llm_tool(name="terraria_press")
    async def terraria_press(self, event: AstrMessageEvent, key: str, hold_ms: str = "200") -> str:
        """按一个键：a/d 左右走，space 跳，e 打开背包，1-9 切换快捷栏，esc 关闭界面。

        Args:
            key(string): 键名，如 a、d、space、e、1、esc、left、right
            hold_ms(string): 按住毫秒数，默认 200，走路可以 400-800
        """
        try:
            ms = int(float(hold_ms or 200))
        except Exception:  # noqa: BLE001
            ms = 200
        return await asyncio.to_thread(_press, key, ms)

    @llm_tool(name="terraria_click")
    async def terraria_click(self, event: AstrMessageEvent, x: str, y: str, button: str = "left") -> str:
        """在游戏窗口内点击，坐标是 0~1 的相对位置（0.5,0.5 是屏幕正中）。

        Args:
            x(string): 横向相对位置 0~1
            y(string): 纵向相对位置 0~1
            button(string): left 或 right
        """
        try:
            rx = float(x)
            ry = float(y)
        except Exception:  # noqa: BLE001
            return "坐标要传 0~1 的数字。"
        return await asyncio.to_thread(_click, rx, ry, button)

    @llm_tool(name="terraria_status")
    async def terraria_status(self, event: AstrMessageEvent) -> str:
        """检查泰拉瑞亚是否在运行、窗口是否在前台。"""
        hwnd, rect = _find_terraria_window()
        if not hwnd:
            return "泰拉瑞亚没在运行。"
        return f"游戏在运行，窗口 {'在前台' if _is_foreground(hwnd) else '不在前台'}，位置 {rect}。"

    @filter.command("泰拉")
    async def terraria_cmd(self, event: AstrMessageEvent, action: str = ""):
        """发 /泰拉 看一眼当前画面。"""
        if action in ("状态", "status"):
            hwnd, _ = _find_terraria_window()
            yield event.plain_result("游戏在运行" if hwnd else "泰拉瑞亚没在运行。")
            return
        try:
            jpeg = await asyncio.to_thread(lambda: _screenshot(_find_terraria_window()[1]))
            desc = await asyncio.to_thread(_describe, jpeg)
            os.makedirs(_SHOT_DIR, exist_ok=True)
            path = os.path.join(_SHOT_DIR, f"t_{int(time.time())}.jpg")
            with open(path, "wb") as fh:
                fh.write(jpeg)
            yield event.chain_result([Plain(desc), Image.fromFileSystem(path)])
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"看不了：{exc}")
