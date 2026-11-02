"""心情 / 精力的共用内核：一天的能量曲线、打盹、心情命名。

为什么要单独抽出来
------------------
以前 soul 自己算一套精力（只会涨，永远 0.98），heart 自己算一套（会掉，掉到 0.09），
`/心情` 显示的是 soul 的数字、心情名却是 heart 算的，于是出现「精力 0.98 · 心情 困」。
现在只留这一套：谁要显示、谁要判定，都读这里。

三个约定
--------
1. 精力跟着「一天的能量曲线」走，不是只增不减。
2. 正在聊天时不回血（回得极慢），不聊了才往曲线靠；夜里靠得快。
3. 「困」必须和精力自洽：精力不低就不许说困。
"""

from __future__ import annotations

import math

# 一天的能量基线：凌晨最低，上午最足，午后小低谷，晚上往下走
# (小时, 精力)
ENERGY_CURVE = [
    (0.0, 0.10),
    (5.0, 0.10),
    (6.5, 0.30),
    (7.0, 0.72),
    (9.0, 0.88),
    (12.0, 0.82),
    (14.0, 0.62),
    (17.0, 0.68),
    (19.0, 0.55),
    (21.0, 0.42),
    (23.0, 0.22),
    (24.0, 0.10),
]

SLEEPY_BELOW = 0.22        # 低于这个才算困
SLEEPY_NIGHT_BELOW = 0.45  # 夜里（23 点后 / 6 点前）低于这个也算困

# 聊天时的「疲惫上限」：不管聊多久，白天的精力最多比当前时段的基线低这么多。
# 以前没有这个上限，一轮长聊（几百句）能把精力从 0.6 一路打到 0.05，
# 之后好几个小时都在「困」——这就是「下午聊一小时，一晚上都困」的根。
CHAT_DEFICIT = 0.20

# 单句话的精力消耗：以前 0.003~0.012，一百句就掉 0.3~1.0，太狠了
DRAIN_MIN = 0.0012
DRAIN_MAX = 0.0045


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def energy_target(hour: float) -> float:
    """某个时刻的精力基线。hour 用 0~24 的浮点，23.5 就是 23:30。"""
    h = hour % 24.0
    for i in range(len(ENERGY_CURVE) - 1):
        h0, v0 = ENERGY_CURVE[i]
        h1, v1 = ENERGY_CURVE[i + 1]
        if h0 <= h <= h1:
            if h1 == h0:
                return v0
            return v0 + (v1 - v0) * (h - h0) / (h1 - h0)
    return ENERGY_CURVE[-1][1]


def is_asleep_hour(hour: float) -> bool:
    """23:00 ~ 06:59 算睡觉时段。"""
    h = hour % 24.0
    return h >= 23.0 or h < 7.0


def tick(energy: float, hours: float, hour: float, idle_h: float) -> float:
    """过了一段时间，精力往当前时段的基线靠。

    hours   距上次结算过了几小时
    hour    现在几点（0~24）
    idle_h  多久没跟他说话了
    """
    energy = clamp(energy, 0.05, 1.0)
    if hours <= 0:
        return energy
    target = energy_target(hour)
    asleep = is_asleep_hour(hour)
    if idle_h < 0.5:
        # 正聊着：比基线低的时候回得快一点（别一直趴在地上），
        # 高于基线时慢慢落回去。以前这里 12 小时，等于聊天时完全不回血。
        tau = 4.0 if energy < target else 12.0
    elif asleep and idle_h >= 0.5:
        tau = 1.0   # 睡着了
    else:
        tau = 2.5   # 白天闲着
    k = 1.0 - math.exp(-hours / tau)
    return clamp(energy + (target - energy) * k, 0.05, 1.0)


def drain(energy: float, text: str, hour: float | None = None) -> float:
    """说一句话掉一点精力：说得越长掉得越多，但一句话最多掉 0.0045。

    传了 hour 就会受 CHAT_DEFICIT 保护：白天聊得再狠，也不会比当前基线
    低超过 0.2，避免一轮长聊把整晚的精力提前透支完。
    """
    n = len(text or "")
    cost = DRAIN_MIN + min(DRAIN_MAX - DRAIN_MIN, n / 1000.0 * 0.0025)
    e = clamp(energy - cost, 0.05, 1.0)
    if hour is not None and not is_asleep_hour(hour):
        floor = max(0.05, energy_target(hour) - CHAT_DEFICIT)
        e = max(e, floor)
    return e


def is_sleepy(energy: float, hour: float) -> bool:
    if energy < SLEEPY_BELOW:
        return True
    if is_asleep_hour(hour) and energy < SLEEPY_NIGHT_BELOW:
        return True
    return False


def mood_label(
    score: float, energy: float, idle_h: float, hour: float, angry: bool = False
) -> str:
    """心情名。顺序有讲究：情绪事件 > 困 > 关系状态 > 情绪档位。

    注意：「无聊」不在这里判——那是 soul 自己的无聊度管的（它有真实的无聊模型）。
    以前按情绪分 0.42~0.52 硬映射成「无聊」，才会出现「无聊 0.00 · 心情 无聊」。
    """
    score = clamp(score)
    energy = clamp(energy)
    if angry or score < 0.22:
        return "生气"
    sleepy = is_sleepy(energy, hour)
    # 正聊着（刚回过话）又是白天：精力还剩一点就别判「困」。
    # 以前只要精力掉下去，她一整天（包括正在聊的时候）都挂着「困」。
    if sleepy and idle_h < 0.3 and 7.0 <= (hour % 24.0) < 23.0 and energy >= 0.15:
        sleepy = False
    if sleepy:
        return "困"
    if idle_h >= 20 and score >= 0.55:
        return "黏人"
    if score >= 0.82 and energy >= 0.55:
        return "兴奋"
    if score >= 0.68:
        return "开心"
    if score >= 0.45:
        return "平静"
    if score >= 0.34:
        return "闷闷的"
    if score >= 0.26:
        return "有点难过"
    return "委屈"


def day_curve_points(step: float = 1.0):
    """给测试/调试用：一整天每小时的目标值。"""
    out = []
    h = 0.0
    while h <= 24.0:
        out.append((h, round(energy_target(h), 3)))
        h += step
    return out
