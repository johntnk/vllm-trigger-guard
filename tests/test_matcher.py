import importlib.util
import itertools
from pathlib import Path
import random
import sys
import unittest

from trigger_guard import core as tg
from trigger_guard.egress import EgressGuard


def matcher(sequences=((1, 2),)):
    return tg.TokenMatcher([tg.TriggerRule("r", "synthetic", sequences)], 100)


def reference(tokens, rules):
    # Independent brute-force reference: scans every generated prefix.
    for end in range(1, len(tokens) + 1):
        for rule in sorted(rules, key=lambda r: (r.priority, r.rule_id)):
            if any(tuple(tokens[max(0, end-len(s)):end]) == s
                   for s in rule.token_sequences):
                return tokens[:end-1], rule.rule_id
    return tokens, None


class MatcherTests(unittest.TestCase):
    def test_U01_multitoken_candidate_not_committed(self):
        m = matcher()
        a = m.inspect_block(tg.MatcherState(), [7, 1])
        b = m.inspect_block(a.next_state, [2, 9])
        self.assertEqual((b.triggered, b.safe_prefix_length, b.match_end_output_index), (True, 0, 2))
        self.assertEqual(b.next_state.suffix, (1,))
        self.assertEqual(b.next_state.committed_count, 2)

    def test_U02_same_prefix_different_candidate(self):
        self.assertFalse(matcher().inspect_block(tg.MatcherState(), [1, 3]).triggered)

    def test_U03_single_token(self):
        m = matcher(((5,),))
        self.assertFalse(m.inspect_block(tg.MatcherState(), [1, 3]).triggered)
        self.assertEqual(m.inspect_block(tg.MatcherState(), [1, 5]).safe_prefix_length, 1)
        self.assertEqual(m.inspect_block(tg.MatcherState(), [5]).safe_prefix_length, 0)

    def test_U04_overlap_and_deterministic_tie(self):
        rules = [tg.TriggerRule("b", "b", ((2,),), 0),
                 tg.TriggerRule("a", "a", ((1, 2),), 0),
                 tg.TriggerRule("z", "z", ((1, 2),), -1)]
        for perm in itertools.permutations(rules):
            d = tg.TokenMatcher(perm, 10).inspect_block(tg.MatcherState(), [1, 2, 2])
            self.assertEqual((d.safe_prefix_length, d.matched_rule_id), (1, "z"))

    def test_U05_across_many_blocks(self):
        m = matcher(((1, 2, 3, 4),))
        state = tg.MatcherState()
        for t in [1, 2, 3]:
            state = m.inspect_block(state, [t]).next_state
        self.assertTrue(m.inspect_block(state, [4, 9]).triggered)

    def test_U06_empty_and_padding_rejected(self):
        m = matcher()
        state = tg.MatcherState((1,), 1)
        self.assertEqual(m.inspect_block(state, []).next_state, state)
        for bad in [-1, 100, True, 2.0]:
            with self.assertRaises(ValueError):
                m.inspect_block(state, [bad])

    def test_U07_all_small_partitions(self):
        m = matcher(((1, 2), (3, 4, 5)))
        for seq in ([7, 1, 2, 8], [3, 4, 5, 1, 2], [7, 1, 3, 9]):
            expected = reference(list(seq), [tg.TriggerRule("r", "x", ((1, 2), (3, 4, 5)))])[0]
            for mask in range(1 << (len(seq)-1)):
                state, got, start = tg.MatcherState(), [], 0
                cuts = [i+1 for i in range(len(seq)-1) if mask & (1 << i)] + [len(seq)]
                for end in cuts:
                    block = seq[start:end]
                    d = m.inspect_block(state, block)
                    got.extend(block[:d.safe_prefix_length])
                    state, start = d.next_state, end
                self.assertEqual(got, expected)

    def test_U08_prompt_is_not_matcher_input(self):
        prompt = [1, 2]
        self.assertTrue(prompt)
        self.assertFalse(matcher().inspect_block(tg.MatcherState(), [9, 2]).triggered)

    def test_U09_random_reference_seed42(self):
        rng = random.Random(42)
        for _ in range(2000):
            rules = [tg.TriggerRule(str(i), "x", (tuple(rng.randrange(5) for _ in range(rng.randrange(1, 5))),), rng.randrange(3)) for i in range(5)]
            m = tg.TokenMatcher(rules, 10)
            seq = [rng.randrange(5) for _ in range(rng.randrange(30))]
            expected, rule = reference(seq, rules)
            state, got, i, hit = tg.MatcherState(), [], 0, None
            while i < len(seq):
                b = seq[i:i+rng.randrange(1, 8)]
                d = m.inspect_block(state, b)
                got.extend(b[:d.safe_prefix_length])
                if d.triggered:
                    hit = d.matched_rule_id
                state = d.next_state
                i += len(b)
            self.assertEqual((got, hit), (expected, rule))

    def test_U10_interleaved_requests_and_late_blocks(self):
        m = matcher()
        a = m.inspect_block(tg.MatcherState(), [1]).next_state
        b = m.inspect_block(tg.MatcherState(), [3]).next_state
        done = m.inspect_block(a, [2]).next_state
        self.assertFalse(m.inspect_block(b, [2]).triggered)
        late = m.inspect_block(done, [7, 8])
        self.assertEqual(late.safe_prefix_length, 0)
        self.assertFalse(late.triggered)
        self.assertEqual(late.next_state, done)
        self.assertFalse(m.inspect_block(tg.MatcherState(), [2]).triggered)

    def test_U11_configuration_errors(self):
        for seq in [(), (-1,), (100,), (True,), (1.5,)]:
            with self.assertRaises(ValueError):
                matcher((seq,))
        with self.assertRaises(ValueError):
            tg.TokenMatcher([tg.TriggerRule("r", "r", ((5,),))], 10, [5])
        with self.assertRaises(ValueError):
            tg.TokenMatcher([], 100)
        with self.assertRaises(ValueError):
            tg.TokenMatcher([tg.TriggerRule("r", "", ((5,),))], 10)

    def test_U12_notice_boundary(self):
        with self.assertRaises(ValueError):
            tg.validate_notice(["abc"], "bc--notice")
        with self.assertRaises(ValueError):
            tg.validate_notice(["abc"], "notice abc")
        tg.validate_notice(["花朵"], "\n\n【提示】我不能继续了。")
        guard = EgressGuard([{"text": "abc", "rule_id": "r"}], "bc")
        guard.inspect("a")
        with self.assertRaises(ValueError):
            guard.finish_notice()

    def test_M01_rejected_draft_not_inspected(self):
        draft, validated = [1, 2], [1, 3]
        self.assertTrue(matcher().inspect_block(tg.MatcherState(), draft).triggered)
        self.assertFalse(matcher().inspect_block(tg.MatcherState(), validated).triggered)

    def test_M02_M03_M04_validated_origins(self):
        for origin in ["accepted", "recovered", "bonus"]:
            with self.subTest(evidence="replay", origin=origin):
                d = matcher().inspect_block(tg.MatcherState(), [1, 2, 9])
                self.assertEqual(d.safe_prefix_length, 1)

    def test_E11_literal_path_and_all_chunk_splits(self):
        text = "这里有花朵和草"
        for cut in range(len(text)+1):
            g = EgressGuard([{"rule_id": "r", "text": "花朵"}], "\n提示")
            a, b = g.inspect(text[:cut]), g.inspect(text[cut:])
            self.assertEqual(a.safe_text + b.safe_text, "这里有花")
            self.assertNotIn("花朵", a.safe_text + b.safe_text + g.finish_notice())
            self.assertEqual(g.finish_notice(), "")
            self.assertEqual(g.inspect("extra").safe_text, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
