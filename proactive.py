"""主动想起 / 勿扰 / 节奏规则（纯函数，便于测试）。"""

from __future__ import annotations

import time
from dataclasses import dataclass


def _hm_to_minutes(value: str) -> int:
    try:
        hh, mm = value.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return 0


def in_quiet_hours(now: float, quiet_start: str = "23:00", quiet_end: str = "07:00") -> bool:
    lt = time.localtime(now)
    cur = lt.tm_hour * 60 + lt.tm_min
    start = _hm_to_minutes(quiet_start)
    end = _hm_to_minutes(quiet_end)
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


@dataclass(slots=True)
class ProactiveState:
    """主动行为状态。

    除基础时间线外，保留原版类似的状态维度：
    unanswered_count（连续未回应次数）、desire（想联系的欲望）、
    affinity（亲密度）、last_scene（上次主动场景）、epoch（主动纪元）。
    """

    stream_id: str
    last_message_ts: float = 0.0
    last_proactive_ts: float = 0.0
    scheduled_at: float = 0.0
    scheduled_reason: str = ""
    paused: bool = False
    pause_reason: str = ""
    unanswered_count: int = 0
    desire: float = 0.0
    affinity: float = 0.0
    last_scene: str = ""
    epoch: int = 0


def effective_trigger_probability(
    trigger_probability: float,
    unanswered_count: int = 0,
    unanswered_boost: float = 0.0,
    desire: float = 0.0,
) -> float:
    """按未回应次数与欲望修正触发概率（上限 1.0）。"""
    boosted = trigger_probability + unanswered_boost * min(max(unanswered_count, 0), 3)
    boosted += max(0.0, min(desire, 1.0)) * 0.1
    return max(0.0, min(1.0, boosted))


def should_trigger(
    state: ProactiveState,
    *,
    now: float,
    silence_threshold: int,
    trigger_probability: float,
    min_interval: int,
    quiet_start: str,
    quiet_end: str,
    unanswered_boost: float = 0.0,
) -> bool:
    """判断此刻是否该主动联系。

    - 勿扰 / 暂停 / 沉默未到 / 距上次主动太近 → 否；
    - 有预约（scheduled_at）→ 到期即触发；
    - 否则按「基础概率 + 未回应加成 + 欲望加成」掷骰。
    """
    if state.paused or in_quiet_hours(now, quiet_start, quiet_end):
        return False
    if now - state.last_message_ts < silence_threshold:
        return False
    if now - state.last_proactive_ts < min_interval:
        return False
    if state.scheduled_at > 0:
        return now >= state.scheduled_at
    import random

    probability = effective_trigger_probability(
        trigger_probability,
        unanswered_count=state.unanswered_count,
        unanswered_boost=unanswered_boost,
        desire=state.desire,
    )
    return random.random() < probability
