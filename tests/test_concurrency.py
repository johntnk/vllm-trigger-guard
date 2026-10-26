import asyncio
import importlib.util
import itertools
from pathlib import Path
import random
import sys
import unittest

from reference_oracle import LiteralOracle, token_first
from trigger_guard import core as tg
from trigger_guard.concurrency import Admission, ByteBudget, OutputBuffer, Overloaded, TransportFailure
from trigger_guard.egress import EgressProgram, EgressGuard
from trigger_guard.protocol import Publication


class InterleavingTests(unittest.TestCase):
    def test_HC01_02_03_06_14_100000_operations(self):
        rng=random.Random(42)
        rules=[tg.TriggerRule(f'r{i:03d}',str(i),((i+1,7),)) for i in range(40)]
        matcher=tg.TokenMatcher(rules,128)
        self.assertIsNotNone(matcher.automaton)
        states=[tg.MatcherState() for _ in range(128)]
        histories=[[] for _ in states]
        for step in range(100000):
            k=rng.randrange(len(states));state=states[k]
            if state.terminal or len(histories[k])>48:
                self.assertEqual(matcher.inspect_block(state,[]).next_state,state)
                if state.terminal:self.assertEqual(matcher.inspect_block(state,[1,7]).safe_prefix_length,0)
                states[k]=state=tg.MatcherState();histories[k]=[]
            block=[rng.randrange(1,100) for _ in range(rng.choice([0,1,2,4,8]))]
            prior=len(histories[k]);full=histories[k]+block
            hit=token_first(full,rules)
            decision=matcher.inspect_block(state,block)
            self.assertEqual(decision.triggered,hit is not None)
            self.assertEqual(decision.safe_prefix_length,(hit[0]-prior) if hit else len(block))
            if hit:self.assertEqual(decision.matched_rule_id,hit[1])
            histories[k].extend(block[:decision.safe_prefix_length]);states[k]=decision.next_state
            self.assertEqual(states[k].committed_count,len(histories[k]))

    def test_HC04_05_23_tie_overlap_chunking(self):
        rules=[dict(rule_id=f'r{i:03d}',text=f'Z{i:03d}END') for i in range(40)]
        rules += [dict(rule_id='a',text='花朵',priority=1),dict(rule_id='b',text='朵',priority=0)]
        program=EgressProgram(rules);oracle=LiteralOracle(rules)
        text='𠮷safe花朵tail';expected=oracle.first(text)
        for width in [1,2,3,8,64]:
            guard=EgressGuard(rules,'\nNOTICE',program);out='';hit=None
            for offset in range(0,len(text),width):
                decision=guard.inspect(text[offset:offset+width]);out+=decision.safe_text
                hit=hit or decision.rule_id
            self.assertEqual(out,expected['safe']);self.assertEqual(hit,expected['rule_id'])
            self.assertEqual(guard.finish_notice(),'\nNOTICE');self.assertEqual(guard.finish_notice(),'')

    def test_HC24_26_notice_and_failure(self):
        rules=[dict(rule_id=f'r{i:03d}',text=f'Z{i:03d}END') for i in range(40)]
        rules.append(dict(rule_id='boundary',text='aNOTICE'))
        guard=EgressGuard(rules,'NOTICE',EgressProgram(rules));guard.inspect('a')
        with self.assertRaises(ValueError):guard.finish_notice()
        pub=Publication({'rules':rules,'notice':'\nOK'},'core_egress',EgressProgram(rules))
        with self.assertRaises(RuntimeError):pub.process('unsafe',{'matcher_error':True})
        self.assertEqual(pub.process('later'),[])

    def test_dictionary_program_shared_state_independent(self):
        rules=[dict(rule_id=f'r{i}',text=f'BEGIN{i:04d}END') for i in range(1000)]
        program=EgressProgram(rules)
        a,b=[EgressGuard(rules,'\nNOTICE',program) for _ in range(2)]
        self.assertIs(a.program,b.program)
        a.inspect('BEGIN0000EN');b.inspect('safe')
        self.assertTrue(a.inspect('D').triggered);self.assertFalse(b.inspect('D').triggered)
        with self.assertRaises(TypeError):program.automaton.edges[0]['bad']=1


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_HC27_fifo_token_waiting_and_timeout_cleanup(self):
        a=Admission(engine_limit=1,waiting_limit=1,waiting_tokens=10,inflight_tokens=20)
        first=await a.acquire(10,4,1)
        waiting=asyncio.create_task(a.acquire(10,4,1));await asyncio.sleep(0)
        with self.assertRaises(Overloaded):await a.acquire(1,1,1)
        self.assertEqual(a.snapshot()['waiting'],1)
        a.release_engine(first);second=await waiting
        with self.assertRaises(TimeoutError):await a.acquire(10,4,.001)
        self.assertEqual(a.snapshot()['waiting_tokens'],0)
        a.release_engine(second);a.release_engine(second)
        self.assertEqual(a.inflight,0)

    async def test_HC16_27_cancel_admission_races(self):
        for release_first in (False,True):
            with self.subTest(release_before_cancel=release_first):
                for _ in range(100):
                    a=Admission(engine_limit=1)
                    first=await a.acquire(1,1,1)
                    waiting=asyncio.create_task(a.acquire(1,1,1));await asyncio.sleep(0)
                    self.assertEqual(len(a.waiters),1)
                    if release_first:a.release_engine(first)
                    self.assertTrue(waiting.cancel())
                    with self.assertRaises(asyncio.CancelledError):await waiting
                    if not release_first:a.release_engine(first)
                    self.assertTrue(waiting.cancelled())
                    self.assertEqual(a.inflight,0);self.assertEqual(len(a.waiters),0)
                    self.assertEqual(a.inflight_tokens,0)
                    self.assertEqual(a.waiting_tokens,0);self.assertEqual(a.waiting_bytes,0)
                    recovered=await a.acquire(1,1,1)
                    a.release_engine(recovered)
                    self.assertEqual(a.inflight,0)

    async def test_HC17_25_buffer_terminal_reserved_and_send_accounting(self):
        global_budget=ByteBudget(2048);buffer=OutputBuffer(global_budget,1024)
        buffer.put(b'a'*100);buffer.finish({'notice':'fixed','finish':'stop'})
        self.assertFalse(buffer.put(b'after terminal'))
        self.assertEqual(await buffer.next(),b'a'*100)
        self.assertGreater(global_budget.used,0)
        buffer.sent();self.assertEqual(global_budget.used,0)
        self.assertIsNone(await buffer.next());buffer.close()
        overflow=OutputBuffer(global_budget,512)
        with self.assertRaises(TransportFailure):overflow.put(b'X'*300)
        with self.assertRaises(TransportFailure):await overflow.next()
        overflow.close();self.assertEqual(global_budget.used,0)

    async def test_HC03_17_31_slow_channel_does_not_block_peers(self):
        budget=ByteBudget(32768)
        buffers=[OutputBuffer(budget,2048) for _ in range(32)]
        for channel in buffers:channel.put(b'safe')
        # Leave three consumers completely idle (about 10%).
        for channel in buffers[3:]:
            self.assertEqual(await channel.next(),b'safe');channel.sent()
        for channel in buffers:channel.close()
        self.assertEqual(budget.used,0)


if __name__=='__main__':unittest.main(verbosity=2)
