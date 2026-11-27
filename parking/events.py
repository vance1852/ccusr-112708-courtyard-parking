"""设备帧解码：按 ``site.json`` 的 ``wire_fields`` 字段顺序把 args 还原为具名事件。

解码只负责协议层，不做任何业务推断。每类事件保留原始帧引用，凭证需要
回溯设备证据时可以直接查到 ``arrival``、``source``、``captured`` 与原值。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 消息类型 -> 协议字段名（与 site.json wire_fields 的中文顺序对应）
FIELD_NAMES: dict[str, tuple[str, ...]] = {
    "enter": ("tag", "stay_id"),
    "pass": ("tag", "from_area", "to_area"),
    "spot": ("group", "state", "tag"),
    "price": ("policy_id", "start_sec"),
    "exit_req": ("stay_id", "desk"),
    "payment_lease": ("stay_id", "desk", "lease_id", "until_sec"),
    "paid": ("stay_id", "lease_id", "txn", "fen"),
    "gate": ("stay_id", "action"),
    "link": ("state",),
    "correction": ("sensor", "group", "tag"),
    "ask": ("stay_id",),
}


@dataclass(frozen=True, slots=True)
class Event:
    arrival: int
    received: int
    captured: int
    source: str
    type: str
    fields: dict[str, Any]
    raw: dict[str, Any]

    def f(self, name: str) -> Any:
        return self.fields[name]

    def evidence_ref(self) -> dict[str, Any]:
        """写入凭证的设备证据指针。"""
        return {
            "arrival": self.arrival,
            "source": self.source,
            "type": self.type,
            "captured": self.captured,
            "received": self.received,
        }


def decode_frame(frame: dict[str, Any], wire_fields: dict[str, str] | None = None) -> Event:
    """按协议字段顺序解码一帧。

    ``wire_fields`` 缺省时只按内置字段名校验；传入现场配置时同时核对
    配置中声明的字段数量，防止协议与解码器漂移。
    """
    msg_type = frame["type"]
    if msg_type not in FIELD_NAMES:
        raise ValueError(f"未知设备消息类型: {msg_type}")
    names = FIELD_NAMES[msg_type]
    args = frame["args"]
    if len(names) != len(args):
        raise ValueError(f"{msg_type} 载荷宽度 {len(args)} 与协议 {len(names)} 不符")
    if wire_fields is not None:
        if len(wire_fields[msg_type].split()) != len(args):
            raise ValueError(f"{msg_type} 与 site.json wire_fields 声明宽度不符")
    return Event(
        arrival=frame["arrival"],
        received=frame["received"],
        captured=frame["captured"],
        source=frame["source"],
        type=msg_type,
        fields=dict(zip(names, args)),
        raw=frame,
    )
