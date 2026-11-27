"""费率政策时间线与每次停放的费率承诺。

政策切换（``price`` 帧）是开放口径的切换：

- 开放时段内登记的停放，保留入场时已经生效的费率承诺，直到当次停放结束；
  访客从免费区转入收费区时，该承诺同样保留至当次停放结束。
- 政策切换之后才登记的新停放，按新开放口径计费。

因此费率承诺在 ``enter`` 时按当时政策快照绑定到停放（stay），后续 ``price``
帧不回溯改变任何在途停放。设备掉线补送的消息仍携带原现场时刻，计费取的是
消息发生时的区域事实与承诺，而不是消息到达时的价格。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateSchedule:
    """某一政策口径下各区费率（分/分钟）。"""

    policy_id: str
    effective_from: int
    rates: dict[str, int]

    def rate_of(self, area: str) -> int:
        return self.rates.get(area, 0)


class PolicyTimeline:
    """按现场时刻维护政策序列，回答任意时刻入场应绑定哪张费率表。"""

    def __init__(self, profile: dict):
        self._gates: dict[str, str] = profile["gates"]
        policy = profile["policy"]
        self._unit = policy["rate_unit"]
        if self._unit != "fen_per_min":
            raise ValueError(f"不支持的费率单位: {self._unit}")
        baseline = RateSchedule(
            policy_id="baseline",
            effective_from=0,
            rates={area: detail["rate"] for area, detail in profile["areas"].items()},
        )
        self._schedules: list[RateSchedule] = [baseline]

    def apply_price(self, policy_id: str, start_sec: int, rates: dict[str, int]) -> RateSchedule:
        """登记一条新政策。rates 为该政策完整的分区费率表。"""
        if self._schedules and start_sec <= self._schedules[-1].effective_from:
            raise ValueError("政策开始时刻必须严格晚于上一条政策")
        schedule = RateSchedule(policy_id, start_sec, dict(rates))
        self._schedules.append(schedule)
        return schedule

    def schedule_at(self, captured_sec: int) -> RateSchedule:
        """现场时刻 ``captured_sec`` 生效的政策（取开始时刻不晚于它的最后一条）。"""
        current = self._schedules[0]
        for schedule in self._schedules[1:]:
            if schedule.effective_from <= captured_sec:
                current = schedule
            else:
                break
        return current

    def entry_schedule(self, entry_captured: int) -> RateSchedule:
        """一次 ``enter`` 应绑定的费率承诺。"""
        return self.schedule_at(entry_captured)

    def gate_area(self, gate: str) -> str:
        return self._gates[gate]
