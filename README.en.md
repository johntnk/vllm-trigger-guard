# vLLM Trigger Guard

[中文](README.md) · [Installation](docs/INSTALL.md) · [Architecture](docs/ARCHITECTURE.md)

Experimental post-verification stopping for vLLM/MTP, with an additional literal-text egress check and a bounded streaming adapter. This is an independent project, not an official vLLM component.

## Semantics

When a configured literal would be completed, withhold the completing token or character, stop the request, and append one fixed server notice. Previously emitted prefixes remain visible: blocking `SECRET` may produce `SECRE` followed by the notice. This is **not whole-word redaction** or semantic content moderation.

The core token matcher only consumes validated, reachable tokens. The text layer covers alternate tokenizations and substrings within larger tokens. Keep both layers for the integrated configuration.

## CPU quick start

```bash
git clone https://github.com/johntnk/vllm-trigger-guard.git
cd vllm-trigger-guard
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test]"
python -m trigger_guard demo
python -m unittest discover -s tests -v
```

The demo uses synthetic text, requires no GPU/model, and does not import vLLM.

## Integration and evidence

The optional integration is pinned to vLLM **0.17.0**, commit `b31e9326a7d9394aab8c767f8ebe225c65594b60`. The installer verifies input files, copies the package into a new overlay, applies the patches and checks output hashes. It does not modify the installed package. Follow [INSTALL.md](docs/INSTALL.md); newer vLLM versions are not supported by these patches.

Historical experiments verified ordinary synthetic dictionaries up to 100,000 entries and long-phrase dictionaries up to 35,000 entries. A matched, preloaded short-request comparison (32 requests per arm) measured TTFT p95 at 373 ms for 16 rules and 388 ms for 100,000 rules. A 60-minute run with 100,000 rules and **engine admission 4**, at approximately 0.42 requests/s, completed 1,494 requests with 21 intentional client cancellations and no rejections.

These are bounded historical observations, not a general safety guarantee or production capacity. The exported package and new launcher have not been revalidated with GPU inference. No matched benchmark against native vLLM `stop` was performed. The older CUDA gather failure remains incompletely attributed. See the [full report](docs/REPORT.zh-CN.md), [aggregate results](benchmarks/results/20261026/summary.json), and [current validation](docs/VALIDATION.md).

The service binds only to loopback and supports a restricted text-completion interface, one API worker and one GPU. There is no authentication. Multi-tenant rule changes, multiple completions, logprobs, structured output and several other sampling combinations are unsupported. No model weights, original request logs or private machine configuration are included.

## Attribution

Apache-2.0. Aho–Corasick and streaming checks are established techniques. The GDN patch is a backport of upstream [vLLM #34871](https://github.com/vllm-project/vllm/pull/34871), credited to its authors. See [NOTICE](NOTICE) and [PROVENANCE.json](PROVENANCE.json). AI assistance was used for implementation, packaging, documentation and experiments.
