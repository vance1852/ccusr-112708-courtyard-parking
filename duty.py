"""岗亭值班入口：回放设备帧、办理转停收费、出具可追溯凭证。

用法（标准库即可，建议以隔离模式运行）::

    python3 -I duty.py replay                 # 全量回放，逐帧列出新出具的凭证
    python3 -I duty.py dossier P1             # 财务交班：查一次停放收了哪段、谁结算
    python3 -I duty.py handover               # 交班清单：未结停放与待核占位/费用
    python3 -I duty.py replay --json          # 结构化输出
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from device_replay import ROOT, load_profile, read_frames  # noqa: E402
from parking.engine import ReplayEngine  # noqa: E402


def build_engine(root: Path = ROOT) -> ReplayEngine:
    engine = ReplayEngine(load_profile(root))
    engine.replay(read_frames(root))
    return engine


def _print_voucher(voucher) -> None:
    print(f"[{voucher.id}] {voucher.kind:<10} arrival={voucher.arrival} "
          f"captured={voucher.captured} 停放={voucher.stay_id} 岗亭={voucher.desk}")
    print(f"    {voucher.title}")
    for key, value in voucher.payload.items():
        print(f"      - {key}: {json.dumps(value, ensure_ascii=False)}")
    refs = ", ".join(f"{ev['source']}:{ev['type']}@arrival{ev['arrival']}"
                     f"(现场{ev['captured']})" for ev in voucher.evidence)
    print(f"    证据链: {refs}")


def cmd_replay(engine: ReplayEngine, as_json: bool) -> None:
    if as_json:
        print(json.dumps([asdict(v) for v in engine.vouchers],
                         ensure_ascii=False, indent=2))
        return
    print(f"共处理至 arrival={engine._last_arrival}，出具凭证 {len(engine.vouchers)} 张：\n")
    for voucher in engine.vouchers:
        _print_voucher(voucher)
        print()
    if engine.anomalies:
        print("异常记录：")
        for item in engine.anomalies:
            print(f"  - arrival{item['arrival']} {item['kind']}: {item['detail']}")


def cmd_dossier(engine: ReplayEngine, stay_id: str, as_json: bool) -> None:
    if stay_id not in engine.stays:
        raise SystemExit(f"未找到停车号 {stay_id}")
    data = engine.dossier(stay_id)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"停放档案 {stay_id}（车签 {data['tag']}）")
    print(f"  入场: {data['entry']['gate']} @现场{data['entry']['captured']} "
          f"证据 arrival={data['entry']['evidence']['arrival']}")
    rc = data["rate_commitment"]
    print(f"  费率承诺: 政策 {rc['policy_id']}（自 {rc['effective_from']} 起）费率 {rc['rates']}")
    print("  通道穿越（仅经过，不计停放）:")
    for p in data["passes"]:
        print(f"    - {p['from_area']}->{p['to_area']} @现场{p['captured']}")
    print("  实际停放区段:")
    for iv in data["occupancy_view"]["intervals"]:
        end = "未闭合" if iv["end"] is None else iv["end"]
        print(f"    - {iv['area']}: {iv['start']} -> {end} "
              f"({iv['seconds'] if iv['end'] is not None else '?'} 秒, "
              f"费率 {iv['rate_fen_per_min']} 分/分, 政策 {iv['policy_id']})")
    print("  计费明细（按入场承诺费率，累计后四舍五入到分）:")
    for d in data["occupancy_view"]["bill_detail"]:
        print(f"    - {d['area']} {d['start']}->{d['end']} {d['seconds']}秒 "
              f"× {d['rate']}分/分 = {d['raw_fen']}")
    print(f"  当前应付: {data['occupancy_view']['current_bill_fen']} 分")
    print("  收款:")
    for r in data["receipts"]:
        flag = "重复帧" if r["duplicate"] else ("有效" if r["authorized"] else "未授权")
        print(f"    - {r['desk']}/{r['txn']} {r['fen']}分 [{flag}] {r['note']}")
    print(f"  结算岗亭: {data['settle_desk'] or '尚未结算'}")
    print("  待办:")
    for item in data["open_items"]:
        status = f"已结 arrival={item['resolved_arrival']}（{item['resolution']}）" \
            if item["resolved_arrival"] else "未结"
        print(f"    - [{item['code']}] {item['detail']} -> {status}")
    print("  凭证链:")
    for v in data["vouchers"]:
        print(f"    - [{v['id']}] {v['kind']} {v['title']}（岗亭 {v['desk']}）")


def cmd_handover(engine: ReplayEngine, as_json: bool) -> None:
    data = engine.handover()
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"交班快照（截至 arrival={data['last_arrival']}）")
    print("未结/待核停放：")
    for stay in data["open_stays"]:
        print(f"  - {stay['stay_id']}（{stay['tag']}）已结算={stay['settled']} "
              f"当前应付={stay['current_bill_fen']}分")
        for item in stay["unresolved_items"]:
            print(f"      待办 {item['code']}: {item['detail']}")
        for area, start, _ in stay["open_intervals"]:
            print(f"      未闭合占位: {area} 自现场 {start}")
    if not data["open_stays"]:
        print("  （无）")
    print(f"凭证累计 {len(data['vouchers'])} 张，异常 {len(data['anomalies'])} 条。")


def main() -> None:
    parser = argparse.ArgumentParser(description="机关院落临时开放：分区转停收费与凭证交割")
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    p_replay = sub.add_parser("replay", help="按接收顺序回放全部设备帧并列出凭证")
    p_replay.add_argument("--json", action="store_true")
    p_dossier = sub.add_parser("dossier", help="查一次停放的完整档案与凭证链")
    p_dossier.add_argument("stay_id")
    p_dossier.add_argument("--json", action="store_true")
    p_handover = sub.add_parser("handover", help="财务交班清单")
    p_handover.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine = build_engine(args.root)
    if args.command == "replay":
        cmd_replay(engine, args.json)
    elif args.command == "dossier":
        cmd_dossier(engine, args.stay_id, args.json)
    elif args.command == "handover":
        cmd_handover(engine, args.json)


if __name__ == "__main__":
    main()
