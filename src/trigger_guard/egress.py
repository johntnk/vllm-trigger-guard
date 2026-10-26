"""Literal incremental text guard for the experiment's server/client boundary."""
from dataclasses import dataclass
from types import MappingProxyType


class EgressProgram:
    """Compile once at readiness; requests own only their bounded cursor/state."""
    def __init__(self, rules):
        self.rules = tuple(MappingProxyType(dict(r)) for r in
                           sorted(rules, key=lambda r: (r.get('priority', 0), r['rule_id'])))
        if not self.rules or any(not r['text'] for r in self.rules):
            raise ValueError('nonempty literal rules required')
        self.max_suffix = max(len(r['text']) for r in self.rules)-1
        self.automaton = None
        if len(self.rules) > 32:
            from .core import SparseAutomaton
            self.automaton = SparseAutomaton((r['text'], i) for i,r in enumerate(self.rules))


@dataclass(frozen=True)
class TextDecision:
    safe_text: str
    triggered: bool
    rule_id: str | None


class EgressGuard:
    def __init__(self, rules, notice, program=None):
        self.program = program or EgressProgram(rules)
        self.rules = self.program.rules
        self.max_suffix = self.program.max_suffix
        self.cursor = 0
        self.suffix = ""
        self.terminal = False
        self.notice_sent = False
        self.notice = notice

    def inspect(self, text):
        if self.terminal:
            return TextDecision("", False, None)
        if self.program.automaton is not None:
            program = self.program.automaton
            for pos, char in enumerate(text):
                next_cursor = program.advance(self.cursor, char)
                rank = program.winners[next_cursor]
                if rank is not None:
                    self.terminal = True
                    return TextDecision(text[:pos], True, self.rules[rank]['rule_id'])
                self.cursor = next_cursor
            return TextDecision(text, False, None)
        joined = self.suffix + text
        hit = None
        for rank, rule in enumerate(self.rules):
            start = joined.find(rule["text"])
            if start >= 0:
                completion = start + len(rule["text"]) - 1
                candidate = (completion, rank, rule["rule_id"])
                if hit is None or candidate < hit:
                    hit = candidate
        if hit is not None:
            safe = text[:max(0, hit[0] - len(self.suffix))]
            self.terminal = True
        else:
            safe = text
        self.suffix = ((self.suffix + safe)[-self.max_suffix:]
                       if self.max_suffix else "")
        return TextDecision(safe, hit is not None, hit[2] if hit else None)

    def finish_notice(self):
        if self.notice_sent:
            return ""
        if self.program.automaton is not None:
            cursor = self.cursor
            for char in self.notice:
                cursor = self.program.automaton.advance(cursor, char)
                if self.program.automaton.winners[cursor] is not None:
                    raise ValueError('notice unsafe at published-text boundary')
        elif any(r["text"] in self.suffix + self.notice for r in self.rules):
            raise ValueError("notice unsafe at published-text boundary")
        self.notice_sent = True
        self.terminal = True
        return self.notice
