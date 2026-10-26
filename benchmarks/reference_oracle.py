"""Independent finite-literal oracle: length-indexed substring enumeration.

No production matcher/automaton imports. Exact Unicode code points, no folding.
For very large corpora this avoids doing N string scans for every test trajectory.
"""
class LiteralOracle:
    def __init__(self, rules):
        self.by_text = {}
        for rule in rules:
            rank = (rule.get('priority', 0), rule['rule_id'])
            prior = self.by_text.get(rule['text'])
            if prior is None or rank < prior:
                self.by_text[rule['text']] = rank
        self.lengths = sorted({len(s) for s in self.by_text})

    def first(self, text):
        for end in range(1, len(text)+1):
            found = []
            for size in self.lengths:
                if size > end:
                    break
                rank = self.by_text.get(text[end-size:end])
                if rank is not None:
                    found.append(rank)
            if found:
                return {'end': end-1, 'rule_id': min(found)[1], 'safe': text[:end-1]}
        return None


def token_first(tokens, rules, eos=None, budget=None):
    # Intentionally brute-force independent oracle for small randomized tests.
    prefix=[]
    for pos, token in enumerate(tokens):
        if budget is not None and pos >= budget:
            break
        prefix.append(token)
        hits=[]
        for rule in rules:
            for seq in rule.token_sequences:
                if len(seq)<=len(prefix) and tuple(prefix[-len(seq):])==tuple(seq):
                    hits.append((rule.priority,rule.rule_id))
        if hits:
            return pos,min(hits)[1]
        if token==eos:
            break
    return None
