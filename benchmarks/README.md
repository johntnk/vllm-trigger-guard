# Reproducing bounded measurements

These tools require explicit invocation. They do not allocate GPUs, start a server, change an engine configuration or stop another service. Use the [installation guide](../docs/INSTALL.md) and an assigned GPU for model experiments. Historical aggregates are under `results/20261026`; original machine configuration and per-request logs are not published.

## Synthetic dictionaries

```bash
python -m benchmarks.build_corpora --out runs/dictionaries --sizes 16 128 1000 --seeds 42
```

The generator provides ordinary, shared-prefix and long-phrase families with fixed trigger anchors. Use the same rule data when comparing two implementations. The historical 100,000-rule result concerns the ordinary and shared-prefix families; long phrases reached a lower state-budget limit.

## Explicit HTTP load

The driver is intentionally restricted to `http://127.0.0.1:18871`. It makes no retries and records planned arrivals, actual dispatches, rejected requests, cancellation and terminal protocol checks separately. The model argument supplies the tokenizer and chat template; the server's loaded policy must match `--policy`.

```bash
python -m benchmarks.run_load \
  --model /path/to/local/model --policy examples/policy.json \
  --profile S --concurrency 1 --count 8 --out runs/load-001
```

Only after a small check and a resource review, select a different load explicitly:

```bash
python -m benchmarks.run_load \
  --model /path/to/local/model --policy examples/policy.json \
  --profile M --rate 0.42 --duration 600 --count 300 \
  --trigger-every 10 --slow-fraction 0.01 --disconnect-fraction 0.01 \
  --out runs/load-002
```

`--rate` selects open-loop arrivals; otherwise the driver uses closed-loop concurrency. `--duration` is bounded by `--count`, so always check `duration_target_met` and `planned_arrival_span_seconds`. Reaching the count early does not establish the requested duration. A synthetic trigger workload is not proof that a live model actually attempted every configured literal.

## Interpreting the published data

- `summary.json` records historical scope, denominators and selected paired measurements.
- `groups.csv` contains group-level aggregates. Its timing columns include each group's completed workload mix; compare ordinary responses separately when policy hits shorten responses.
- `dictionary_capacity.csv` records CPU build/capacity points and process RSS, not net index memory.
- The historical full run had 4,456 dispatched requests including protection-disabled diagnostic controls. Safety conclusions use only the protection-enabled subsets.
- The 16-concurrent short groups and the one-hour run at engine admission 4 are distinct experiments.
- The original archive hash identifies a privately retained data version. It does not make the complete raw dataset publicly reproducible.

No production SLO, universal zero-leakage claim, or superiority over native `stop` is inferred from these measurements.
