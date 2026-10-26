import asyncio
import sys
from pathlib import Path
import unittest

from trigger_guard.protocol import Publication


POLICY = {"rules": [{"rule_id": "r", "text": "花朵"}], "notice": "\n\n【提示】我不能继续了。"}


class ProtocolTests(unittest.TestCase):
    def test_E07_exact_notice_finish_order(self):
        p = Publication(POLICY, "core_egress")
        first = p.process("花")
        final = p.process("", {"triggered": True}, "stop")
        self.assertEqual(first + final, [("model", "花"), ("notice", POLICY["notice"]), ("finish", "stop")])
        self.assertEqual(p.process("extra", {"triggered": True}, "stop"), [])
        self.assertEqual(p.notice_count, 1)

    def test_E08_nonstream_is_same_concatenation(self):
        p = Publication(POLICY, "core_egress")
        events = p.process("这里有花朵和更多")
        self.assertEqual("".join(v for k, v in events if k != "finish"), "这里有花"+POLICY["notice"])
        self.assertEqual(p.egress_only_hits, 1)

    def test_E11_egress_covers_token_matcher_miss(self):
        p = Publication(POLICY, "core_egress")
        a = p.process("花")
        b = p.process("朵")
        self.assertEqual(a[0], ("model", "花"))
        self.assertEqual(b, [("notice", POLICY["notice"]), ("finish", "stop")])
        self.assertEqual((p.core_hits, p.egress_only_hits), (0, 1))

    def test_E01_normal_eos_or_length_no_notice(self):
        for mode in ["off", "core", "egress", "core_egress"]:
            for reason in ["stop", "length"]:
                p = Publication(POLICY, mode)
                self.assertEqual(p.process("花费", finish_reason=reason), [("model", "花费"), ("finish", reason)])
                self.assertEqual(p.notice_count, 0)

    def test_client_cancellation_not_on_notice_path(self):
        # The publication method has no await/IO: visibility need not wait for abort.
        p = Publication(POLICY, "egress")
        self.assertEqual(p.process("花朵")[1][0], "notice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
