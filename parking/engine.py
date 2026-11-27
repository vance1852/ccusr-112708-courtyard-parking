"""按接收顺序回放设备事件的事件溯源引擎。

设计原则
--------
1. 原始帧不可变、不丢弃（重复帧也保留），所有结论都挂着设备证据指针，
   凭证可以一路回溯到 ``arrival``/``source``/``captured``。
2. 状态只由截至某一接收序号（arrival）已到达的事件推出；设备补送的消息
   按其 ``captured`` 现场时刻落位，价格按消息发生时的政策与承诺计，不按
   到达时的价格重算。
3. 凭证只追加、不修改。补来的证据改变金额时另开差额凭证，原凭证与已经
   合法放行的事实原样保留。
4. 通道穿越（pass）只证明“经过”，不证明“停放”；停放区间只由探头占位
   证据建立。抬杆（gate open）不代表探头确认驶离，驶离以 spot empty 为准。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from parking.events import Event, decode_frame
from parking.schedule import PolicyTimeline, RateSchedule


def round_half_up_fen(numerator: int, denominator: int = 60) -> int:
    """四舍五入到分（半数向上）。金额以 分/分钟 × 秒 表示为有理数。"""
    if numerator < 0:
        return -((-2 * numerator + denominator) // (2 * denominator))
    return (2 * numerator + denominator) // (2 * denominator)


# ---------------------------------------------------------------- 领域状态

@dataclass(frozen=True, slots=True)
class SpotFact:
    arrival: int
    captured: int
    sensor: str
    group: str
    area: str
    state: str          # present / empty
    reported_tag: str   # 车签或 unknown
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Correction:
    arrival: int
    captured: int
    sensor: str
    group: str
    tag: str
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PassFact:
    arrival: int
    captured: int
    tag: str
    from_area: str
    to_area: str
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Lease:
    lease_id: str
    desk: str
    until_sec: int
    arrival: int
    captured: int
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Receipt:
    desk: str
    lease_id: str
    txn: str
    fen: int
    arrival: int
    captured: int
    duplicate: bool
    authorized: bool
    note: str
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Voucher:
    """不可变凭证。kind: release / receipt / settlement / adjustment。"""

    id: str
    kind: str
    arrival: int
    captured: int
    stay_id: str
    desk: str | None
    title: str
    payload: dict[str, Any]
    evidence: list[dict[str, Any]]


@dataclass(slots=True)
class OpenItem:
    code: str
    created_arrival: int
    detail: str
    resolved_arrival: int | None = None
    resolution: str | None = None


@dataclass(slots=True)
class StayState:
    stay_id: str
    tag: str
    entry_gate: str
    entry_captured: int
    schedule: RateSchedule
    entry_evidence: dict[str, Any]
    passes: list[PassFact] = field(default_factory=list)
    leases: list[Lease] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    exit_requests: list[dict[str, Any]] = field(default_factory=list)
    gate_actions: list[dict[str, Any]] = field(default_factory=list)
    vouchers: list[Voucher] = field(default_factory=list)
    open_items: list[OpenItem] = field(default_factory=list)
    settled: bool = False
    settle_desk: str | None = None
    last_bill_fen: int | None = None
    asks: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------- 引擎

class ReplayEngine:
    def __init__(self, profile: dict):
        self.profile = profile
        self.timeline = PolicyTimeline(profile)
        self.gates: dict[str, str] = profile["gates"]
        self.paypoints: dict[str, dict] = profile["paypoints"]

        # 位组 -> 区域（组内车位同属一区）
        self.group_area: dict[str, str] = {}
        for area, detail in profile["areas"].items():
            for space in detail["spaces"]:
                self.group_area[space] = area

        self.stays: dict[str, StayState] = {}
        self.tag_to_stay: dict[str, str] = {}
        self.spot_facts: list[SpotFact] = []
        self.corrections: list[Correction] = []
        self.link_events: list[dict[str, Any]] = []
        self.anomalies: list[dict[str, Any]] = []
        self.vouchers: list[Voucher] = []
        self._voucher_seq = 0
        self._last_arrival = 0

    # ------------------------------------------------------------ 回放入口

    def replay(self, frames: Iterable[dict]) -> list[Voucher]:
        issued: list[Voucher] = []
        for frame in frames:
            event = decode_frame(frame, self.profile["wire_fields"])
            new_vouchers = self.apply(event)
            issued.extend(new_vouchers)
        return issued

    def apply(self, event: Event) -> list[Voucher]:
        if event.arrival <= self._last_arrival:
            raise ValueError("帧必须按 arrival 严格递增回放")
        self._last_arrival = event.arrival
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is None:
            raise ValueError(f"没有 {event.type} 的处理协议")
        before = len(self.vouchers)
        handler(event)
        return self.vouchers[before:]

    # ------------------------------------------------------------ 各协议

    def _on_enter(self, e: Event) -> None:
        stay_id = e.f("stay_id")
        tag = e.f("tag")
        if stay_id in self.stays:
            self.anomalies.append({
                "arrival": e.arrival, "kind": "duplicate_enter",
                "detail": f"停车号 {stay_id} 重复登记", "evidence": e.evidence_ref(),
            })
            return
        gate_area = self.gates[e.source]
        schedule = self.timeline.entry_schedule(e.captured)
        stay = StayState(
            stay_id=stay_id, tag=tag, entry_gate=e.source,
            entry_captured=e.captured, schedule=schedule,
            entry_evidence=e.evidence_ref(),
        )
        self.stays[stay_id] = stay
        self.tag_to_stay[tag] = stay_id

    def _on_pass(self, e: Event) -> None:
        stay = self._stay_by_tag(e.f("tag"), e)
        if stay is None:
            return
        stay.passes.append(PassFact(
            arrival=e.arrival, captured=e.captured, tag=e.f("tag"),
            from_area=e.f("from_area"), to_area=e.f("to_area"),
            evidence=e.evidence_ref(),
        ))

    def _on_spot(self, e: Event) -> None:
        group = e.f("group")
        area = self._group_area(group)
        fact = SpotFact(
            arrival=e.arrival, captured=e.captured, sensor=e.source,
            group=group, area=area, state=e.f("state"),
            reported_tag=e.f("tag"), evidence=e.evidence_ref(),
        )
        self.spot_facts.append(fact)
        # 探头确认空位：关闭“场内仍可能占位”的待核项（抬杆不会关闭它）
        if fact.state == "empty":
            stay = self._stay_by_tag(fact.reported_tag, e) if fact.reported_tag != "unknown" else None
            if stay is not None:
                self._resolve_item(stay, "occupancy_pending", e.arrival,
                                   f"探头 {fact.sensor} 于现场时刻 {fact.captured} 确认 {group} 驶离")

    def _on_correction(self, e: Event) -> None:
        group = e.f("group")
        self.corrections.append(Correction(
            arrival=e.arrival, captured=e.captured, sensor=e.f("sensor"),
            group=group, tag=e.f("tag"), evidence=e.evidence_ref(),
        ))
        stay = self._stay_by_tag(e.f("tag"), e)
        if stay is not None:
            self._reconsider_billing(stay, e)

    def _on_price(self, e: Event) -> None:
        start = e.f("start_sec")
        table_key = f"rates_from_{start}"
        rates = self.profile["policy"].get(table_key)
        if rates is None:
            raise ValueError(f"政策 {e.f('policy_id')} 缺少费率表 {table_key}")
        self.timeline.apply_price(e.f("policy_id"), start, rates)
        # 注意：不回溯修改任何在途 stay 的 schedule —— 承诺保留至当次停放结束。

    def _on_payment_lease(self, e: Event) -> None:
        stay = self._require_stay(e.f("stay_id"), e)
        if stay is None:
            return
        if e.f("desk") != e.source:
            self.anomalies.append({
                "arrival": e.arrival, "kind": "lease_source_mismatch",
                "detail": "lease 岗亭与消息来源不一致", "evidence": e.evidence_ref(),
            })
        stay.leases.append(Lease(
            lease_id=e.f("lease_id"), desk=e.f("desk"),
            until_sec=e.f("until_sec"), arrival=e.arrival,
            captured=e.captured, evidence=e.evidence_ref(),
        ))

    def _on_paid(self, e: Event) -> None:
        stay = self._require_stay(e.f("stay_id"), e)
        if stay is None:
            return
        desk, lease_id, txn, fen = e.source, e.f("lease_id"), e.f("txn"), e.f("fen")
        # 流水只在本岗亭持久唯一：按 (岗亭, 流水) 去重
        dup = next((r for r in stay.receipts if r.desk == desk and r.txn == txn), None)
        if dup is not None:
            if dup.fen != fen:
                self.anomalies.append({
                    "arrival": e.arrival, "kind": "txn_amount_conflict",
                    "detail": f"{desk} 流水 {txn} 重复上报且金额不一致",
                    "evidence": e.evidence_ref(),
                })
            stay.receipts.append(Receipt(
                desk=desk, lease_id=lease_id, txn=txn, fen=fen,
                arrival=e.arrival, captured=e.captured, duplicate=True,
                authorized=dup.authorized, note="重复消息，按一笔记账",
                evidence=e.evidence_ref(),
            ))
            return
        note, authorized = self._check_authorization(stay, desk, lease_id, e)
        receipt = Receipt(
            desk=desk, lease_id=lease_id, txn=txn, fen=fen,
            arrival=e.arrival, captured=e.captured, duplicate=False,
            authorized=authorized, note=note, evidence=e.evidence_ref(),
        )
        stay.receipts.append(receipt)
        stay.vouchers.append(self._issue(
            stay, "receipt", e, desk,
            f"收款回执 {desk}/{txn}",
            {
                "txn": txn, "lease_id": lease_id, "fen": fen,
                "authorized": authorized, "note": note,
                "occurred_captured": e.captured,
                "offline_at_occurrence": self._was_offline(desk, e.captured),
            },
            [e.evidence_ref()],
        ))

    def _on_exit_req(self, e: Event) -> None:
        stay = self._require_stay(e.f("stay_id"), e)
        if stay is None:
            return
        desk = e.f("desk")
        stay.exit_requests.append({
            "arrival": e.arrival, "captured": e.captured,
            "desk": desk, "evidence": e.evidence_ref(),
        })
        view = self.evaluate(stay, e.arrival)
        applicable = self._applicable_receipts(stay)
        paid_fen = sum(r.fen for r in applicable)

        if stay.settled:
            # 已在另一岗亭结算：核对付款适用范围与授权，禁止同段再收
            stay.vouchers.append(self._issue(
                stay, "settlement", e, desk,
                f"出口核对（{desk}）：本车已结算，禁止重复收款",
                {
                    "settle_desk": stay.settle_desk,
                    "bill_fen": stay.last_bill_fen, "paid_fen": paid_fen,
                    "balance_fen": paid_fen - (stay.last_bill_fen or 0),
                    "collect_fen": 0,
                    "applicable_receipts": [self._receipt_ref(r) for r in applicable],
                },
                [e.evidence_ref()],
            ))
            return

        if not view["determined"]:
            # 没有确定的付费停放区间：现场放行，待核费用与可能占位分开挂账
            items = [OpenItem(
                code="fee_pending", created_arrival=e.arrival,
                detail=f"{desk} 放行时付费停放区间尚未确定，费用待核",
            )]
            if view["open_intervals"]:
                items.append(OpenItem(
                    code="occupancy_pending", created_arrival=e.arrival,
                    detail="抬杆不等于探头确认驶离，场内仍可能占位："
                           + "；".join(f"{a} 自 {s} 起" for a, s, _ in view["open_intervals"]),
                ))
            stay.open_items.extend(items)
            stay.vouchers.append(self._issue(
                stay, "release", e, desk,
                f"现场放行凭证（{desk}，费用待核）",
                {
                    "determined": False,
                    "pending_items": [it.code for it in items],
                    "known_intervals": view["interval_dicts"],
                    "reason": "尚无确定的付费停放区间，先放行后核费",
                },
                [e.evidence_ref(), *view["evidence"]],
            ))
            return

        # 区间已确定：本岗亭持有效授权才能作为结算岗收款
        lease_ok = self._desk_lease(stay, desk) is not None
        collect = max(0, view["bill_fen"] - paid_fen)
        if lease_ok and collect > 0:
            title = f"结算凭证（{desk}）"
            note = f"本岗亭持有效收款授权，应补收 {collect} 分"
        elif lease_ok:
            title = f"结算凭证（{desk}）"
            note = "本岗亭持有效收款授权，款项已覆盖，无需再收"
        else:
            title = f"出口核对（{desk}）：无收款授权，不予收款"
            note = "该岗亭不持有本车有效付款承诺，不得收取本段费用"
        stay.settled = True
        stay.settle_desk = desk
        stay.last_bill_fen = view["bill_fen"]
        self._resolve_item(stay, "fee_pending", e.arrival, f"由 {desk} 结算凭证承接核费")
        stay.vouchers.append(self._issue(
            stay, "settlement", e, desk, title,
            {
                "determined": True,
                "bill_fen": view["bill_fen"],
                "bill_detail": view["bill_detail"],
                "paid_fen": paid_fen,
                "collect_fen": collect if lease_ok else 0,
                "balance_fen": paid_fen - view["bill_fen"],
                "lease_held": lease_ok,
                "settle_desk": desk,
                "applicable_receipts": [self._receipt_ref(r) for r in applicable],
                "note": note,
            },
            [e.evidence_ref(), *view["evidence"]],
        ))

    def _on_gate(self, e: Event) -> None:
        stay = self._require_stay(e.f("stay_id"), e)
        if stay is None:
            return
        # 抬杆只是岗亭动作：不关闭占位、不结束计费，驶离仍等 spot empty
        stay.gate_actions.append({
            "arrival": e.arrival, "captured": e.captured,
            "action": e.f("action"), "evidence": e.evidence_ref(),
        })

    def _on_link(self, e: Event) -> None:
        self.link_events.append({
            "arrival": e.arrival, "captured": e.captured,
            "source": e.source, "state": e.f("state"),
            "evidence": e.evidence_ref(),
        })

    def _on_ask(self, e: Event) -> None:
        stay = self._require_stay(e.f("stay_id"), e)
        if stay is not None:
            stay.asks.append({"arrival": e.arrival, "evidence": e.evidence_ref()})

    # ------------------------------------------------------------ 证据推断

    def evaluate(self, stay: StayState, at_arrival: int) -> dict[str, Any]:
        """截至 ``at_arrival`` 已到达证据下，该车的实际停放区段与计费视图。"""
        tag = stay.tag
        # 1) 收集本车的占位事实：直接点名的 + 经 correction 认定的 unknown
        facts: list[SpotFact] = []
        for fact in self.spot_facts:
            if fact.arrival > at_arrival:
                continue
            if fact.reported_tag == tag:
                facts.append(fact)
            elif fact.reported_tag == "unknown" and fact.state == "present":
                corr = self._correction_for(fact, at_arrival)
                if corr is not None and corr.tag == tag:
                    facts.append(fact)

        # 2) 每个位组推出开闭区间。成员位空置会关闭覆盖该成员的小组
        #    占位（同一辆车不可能仍停在该组的其他成员上）。补送消息按
        #    captured 现场时刻落位；若到达顺序与现场顺序相反（先收到 empty
        #    才补来 present），标为待核，不静默造区间。
        per_group: dict[str, list[tuple[int, int | None, dict]]] = {}
        for fact in sorted(facts, key=lambda f: (f.captured, f.arrival)):
            slots = per_group.setdefault(fact.group, [])
            if fact.state == "present":
                if not slots or slots[-1][1] is not None:
                    slots.append((fact.captured, None, fact.evidence))
            elif fact.state == "empty":
                if slots and slots[-1][1] is None and slots[-1][0] <= fact.captured:
                    start, _, ev = slots[-1]
                    slots[-1] = (start, fact.captured, ev)
                else:
                    self.anomalies.append({
                        "arrival": fact.arrival, "kind": "evidence_out_of_order",
                        "detail": f"位组 {fact.group} 现场时刻 {fact.captured} 的空置"
                                  f"没有可闭合的在先占位，区间待核",
                        "evidence": fact.evidence,
                    })
                    continue
                # 关闭成员集合相交的其他小组的开口区间
                emptied = set(fact.group.split(","))
                for other_group, other_slots in per_group.items():
                    if other_group == fact.group or not other_slots or other_slots[-1][1] is not None:
                        continue
                    if emptied & set(other_group.split(",")) and other_slots[-1][0] <= fact.captured:
                        start, _, ev = other_slots[-1]
                        other_slots[-1] = (start, fact.captured, ev)
            else:
                raise ValueError(f"未知 spot 状态 {fact.state}")

        # 3) 通道穿越关闭来源区的开口区间（过通道不等于在到区停放）
        for p in stay.passes:
            if p.arrival > at_arrival:
                continue
            for group, slots in per_group.items():
                if self._group_area(group) != p.from_area:
                    continue
                if slots and slots[-1][1] is None and slots[-1][0] <= p.captured:
                    start, _, ev = slots[-1]
                    slots[-1] = (start, p.captured, ev)

        # 4) 同区合并为实际停放区段
        by_area: dict[str, list[list[int]]] = {}
        evidence: list[dict[str, Any]] = []
        for group, slots in per_group.items():
            area = self._group_area(group)
            for start, end, ev in slots:
                evidence.append(ev)
                by_area.setdefault(area, []).append([start, end])
        intervals: list[tuple[str, int, int | None]] = []
        open_intervals: list[tuple[str, int, None]] = []
        for area, raw in by_area.items():
            raw.sort()
            merged: list[list[int | None]] = []
            for start, end in raw:
                if merged and (merged[-1][1] is None or start <= merged[-1][1]):
                    if merged[-1][1] is None:
                        continue  # 已有的开口区间吞掉后续同区区间
                    merged[-1][1] = max(merged[-1][1], end) if end is not None else None
                else:
                    merged.append([start, end])
            for start, end in merged:
                if end is None:
                    open_intervals.append((area, start, None))
                else:
                    intervals.append((area, start, end))
        intervals.sort(key=lambda x: (x[1], x[2]))

        # 5) 仅对费率承诺下的收费区计费，按实际秒占一分钟的比例，累计后一次取整
        bill_num = 0
        bill_detail = []
        interval_dicts = []
        for area, start, end in intervals:
            seconds = end - start
            rate = stay.schedule.rate_of(area)
            interval_dicts.append({
                "area": area, "start": start, "end": end,
                "seconds": seconds, "rate_fen_per_min": rate,
                "policy_id": stay.schedule.policy_id,
            })
            if rate > 0:
                bill_num += rate * seconds
                bill_detail.append({
                    "area": area, "start": start, "end": end,
                    "seconds": seconds, "rate": rate,
                    "raw_fen": f"{rate * seconds}/60",
                })
        for area, start, _ in open_intervals:
            interval_dicts.append({
                "area": area, "start": start, "end": None,
                "rate_fen_per_min": stay.schedule.rate_of(area),
                "policy_id": stay.schedule.policy_id,
            })
        determined = not open_intervals
        return {
            "determined": determined,
            "intervals": intervals,
            "open_intervals": open_intervals,
            "interval_dicts": interval_dicts,
            "bill_detail": bill_detail,
            "bill_numerator": bill_num,
            "bill_fen": round_half_up_fen(bill_num) if determined else None,
            "evidence": evidence,
        }

    def _reconsider_billing(self, stay: StayState, e: Event) -> None:
        """补来的证据改变已结金额：开差额凭证，原凭证与放行事实保留。"""
        if not stay.settled:
            return
        view = self.evaluate(stay, e.arrival)
        if not view["determined"]:
            return
        new_bill = view["bill_fen"]
        old_bill = stay.last_bill_fen
        if new_bill == old_bill:
            return
        paid_fen = sum(r.fen for r in self._applicable_receipts(stay))
        delta = new_bill - old_bill
        stay.last_bill_fen = new_bill
        stay.vouchers.append(self._issue(
            stay, "adjustment", e, stay.settle_desk,
            f"差额更正凭证（证据到达 arrival={e.arrival}）",
            {
                "previous_bill_fen": old_bill,
                "revised_bill_fen": new_bill,
                "bill_delta_fen": delta,
                "paid_fen": paid_fen,
                "net_balance_fen": paid_fen - new_bill,
                "action": ("应补收" if delta > 0 and paid_fen < new_bill else "应退还/挂贷方"),
                "bill_detail": view["bill_detail"],
                "preserved": "原结算凭证与已合法放行事实不变，本凭证只处理差额",
            },
            [e.evidence_ref(), *view["evidence"]],
        ))

    # ------------------------------------------------------------ 辅助

    def _issue(self, stay: StayState, kind: str, e: Event,
               desk: str | None, title: str, payload: dict,
               evidence: list[dict]) -> Voucher:
        self._voucher_seq += 1
        voucher = Voucher(
            id=f"V{self._voucher_seq:03d}", kind=kind, arrival=e.arrival,
            captured=e.captured, stay_id=stay.stay_id, desk=desk,
            title=title, payload=payload, evidence=evidence,
        )
        self.vouchers.append(voucher)
        return voucher

    def _group_area(self, group: str) -> str:
        first = group.split(",")[0]
        if first not in self.group_area:
            raise ValueError(f"未知位组 {group}")
        area = self.group_area[first]
        for member in group.split(","):
            if self.group_area.get(member) != area:
                raise ValueError(f"位组 {group} 跨区域，无法解释")
        return area

    def _correction_for(self, fact: SpotFact, at_arrival: int) -> Correction | None:
        latest = None
        for c in self.corrections:
            if c.arrival > at_arrival:
                continue
            if c.sensor == fact.sensor and c.group == fact.group and c.arrival > fact.arrival:
                latest = c
        return latest

    def _stay_by_tag(self, tag: str, e: Event) -> StayState | None:
        stay_id = self.tag_to_stay.get(tag)
        if stay_id is None:
            self.anomalies.append({
                "arrival": e.arrival, "kind": "unknown_tag",
                "detail": f"车签 {tag} 没有对应入场登记", "evidence": e.evidence_ref(),
            })
            return None
        return self.stays[stay_id]

    def _require_stay(self, stay_id: str, e: Event) -> StayState | None:
        stay = self.stays.get(stay_id)
        if stay is None:
            self.anomalies.append({
                "arrival": e.arrival, "kind": "unknown_stay",
                "detail": f"停车号 {stay_id} 没有入场登记", "evidence": e.evidence_ref(),
            })
        return stay

    def _desk_lease(self, stay: StayState, desk: str) -> Lease | None:
        """该岗亭当前是否持有本车有效、且具离线收款资格的付款承诺。"""
        if not self.paypoints.get(desk, {}).get("offline", False):
            return None
        return next((l for l in stay.leases if l.desk == desk), None)

    def _check_authorization(self, stay: StayState, desk: str,
                             lease_id: str, e: Event) -> tuple[str, bool]:
        lease = next((l for l in stay.leases
                      if l.desk == desk and l.lease_id == lease_id), None)
        if lease is None:
            self.anomalies.append({
                "arrival": e.arrival, "kind": "payment_without_lease",
                "detail": f"{desk} 对 {stay.stay_id} 无对应付款承诺即收款",
                "evidence": e.evidence_ref(),
            })
            return "无对应付款承诺，付款不计入本车结算范围", False
        if not self.paypoints.get(desk, {}).get("offline", False):
            self.anomalies.append({
                "arrival": e.arrival, "kind": "desk_not_offline_capable",
                "detail": f"{desk} 不具备离线收款授权", "evidence": e.evidence_ref(),
            })
            return "岗亭无离线收款授权", False
        if e.captured > lease.until_sec:
            self.anomalies.append({
                "arrival": e.arrival, "kind": "lease_expired",
                "detail": f"承诺 {lease_id} 截至 {lease.until_sec}，付款发生于 {e.captured}",
                "evidence": e.evidence_ref(),
            })
            return "付款承诺已过截止时刻", False
        return f"承诺 {lease_id} 有效（截至 {lease.until_sec}），岗亭具离线收款授权", True

    def _applicable_receipts(self, stay: StayState) -> list[Receipt]:
        """适用于本车结算的收款：授权有效且非重复帧。"""
        return [r for r in stay.receipts if r.authorized and not r.duplicate]

    def _was_offline(self, desk: str, captured: int) -> bool:
        offline = False
        for evt in self.link_events:
            if evt["source"] != desk or evt["captured"] > captured:
                continue
            offline = evt["state"] == "offline"
        return offline

    def _resolve_item(self, stay: StayState, code: str, arrival: int, resolution: str) -> None:
        for item in stay.open_items:
            if item.code == code and item.resolved_arrival is None:
                item.resolved_arrival = arrival
                item.resolution = resolution

    @staticmethod
    def _receipt_ref(r: Receipt) -> dict[str, Any]:
        return {"desk": r.desk, "txn": r.txn, "fen": r.fen,
                "lease_id": r.lease_id, "captured": r.captured}

    # ------------------------------------------------------------ 查询出口

    def dossier(self, stay_id: str) -> dict[str, Any]:
        """财务交班查询：一张凭证链查实际收了哪段、哪一岗亭成为结算依据。"""
        stay = self.stays[stay_id]
        view = self.evaluate(stay, self._last_arrival)
        return {
            "stay_id": stay.stay_id,
            "tag": stay.tag,
            "entry": {
                "gate": stay.entry_gate, "captured": stay.entry_captured,
                "evidence": stay.entry_evidence,
            },
            "rate_commitment": {
                "policy_id": stay.schedule.policy_id,
                "effective_from": stay.schedule.effective_from,
                "rates": stay.schedule.rates,
            },
            "passes": [asdict(p) for p in stay.passes],
            "occupancy_view": {
                "determined": view["determined"],
                "intervals": view["interval_dicts"],
                "bill_detail": view["bill_detail"],
                "current_bill_fen": view["bill_fen"],
            },
            "leases": [asdict(l) for l in stay.leases],
            "receipts": [asdict(r) for r in stay.receipts],
            "exit_requests": stay.exit_requests,
            "gate_actions": stay.gate_actions,
            "open_items": [asdict(i) for i in stay.open_items],
            "settled": stay.settled,
            "settle_desk": stay.settle_desk,
            "vouchers": [asdict(v) for v in stay.vouchers],
        }

    def handover(self) -> dict[str, Any]:
        """交班：未结停放、未核销待办、各班次留下的凭证与差额。"""
        open_stays = []
        for stay_id, stay in self.stays.items():
            unresolved = [asdict(i) for i in stay.open_items if i.resolved_arrival is None]
            view = self.evaluate(stay, self._last_arrival)
            if not stay.settled or unresolved or view["open_intervals"]:
                open_stays.append({
                    "stay_id": stay_id, "tag": stay.tag,
                    "settled": stay.settled,
                    "unresolved_items": unresolved,
                    "open_intervals": view["open_intervals"],
                    "current_bill_fen": view["bill_fen"],
                })
        return {
            "open_stays": open_stays,
            "vouchers": [asdict(v) for v in self.vouchers],
            "anomalies": self.anomalies,
            "last_arrival": self._last_arrival,
        }
