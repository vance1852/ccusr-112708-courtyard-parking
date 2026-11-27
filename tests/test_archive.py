"""核对本地记录在装载时完整保留了协议字段与输入顺序。"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from device_replay import ROOT, load_profile, read_frames


class DeviceArchiveTest(unittest.TestCase):
    def test_envelopes_and_payload_widths(self):
        profile = load_profile()
        frames = list(read_frames())
        self.assertEqual(len(frames), 17)
        self.assertEqual(set(profile["wire_fields"]), {f["type"] for f in frames})
        self.assertEqual([f["arrival"] for f in frames], list(range(1, len(frames) + 1)))
        self.assertEqual([f["received"] for f in frames], sorted(f["received"] for f in frames))
        for frame in frames:
            with self.subTest(arrival=frame["arrival"]):
                self.assertIsInstance(frame["captured"], int)
                self.assertTrue(frame["source"])
                self.assertEqual(len(profile["wire_fields"][frame["type"]].split()), len(frame["args"]))

    def test_cli_matches_raw_archive(self):
        run = subprocess.run(
            [sys.executable, "device_replay.py"],
            cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual([json.loads(line) for line in run.stdout.splitlines()], list(read_frames()))

    def test_related_device_evidence_is_present_in_reception_order(self):
        frames = list(read_frames())
        indices = [3, 4, 7, 8]
        self.assertEqual([
            (frames[i]["source"], frames[i]["type"], frames[i]["args"][0]) for i in indices
        ], [('gB', 'pass', 'V1'), ('i-s', 'spot', 'i1,i2'), ('ops', 'price', 'holiday-close'), ('i-s', 'spot', 'i2')])
        indices = [5, 6, 9, 11, 12, 13]
        self.assertEqual([
            (frames[i]["source"], frames[i]["type"], frames[i]["args"][0]) for i in indices
        ], [('desk1', 'payment_lease', 'P1'), ('desk1', 'link', 'offline'), ('exit', 'exit_req', 'P1'), ('i-s', 'spot', 'i2'), ('desk1', 'paid', 'P1'), ('desk1', 'paid', 'P1')])
        indices = [10, 14, 15, 16]
        self.assertEqual([
            (frames[i]["source"], frames[i]["type"], frames[i]["args"][0]) for i in indices
        ], [('exit', 'gate', 'P1'), ('exit', 'exit_req', 'P1'), ('ops', 'correction', 'i-s'), ('finance', 'ask', 'P1')])
