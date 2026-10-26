# SPDX-License-Identifier: Apache-2.0
"""Experimental post-verification token guard. Pure CPU, no vLLM dependencies.

TOKEN_SEQUENCE is deliberately narrower than literal-text filtering. Candidate
blocks MUST have been validated and had native-unreachable positions excluded.
"""
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from collections import deque
from types import MappingProxyType
import hashlib
import os

IMPLEMENTATION_VERSION = "trigger-guard-v2.1"
MAX_RULES = 100000
MAX_RULE_CHARACTERS = 16000000
MAX_RULE_LENGTH = 4096
MAX_RULE_TOKENS = 8000000
MAX_PROGRAM_STATES = 4000000


class SparseAutomaton:
    """Immutable sparse Aho-Corasick program; only the cursor is per request.

    Patterns carry integer ranks (priority then rule_id). Failure outputs retain
    only the lowest rank: the API needs the first completion, not every match.
    """
    def __init__(self, patterns):
        edges, failure, winners = [{}], [0], [None]
        for sequence, rank in patterns:
            node = 0
            for symbol in sequence:
                child = edges[node].get(symbol)
                if child is None:
                    if len(edges) >= MAX_PROGRAM_STATES:
                        raise ValueError("POLICY_BUILD_BUDGET: automaton states")
                    child = len(edges)
                    edges[node][symbol] = child
                    edges.append({}); failure.append(0); winners.append(None)
                node = child
            if winners[node] is None or rank < winners[node]:
                winners[node] = rank
        queue = deque(edges[0].values())
        while queue:
            node = queue.popleft()
            for symbol, child in edges[node].items():
                queue.append(child)
                fallback = failure[node]
                while fallback and symbol not in edges[fallback]:
                    fallback = failure[fallback]
                failure[child] = edges[fallback].get(symbol, 0)
                inherited = winners[failure[child]]
                if inherited is not None and (winners[child] is None or inherited < winners[child]):
                    winners[child] = inherited
        self.edges = tuple(MappingProxyType(e) for e in edges)
        self.failure = tuple(failure)
        self.winners = tuple(winners)

    def advance(self, cursor, symbol):
        while cursor and symbol not in self.edges[cursor]:
            cursor = self.failure[cursor]
        return self.edges[cursor].get(symbol, 0)


@dataclass(frozen=True)
class TriggerRule:
    rule_id: str
    text: str
    token_sequences: tuple[tuple[int, ...], ...]
    priority: int = 0


@dataclass(frozen=True)
class MatcherState:
    suffix: tuple[int, ...] = ()
    committed_count: int = 0
    terminal: bool = False
    matched_rule_id: str | None = None
    cursor: int = 0


@dataclass(frozen=True)
class TriggerDecision:
    triggered: bool
    safe_prefix_length: int
    matched_rule_id: str | None
    match_end_output_index: int | None
    next_state: MatcherState


class TokenMatcher:
    def __init__(self, rules, vocab_size, special_ids=()):
        if type(vocab_size) is not int or vocab_size < 1:
            raise ValueError("invalid vocabulary size")
        self.vocab_size = vocab_size
        self.by_last = {}
        self.max_suffix = 0
        seen = set()
        special_ids = frozenset(special_ids)
        rules = sorted(rules, key=lambda r: (r.priority, r.rule_id))
        if len(rules) > MAX_RULES:
            raise ValueError("CONFIG_LIMIT: rule count")
        token_total = sum(len(s) for r in rules for s in r.token_sequences)
        if token_total > MAX_RULE_TOKENS:
            raise ValueError("CONFIG_LIMIT: total rule tokens")
        for rule in rules:
            if not rule.rule_id or rule.rule_id in seen or not rule.text:
                raise ValueError("rule IDs must be unique and text nonempty")
            if type(rule.priority) is not int or not rule.token_sequences:
                raise ValueError("invalid priority or empty rule sequences")
            seen.add(rule.rule_id)
            for seq in sorted(set(rule.token_sequences)):
                if not seq or any(type(t) is not int or t < 0 or t >= vocab_size
                                  or t in special_ids for t in seq):
                    raise ValueError("empty, invalid, or special token sequence")
                self.max_suffix = max(self.max_suffix, len(seq) - 1)
                self.by_last.setdefault(seq[-1], []).append((seq, rule.rule_id))
        if not seen:
            raise ValueError("at least one rule is required")
        self.by_last = MappingProxyType({k: tuple(v) for k,v in self.by_last.items()})
        self.automaton = None
        self.rule_ids = tuple(r.rule_id for r in rules)
        if max(map(len, self.by_last.values())) > 32:
            self.automaton = SparseAutomaton((seq, rank) for rank,r in enumerate(rules)
                                            for seq in set(r.token_sequences))

    def inspect_block(self, state, effective_token_ids):
        if state.terminal:
            return TriggerDecision(False, 0, None, None, state)
        suffix, count = state.suffix, state.committed_count
        cursor = state.cursor
        for pos, token in enumerate(effective_token_ids):
            if type(token) is not int or not 0 <= token < self.vocab_size:
                raise ValueError("matcher only accepts validated, unpadded token IDs")
            if self.automaton is not None:
                next_cursor = self.automaton.advance(cursor, token)
                rank = self.automaton.winners[next_cursor]
                if rank is not None:
                    rule_id = self.rule_ids[rank]
                    next_state = MatcherState((), count, True, rule_id, cursor)
                    return TriggerDecision(True, pos, rule_id, count, next_state)
                cursor = next_cursor
                count += 1
                continue
            for seq, rule_id in self.by_last.get(token, ()):
                prefix = seq[:-1]
                if not prefix or (len(suffix) >= len(prefix)
                                  and suffix[-len(prefix):] == prefix):
                    next_state = MatcherState(suffix, count, True, rule_id)
                    return TriggerDecision(True, pos, rule_id, count, next_state)
            if self.max_suffix:
                suffix = (suffix + (token,))[-self.max_suffix:]
            count += 1
        return TriggerDecision(False, len(effective_token_ids), None, None,
                               MatcherState(suffix, count, cursor=cursor))


def validate_notice(words, notice):
    if not isinstance(notice, str) or not notice:
        raise ValueError("a nonempty server notice is required")
    for word in words:
        if word in notice:
            raise ValueError("notice contains a trigger")
        # Reject any notice that could complete a word across the boundary,
        # for ANY already-published safe prefix. No output rollback is needed.
        for split in range(1, len(word)):
            if notice.startswith(word[split:]):
                raise ValueError("notice can complete a trigger across its boundary")


def load_policy(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("match_semantics") != "literal_substring":
        raise ValueError("only exact literal_substring semantics are defined")
    if any(not isinstance(data.get(k), str) or not data[k]
           for k in ("policy_version", "notice_id")):
        raise ValueError("policy_version and notice_id are required")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("nonempty rules are required")
    if len(rules) > MAX_RULES:
        raise ValueError("CONFIG_LIMIT: rule count")
    seen = set()
    for rule in rules:
        if (not isinstance(rule.get("text"), str) or not rule["text"]
                or not isinstance(rule.get("rule_id"), str) or not rule["rule_id"]
                or rule["rule_id"] in seen or type(rule.get("priority", 0)) is not int):
            raise ValueError("invalid or duplicate rule")
        seen.add(rule["rule_id"])
    if (sum(len(r['text']) for r in rules) > MAX_RULE_CHARACTERS
            or max(len(r['text']) for r in rules) > MAX_RULE_LENGTH):
        raise ValueError("CONFIG_LIMIT: literal character budget")
    validate_notice([r["text"] for r in rules], data.get("notice"))
    return data


def compile_policy(policy, tokenizer):
    """Encode once at server initialization; preserve exact configured spaces."""
    special = set(tokenizer.all_special_ids)
    vocab_size = max(tokenizer.get_vocab().values()) + 1
    rules = []
    for rule in policy["rules"]:
        seqs = sorted({tuple(tokenizer.encode(prefix + rule["text"],
                                             add_special_tokens=False))
                       for prefix in ("", " ")})
        rules.append({**rule, "token_sequences": seqs})
    compiled = {"policy_version": policy["policy_version"],
                "notice_id": policy["notice_id"],
                "vocab_size": vocab_size, "special_ids": sorted(special),
                "rules": rules}
    compiled['implementation_version'] = IMPLEMENTATION_VERSION
    compiled['match_semantics'] = policy['match_semantics']
    compiled['tokenizer_fingerprint'] = hashlib.sha256(json.dumps(
        {'vocab': sorted(tokenizer.get_vocab().items()), 'special': sorted(special)},
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    serialized = json.dumps(compiled, sort_keys=True, ensure_ascii=False)
    matcher_from_json(serialized)  # validate before any request is accepted
    return serialized


def policy_reference(serialized):
    """Trusted startup-only registry; carry a digest across request RPC, not N rules.

    Active requests retain immutable matcher objects. The bounded process cache
    can evict an unused policy without deleting an active request's reference.
    """
    directory = os.environ.get('TG_POLICY_REGISTRY_DIR')
    if not directory:
        return serialized
    digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    path = Path(directory) / (digest + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('policy registry digest mismatch')
    else:
        with path.open('x', encoding='utf-8', newline='') as f:
            f.write(serialized)
    return 'tg2:' + digest


@lru_cache(maxsize=2)
def matcher_from_json(serialized):
    if serialized.startswith('tg2:'):
        digest = serialized[4:]
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid trusted policy reference')
        path = Path(os.environ['TG_POLICY_REGISTRY_DIR']) / (digest + '.json')
        content = path.read_bytes()
        if len(content) > 256 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError('invalid policy artifact')
        serialized = content.decode('utf-8')
    data = json.loads(serialized)
    rules = [TriggerRule(r["rule_id"], r["text"],
                         tuple(tuple(s) for s in r["token_sequences"]),
                         r.get("priority", 0)) for r in data["rules"]]
    matcher = TokenMatcher(rules, data["vocab_size"], data["special_ids"])
    matcher.policy_version = data["policy_version"]
    matcher.notice_id = data.get("notice_id", "default_notice")
    return matcher


def validate_sampling(params):
    """Called before generation-config EOS expansion, so native EOS is allowed."""
    unsupported = (params.n != 1 or params.stop or params.stop_token_ids
                   or params.min_tokens or params.ignore_eos or params.bad_words
                   or params.logprobs is not None or params.prompt_logprobs is not None
                   or params.structured_outputs is not None
                   or params.repetition_detection is not None
                   or not params.detokenize or not params.skip_special_tokens
                   or params.include_stop_str_in_output)
    if unsupported:
        raise ValueError("unsupported combination for Trigger Guard experiment")


def write_test_trace(path, row):
    """Private controlled-text traces only; intentionally excluded from timings."""
    import time
    row["t_trace_ns"] = time.perf_counter_ns()
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
