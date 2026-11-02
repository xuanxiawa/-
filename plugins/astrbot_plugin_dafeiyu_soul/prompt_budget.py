"""注入预算：soul / heart / shared_brain 三个插件共用的一本账。

为什么需要
----------
soul 的易变段、heart 的心情块、shared_brain 的共享块各自往 prompt 里拼，
谁也没算总账：一句长日记 + 三条群聊 + 十条梗，一轮能塞两三千字。这里给
每个来源一个配额，同一轮里累计记总账，超了就从自己这块截断。

用法（每个插件注入前调一次）：

    text = budget.offer(req, "heart", block, quota=800, audit_dir=_DIR)

- quota：这个来源最多占多少字；不传就用 QUOTAS 里的默认值
- 总预算 TOTAL_BUDGET：三家合计，超了按剩余空间截断
- 账记在 req 对象的属性上，三个 hook 拿到的是同一个 req，不需要跨插件共享状态
- 每次注入往 <audit_dir>/inject_audit.jsonl 记一行，事后能核对到底塞了多少

设计取舍
--------
只做"截断自己"，不动别人已经写进 prompt 的内容——所以哪个插件没加载、
哪个 hook 报错，其他插件都照常工作，不需要额外兜底。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# 注入总量上限，单位：字符（四家配额之和，正好等于它）
TOTAL_BUDGET = 3800

# 每个来源的默认配额；soul 的易变段最重要，给得最多
QUOTAS = {
    "soul": 1200,
    "heart": 800,
    "shared": 900,
    "persona": 900,
}

_ATTR_USED = "_dfy_tail_used"
_ATTR_LOG = "_dfy_tail_log"


def _cut(text: str, room: int) -> str:
    """截到 room 字以内，尽量断在句子/行边界上，并留个省略号。"""
    if len(text) <= room:
        return text
    if room <= 8:
        return text[:room]
    head = text[: room - 1]
    for sep in ("\n", "。", "；", "，", " "):
        i = head.rfind(sep)
        if i >= room // 2:
            head = head[:i]
            break
    return head.rstrip() + "…"


def used(req) -> int:
    """本轮已经用掉的尾部字数。"""
    return int(getattr(req, _ATTR_USED, 0) or 0)


def offer(req, key: str, text: str, quota: int | None = None, audit_dir=None) -> str:
    """给一块要注入尾部的文本上账，返回（可能被截短的）文本。

    超出配额或总预算的部分从这一块自己的尾巴上砍，不碰别人。
    """
    if not text:
        return ""
    limit = QUOTAS.get(key, 1200) if quota is None else int(quota)
    already = used(req)
    room = min(limit, max(0, TOTAL_BUDGET - already))
    body = _cut(text, room) if len(text) > room else text

    setattr(req, _ATTR_USED, already + len(body))
    log = getattr(req, _ATTR_LOG, None)
    if log is None:
        log = []
        setattr(req, _ATTR_LOG, log)
    log.append({"k": key, "chars": len(body), "raw": len(text)})

    if audit_dir is not None:
        _audit(audit_dir, key, len(body), len(text), already)
    return body


def summary(req) -> str:
    """一行摘要，给日志/排查用：soul=820/1200 heart=310/800 …"""
    log = getattr(req, _ATTR_LOG, None) or []
    if not log:
        return "（本轮没有注入）"
    return " ".join(f"{x['k']}={x['chars']}/{x['raw']}" for x in log) + (
        f" total={used(req)}/{TOTAL_BUDGET}"
    )


def _audit(dir_path, key: str, chars: int, raw: int, before: int) -> None:
    """把这一笔记进账本；写失败不影响聊天。"""
    try:
        p = Path(dir_path) / "inject_audit.jsonl"
        row = {
            "ts": round(time.time(), 3),
            "k": key,
            "chars": chars,
            "raw": raw,
            "before": before,
        }
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if p.stat().st_size > 400_000:
            lines = p.read_text("utf-8").splitlines()[-800:]
            p.write_text("\n".join(lines) + "\n", "utf-8")
    except Exception:  # noqa: BLE001
        pass
