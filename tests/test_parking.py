"""分区转停、收费承诺、离线补送、更正差额与凭证追溯的规则测试。"""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from device_replay import load_profile, read_frames  # noqa: E402
from parking.engine import ReplayEngine, round_half_up_fen  # noqa: E402
from parking.events import decode_frame  # noqa: E402

PROFILE = load_profile()


def frame(arrival, received, captured, source, msg_type, args):
    return {
        "arrival": arrival, "received": received, "captured": captured,
        "source": source, "type": msg_type,
        "args": list(args),
    }


class EngineBuilder:
    """按 arrival 顺序拼合成帧，便于只构造关心的场景。"""

    def __init__(self):
        self.profile = copy.deepcopy(PROFILE)
        self.frames = []
        self._n = 0

    def add(self, received, captured, source, msg_type, args):
        self._n += 1
        self.frames.append(frame(self._n, received, captured, source, msg_type, args))
        return self

    def engine(self):
        eng = ReplayEngine(self.profile)
        eng.replay(self.frames)
        return eng


def kinds(eng):
    return [v.kind for v in eng.vouchers]


def by_kind(eng, kind):
    return [v for v in eng.vouchers if v.kind == kind]


class WireDecodeTest(unittest.TestCase):
    def test_field_order_and_width_against_profile(self):
        for raw in read_frames():
            event = decode_frame(raw, PROFILE["wire_fields"])
            self.assertEqual(event.arrival, raw["arrival"])
            self.assertEqual(event.evidence_ref()["source"], raw["source"])

    def test_unknown_type_and_bad_width_rejected(self):
        bad = frame(1, 1, 1, "gA", "enter", ["V9"])
        with self.assertRaises(ValueError):
            decode_frame(bad, PROFILE["wire_fields"])
        weird = dict(frame(1, 1, 1, "gA", "teleport", ["x"]))
        with self.assertRaises(ValueError):
            decode_frame(weird, PROFILE["wire_fields"])

    def test_rounding_half_up(self):
        self.assertEqual(round_half_up_fen(30), 1)    # 0.5 分向上
        self.assertEqual(round_half_up_fen(29), 0)
        self.assertEqual(round_half_up_fen(90), 2)    # 1.5 -> 2
        self.assertEqual(round_half_up_fen(-30), -1)


class ArchiveScenarioTest(unittest.TestCase):
    """对真实 17 帧联调资料的端到端核对。"""

    @classmethod
    def setUpClass(cls):
        cls.eng = ReplayEngine(load_profile())
        cls.eng.replay(read_frames())
        cls.d = cls.eng.dossier("P1")

    def test_pass_is_not_parking(self):
        view = self.d["occupancy_view"]
        areas = [(iv["area"], iv["start"], iv["end"]) for iv in view["intervals"]]
        # 80 的 outer->inner 穿越不产生 inner@80 停放，最早占位是更正后的 82
        self.assertEqual(areas, [("outer", 55, 80), ("inner", 82, 137)])

    def test_billing_uses_entry_rate_commitment(self):
        # 120 改价（inner 50）不影响 50 入场的 P1，仍按 40 计
        self.assertEqual(self.d["rate_commitment"]["policy_id"], "baseline")
        self.assertEqual(self.d["occupancy_view"]["current_bill_fen"], 37)

    def test_duplicate_paid_counts_once_and_marked(self):
        receipts = self.d["receipts"]
        self.assertEqual(len(receipts), 2)
        unique = [r for r in receipts if not r["duplicate"]]
        self.assertEqual(len(unique), 1)
        self.assertEqual(sum(r["fen"] for r in unique), 40)

    def test_offline_receipt_marked_by_occurrence_time(self):
        r = next(r for r in self.d["receipts"] if not r["duplicate"])
        self.assertTrue(r["authorized"])
        # captured=125 处于 desk1 离线窗口（115 起），按发生时区域事实计
        self.assertTrue(self.eng._was_offline("desk1", 125))

    def test_second_desk_release_keeps_pending_items_separate(self):
        release = by_kind(self.eng, "release")[0]
        self.assertEqual(release.desk, "desk2")
        self.assertEqual(release.payload["pending_items"],
                         ["fee_pending", "occupancy_pending"])

    def test_gate_open_does_not_close_occupancy(self):
        # 131 抬杆后仍有未闭合占位，137 探头 empty 才关闭
        stay = self.eng.stays["P1"]
        item = next(i for i in stay.open_items if i.code == "occupancy_pending")
        self.assertEqual(item.resolved_arrival, 12)

    def test_settlement_desk_and_no_double_collection(self):
        settle = by_kind(self.eng, "settlement")[0]
        self.assertEqual(settle.desk, "desk1")
        self.assertEqual(settle.payload["bill_fen"], 10)
        self.assertEqual(settle.payload["collect_fen"], 0)  # 40 已覆盖
        self.assertEqual(self.d["settle_desk"], "desk1")

    def test_correction_emits_adjustment_preserving_original(self):
        adj = by_kind(self.eng, "adjustment")[0]
        self.assertEqual(adj.payload["previous_bill_fen"], 10)
        self.assertEqual(adj.payload["revised_bill_fen"], 37)
        self.assertEqual(adj.payload["bill_delta_fen"], 27)
        self.assertEqual(adj.payload["net_balance_fen"], 3)  # 40-37 贷方
        # 原结算与放行凭证仍在链上
        self.assertEqual(kinds(self.eng)[:3], ["release", "receipt", "settlement"])

    def test_evidence_chain_points_to_raw_frames(self):
        adj = by_kind(self.eng, "adjustment")[0]
        arrivals = {ev["arrival"] for ev in adj.evidence}
        self.assertIn(16, arrivals)       # correction 本身
        self.assertIn(5, arrivals)        # 被回溯的 unknown present

    def test_finance_ask_returns_full_traceability(self):
        self.assertEqual(len(self.d["vouchers"]), 4)
        self.assertEqual(self.d["entry"]["evidence"]["arrival"], 1)

    def test_handover_keeps_open_stay_for_next_shift(self):
        hand = self.eng.handover()
        self.assertEqual([s["stay_id"] for s in hand["open_stays"]], ["P2"])
        self.assertFalse(hand["open_stays"][0]["settled"])


class PriceSwitchTest(unittest.TestCase):
    def test_existing_stay_keeps_old_rate_new_stay_uses_new(self):
        b = EngineBuilder()
        # 老车 V1 在改价前入场
        b.add(10, 10, "gA", "enter", ["V1", "P1"])
        b.add(11, 11, "i-s", "spot", ["i1", "present", "V1"])
        # 120 改价
        b.add(12, 120, "ops", "price", ["holiday-close", 120])
        # 新车 V2 改价后入内院
        b.add(13, 121, "gB", "enter", ["V2", "P2"])
        b.add(14, 122, "i-s", "spot", ["i2", "present", "V2"])
        b.add(15, 180, "i-s", "spot", ["i1", "empty", "V1"])
        b.add(16, 182, "i-s", "spot", ["i2", "empty", "V2"])
        eng = b.engine()
        v1 = eng.evaluate(eng.stays["P1"], 16)
        v2 = eng.evaluate(eng.stays["P2"], 16)
        # V1: 169 秒 × 40 = 6760/60 = 112.67 -> 113
        self.assertEqual(v1["bill_fen"], 113)
        # V2: 60 秒 × 50 = 50
        self.assertEqual(v2["bill_fen"], 50)
        self.assertEqual(eng.stays["P1"].schedule.policy_id, "baseline")
        self.assertEqual(eng.stays["P2"].schedule.policy_id, "holiday-close")


class DoubleCollectionTest(unittest.TestCase):
    def test_paid_at_one_desk_cannot_be_collected_again_elsewhere(self):
        b = EngineBuilder()
        b.add(10, 10, "gA", "enter", ["V1", "P1"])
        b.add(11, 12, "i-s", "spot", ["i1", "present", "V1"])
        b.add(12, 20, "desk1", "payment_lease", ["P1", "desk1", "L1", 200])
        b.add(13, 60, "i-s", "spot", ["i1", "empty", "V1"])
        # 60 秒 × 40 = 40 分，desk1 收讫
        b.add(14, 70, "desk1", "paid", ["P1", "L1", "tx1", 40])
        b.add(15, 72, "exit", "exit_req", ["P1", "desk1"])
        # 另一岗亭再看到这辆车
        b.add(16, 80, "exit", "exit_req", ["P1", "desk2"])
        eng = b.engine()
        self.assertEqual(eng.stays["P1"].settle_desk, "desk1")
        second = [v for v in eng.vouchers if v.kind == "settlement"][-1]
        self.assertEqual(second.desk, "desk2")
        self.assertEqual(second.payload["collect_fen"], 0)
        self.assertIn("禁止重复收款", second.title)

    def test_desk_without_lease_never_collects(self):
        b = EngineBuilder()
        b.add(10, 10, "gA", "enter", ["V1", "P1"])
        b.add(11, 12, "i-s", "spot", ["i1", "present", "V1"])
        b.add(12, 60, "i-s", "spot", ["i1", "empty", "V1"])
        b.add(13, 62, "exit", "exit_req", ["P1", "desk2"])  # desk2 无 lease
        eng = b.engine()
        settle = by_kind(eng, "settlement")[0]
        self.assertFalse(settle.payload["lease_held"])
        self.assertEqual(settle.payload["collect_fen"], 0)

    def test_unauthorized_receipt_excluded_from_settlement(self):
        b = EngineBuilder()
        b.add(10, 10, "gA", "enter", ["V1", "P1"])
        b.add(11, 12, "i-s", "spot", ["i1", "present", "V1"])
        b.add(12, 60, "i-s", "spot", ["i1", "empty", "V1"])
        # desk2 没有付款承诺却上报收款
        b.add(13, 61, "desk2", "paid", ["P1", "L?", "tx9", 40])
        b.add(14, 70, "exit", "exit_req", ["P1", "desk2"])
        eng = b.engine()
        settle = by_kind(eng, "settlement")[0]
        self.assertEqual(settle.payload["applicable_receipts"], [])
        self.assertTrue(any(a["kind"] == "payment_without_lease" for a in eng.anomalies))


class LateEvidenceTest(unittest.TestCase):
    def test_delayed_frames_billed_at_occurrence_facts(self):
        b = EngineBuilder()
        b.add(1, 10, "gA", "enter", ["V1", "P1"])
        b.add(2, 12, "i-s", "spot", ["i1", "present", "V1"])
        # 120 改价先到（到达时刻早于补送证据）
        b.add(3, 120, "ops", "price", ["holiday-close", 120])
        # 100 秒时驶离的证据 200 秒才补送到（captured 仍是 100）
        b.add(200, 100, "i-s", "spot", ["i1", "empty", "V1"])
        eng = b.engine()
        view = eng.evaluate(eng.stays["P1"], 4)
        # 承诺在入场（10 秒，baseline）时绑定：88 秒 × 40 = 3520/60 ≈ 59，
        # 不按证据到达时的新价 50 重算（那样会是 73）
        self.assertEqual(eng.stays["P1"].schedule.policy_id, "baseline")
        self.assertEqual(
            [(a, s, e) for a, s, e in view["intervals"]],
            [("inner", 12, 100)],
        )
        self.assertEqual(view["bill_fen"], round_half_up_fen(88 * 40))
        self.assertNotEqual(view["bill_fen"], round_half_up_fen(88 * 50))

    def test_out_of_order_replay_is_flagged_not_silently_billed(self):
        b = EngineBuilder()
        b.add(1, 10, "gA", "enter", ["V1", "P1"])
        # 到达顺序与现场顺序相反：先收到现场 60 的空置，现场 70 的占位掉线后补
        b.add(50, 60, "i-s", "spot", ["i1", "empty", "V1"])
        b.add(80, 70, "i-s", "spot", ["i1", "present", "V1"])
        eng = b.engine()
        view = eng.evaluate(eng.stays["P1"], 3)
        # 逆序空置不闭合区间，占位保持开口并留待核异常
        self.assertFalse(view["determined"])
        self.assertTrue(any(a["kind"] == "evidence_out_of_order" for a in eng.anomalies))

    def test_correction_extends_paid_interval_and_keeps_release(self):
        b = EngineBuilder()
        b.add(1, 10, "gA", "enter", ["V1", "P1"])
        b.add(2, 12, "i-s", "spot", ["i1,i2", "present", "unknown"])
        b.add(3, 50, "i-s", "spot", ["i2", "present", "V1"])
        b.add(4, 55, "exit", "exit_req", ["P1", "desk2"])  # 未确定，放行
        b.add(5, 90, "i-s", "spot", ["i2", "empty", "V1"])
        b.add(6, 95, "ops", "correction", ["i-s", "i1,i2", "V1"])
        eng = b.engine()
        view = eng.evaluate(eng.stays["P1"], 6)
        self.assertEqual(
            [(a, s, e) for a, s, e in view["intervals"] if a == "inner"],
            [("inner", 12, 90)],
        )
        # 放行凭证仍然保留
        self.assertEqual(by_kind(eng, "release")[0].desk, "desk2")


class UndergroundTest(unittest.TestCase):
    def test_under_area_billed_at_its_own_rate(self):
        b = EngineBuilder()
        # 地下临停区车辆从外院入口进
        b.add(1, 10, "gA", "enter", ["V9", "P9"])
        b.add(2, 11, "o-s", "spot", ["o1", "present", "V9"])
        b.add(3, 20, "gA", "pass", ["V9", "outer", "under"])
        b.add(4, 22, "u-s", "spot", ["u1", "present", "V9"])
        b.add(5, 82, "u-s", "spot", ["u1", "empty", "V9"])
        eng = b.engine()
        view = eng.evaluate(eng.stays["P9"], 5)
        # 外院免费；地下 60 秒 × 25 = 25
        self.assertEqual(view["bill_fen"], 25)
        self.assertEqual([d["area"] for d in view["bill_detail"]], ["under"])


class LeaseWindowTest(unittest.TestCase):
    def test_payment_after_lease_until_is_rejected(self):
        b = EngineBuilder()
        b.add(1, 10, "gA", "enter", ["V1", "P1"])
        b.add(2, 12, "i-s", "spot", ["i1", "present", "V1"])
        b.add(3, 20, "desk1", "payment_lease", ["P1", "desk1", "L1", 100])
        b.add(4, 60, "i-s", "spot", ["i1", "empty", "V1"])
        b.add(5, 101, "desk1", "paid", ["P1", "L1", "tx1", 40])
        eng = b.engine()
        r = eng.stays["P1"].receipts[0]
        self.assertFalse(r.authorized)
        self.assertTrue(any(a["kind"] == "lease_expired" for a in eng.anomalies))


if __name__ == "__main__":
    unittest.main()
