"""临时开放院落的分区转停、收费与凭证交割。

只依赖 Python 3.11 标准库。模块划分：

- ``parking.events``   ：按现场协议把原始帧解码为具名事件。
- ``parking.schedule`` ：费率政策时间线与入场费率承诺。
- ``parking.engine``   ：按接收顺序回放事件的事件溯源引擎，出具凭证。
"""
from parking.engine import ReplayEngine, round_half_up_fen
from parking.events import decode_frame
from parking.schedule import PolicyTimeline

__all__ = ["ReplayEngine", "PolicyTimeline", "decode_frame", "round_half_up_fen"]
