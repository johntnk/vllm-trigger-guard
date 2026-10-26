# Architecture and supported behavior

## Publication path

```text
model drafts -> native verification -> reachable accepted tokens
                                      |
                               core token guard
                                      |
                              incremental decoding
                                      |
                              literal text guard
                                      |
                         bounded publication buffer -> client
                                      |
                          one notice + terminal event on hit
```

The core matcher is deliberately narrower than literal-text matching. It compiles the bare and leading-space tokenizations of each rule, then inspects only verified, reachable outputs. Rejected drafts are not policy evidence. The integration retains native end-of-sequence and output-budget handling. Different tokenizations can evade those registered token paths, so the final decoded-text layer is required.

## Exact semantics

- Matching is case-sensitive, exact Unicode substring matching. There is no NFKC normalization, case folding, semantic classification or synonym matching.
- The first completing match wins; ties are resolved by `(priority, rule_id)`.
- The completing token or character and subsequent output are withheld. Earlier output can include the beginning of the literal. This differs from full-string suppression by native `stop` and from redacting the whole match.
- A fixed server notice is emitted once, followed by one terminal event. Further model text is ignored. Notice validation covers both the notice itself and its boundary with already published text.
- Only generated output is matched. A rule in the prompt does not itself trigger a stop.
- The complete literal may have existed in model computation before publication is blocked. This is an output-publication policy, not prevention of internal model generation.

## Shared programs and request isolation

Token programs use a last-token index for small buckets and an immutable sparse Aho–Corasick automaton for larger buckets. Large text programs use a shared automaton. Each request owns its own cursor/state; immutable programs can be shared within a process. Different processes still hold their own copies.

Compilation runs before request measurement. Trusted startup configuration can store a compiled policy by content digest in a local registry; request RPC carries the reference rather than a complete large dictionary. The cache is bounded. Public HTTP requests cannot upload, replace or reference arbitrary policy files.

## Bounded adapter

The producer and network sender are separate. Admission limits cover streams, engine requests, waiting requests, token reservations and bytes. Per-request and global output budgets include sender-owned bytes until a send completes. A compact terminal descriptor has a separate path so an ordinary payload queue does not prevent recording the terminal decision.

Cancellation cleanup waits for native request reclamation before returning an engine slot. The outer response cleanup also handles disconnects during headers, before body iteration starts. A failed cleanup causes admission to fail closed. Overload rejections, intentional cancellations and transport failures are not reported as successful policy interceptions.

Slow-read tests with bounded responses do not alone prove OS socket buffers became full. Likewise, client cancellation is not proof that computation stopped before its natural completion. The historical report distinguishes these limitations.

## Supported scope

The pinned integrated adapter supports one GPU, one API process, one completion, text prompts and streaming/non-streaming text completions. The launcher enables `core_egress`; library modes `off`, `core` and `egress` exist for diagnostics, and should not be described as the fully tested combined protection.

The engine integration rejects incompatible combinations, including user `stop`/`stop_token_ids`, native `bad_words`, `min_tokens`, `ignore_eos`, logprobs, structured output, repetition detection, disabled detokenization, and altered special-token handling. The HTTP request schema exposes a deliberately smaller set of fields.

Multi-GPU, multiple API workers, tenant-specific policies, frequent rule replacement, tool calls, multimodal moderation, long-term process restart recovery and full numerical equivalence across all model modes remain outside the validated scope. This package is not a production security boundary by itself.

## Attribution and evolution

The integration is a version-specific patch rather than an upstream vLLM API. The GDN correction is upstream work credited in [NOTICE](../NOTICE). Later upstream releases contain additional changes, including stop-string fixes; this project does not claim those fixes. Porting to another release requires code review and new tests rather than editing a version string.
