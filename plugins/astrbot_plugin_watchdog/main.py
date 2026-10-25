"""看门狗：QQ 掉线时弹二维码提醒，NapCat 真死了就拉起来。

每 60 秒：
- 6099 没监听 → NapCat 进程挂了 → 用隐藏启动器拉起
- 6099 在、但 6199 没有连接 → 多半掉登录在等扫码 → 超过 5 分钟就弹二维码 + 托盘气泡（15 分钟最多一次）
"""

from __future__ import annotations

import asyncio
import io
import re
import sqlite3
import subprocess
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

_LOG = r"D:\AstrBot\watchdog.log"
_NOTIFY = r"D:\AstrBot\notify-offline.ps1"
_WSCRIPT = r"C:\Windows\System32\wscript.exe"
_PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
_DB = r"D:\AstrBot\bot\data\data_v4.db"

_IMG_RE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\\\n\r]{500,}")


def _strip_images() -> int:
    """把对话历史里的 base64 图片换成 [图片]，避免每轮重发。"""
    try:
        con = sqlite3.connect(_DB, timeout=15)
        cur = con.cursor()
        rows = cur.execute(
            "SELECT conversation_id, content FROM conversations WHERE length(content) > 20000"
        ).fetchall()
        changed = 0
        for cid, content in rows:
            if not content or "base64," not in content:
                continue
            new = _IMG_RE.sub("[图片]", content)
            if new != content:
                cur.execute(
                    "UPDATE conversations SET content=? WHERE conversation_id=?",
                    (new, cid),
                )
                changed += 1
        con.commit()
        con.close()
        if changed:
            _log(f"清理历史图片: {changed} 个会话")
        return changed
    except Exception as exc:  # noqa: BLE001
        _log(f"清理图片失败: {exc}")
        return 0



def _log(msg: str) -> None:
    try:
        with io.open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _listening(port: int) -> bool:
    try:
        import psutil

        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
                return True
        return False
    except Exception:  # noqa: BLE001
        try:
            out = subprocess.run(
                [r"C:\Windows\System32\netstat.exe", "-ano"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            return f":{port} " in out and "LISTENING" in out
        except Exception:  # noqa: BLE001
            return True


def _link_up() -> bool:
    try:
        import psutil

        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_ESTABLISHED:
                if c.laddr and c.laddr.port == 6199:
                    return True
                if c.raddr and c.raddr.port == 6199:
                    return True
        return False
    except Exception:  # noqa: BLE001
        return True


@register("watchdog", "dafeiyu", "QQ 掉线提醒 + NapCat 自愈", "1.0.0")
class Watchdog(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._last_notify = 0.0
        self._down_since = 0.0

    @filter.on_astrbot_loaded()
    async def start(self):
        try:
            asyncio.create_task(self._loop())
        except Exception:  # noqa: BLE001
            logger.exception("watchdog start failed")

    async def _notify(self):
        now = time.time()
        if now - self._last_notify < 900:
            return
        self._last_notify = now
        _log("掉线超过 5 分钟，弹二维码提醒")
        try:
            subprocess.Popen(
                [_PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", _NOTIFY],
                creationflags=0x08000000,
            )
        except Exception:  # noqa: BLE001
            _log("notify 启动失败")

    async def _loop(self):
        await asyncio.sleep(45)
        _log("watchdog started")
        last_clean = 0.0
        while True:
            try:
                await asyncio.sleep(60)
                # 每 2 分钟清一次历史里的 base64 图片，避免每轮都重发
                if time.time() - last_clean > 120:
                    last_clean = time.time()
                    await asyncio.to_thread(_strip_images)
                if not _listening(6099):
                    _log("NapCat 6099 未监听，拉起")
                    subprocess.Popen(
                        [_WSCRIPT, "//nologo", r"D:\AstrBot\start-napcat-hidden.vbs"],
                        creationflags=0x08000000,
                    )
                    self._down_since = 0.0
                    await asyncio.sleep(30)
                    continue
                if _link_up():
                    if self._down_since:
                        _log("QQ 已恢复连接")
                    self._down_since = 0.0
                    continue
                if not self._down_since:
                    self._down_since = time.time()
                    _log("检测到 QQ 未连接（可能在等扫码）")
                    continue
                if time.time() - self._down_since >= 300:
                    await self._notify()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _log("loop error")
                logger.debug("watchdog loop error", exc_info=True)
