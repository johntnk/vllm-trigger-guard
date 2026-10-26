"""Pure publication state machine, shared by HTTP adapter and replay tests."""
from .egress import EgressGuard


class Publication:
    def __init__(self, policy, mode, program=None):
        if mode not in {"off", "core", "egress", "core_egress"}:
            raise ValueError("invalid publication mode")
        self.mode = mode
        self.guard = EgressGuard(policy["rules"], policy["notice"], program=program)
        self.done = False
        self.core_hits = self.egress_only_hits = self.notice_count = 0

    def process(self, text, core_event=None, finish_reason=None):
        """Return ordered ordinary text / finish events; no network assumptions."""
        if self.done:
            return []
        if core_event and core_event.get('matcher_error'):
            self.done = True
            raise RuntimeError('core matcher failed closed')
        core_hit = bool(core_event and core_event.get("triggered"))
        egress_hit = False
        if self.mode in {"egress", "core_egress"}:
            decision = self.guard.inspect(text)
            text, egress_hit = decision.safe_text, decision.triggered
        else:
            n = self.guard.max_suffix
            self.guard.suffix = (self.guard.suffix + text)[-n:] if n else ""
            if self.guard.program.automaton is not None:
                for char in text:
                    self.guard.cursor = self.guard.program.automaton.advance(self.guard.cursor, char)
        result = [("model", text)] if text else []
        if core_hit or egress_hit:
            self.core_hits += int(core_hit)
            self.egress_only_hits += int(egress_hit and not core_hit)
            result.append(("notice", self.guard.finish_notice()))
            self.notice_count += 1
            finish_reason = "stop"
        if finish_reason is not None:
            self.done = True
            result.append(("finish", finish_reason))
        return result
