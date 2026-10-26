# Installation and the pinned vLLM integration

The CPU library needs Python 3.11 or later. The optional model-serving integration was tested historically on Linux, Python 3.12, vLLM 0.17.0 and PyTorch 2.10.0+cu128, on one 48 GiB RTX 4090 D. The exported launcher has CPU/mock validation only; no new GPU validation was performed during publication.

## 1. Start with an isolated environment

Use a new virtual environment. Existing drivers, system Python and unrelated services do not need to be changed.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[server]"
python -m pip install vllm==0.17.0
```

Use a CUDA/PyTorch combination supported by that vLLM release. This project does not install GPU drivers. A model compatible with the MTP configuration must already be available locally; model weights and access rights are not supplied here.

## 2. Prepare an isolated patched copy

Locate the installed package without loading a model:

```bash
python -c 'import importlib.util,pathlib; print(pathlib.Path(importlib.util.find_spec("vllm").origin).parent)'
```

Pass the resulting directory explicitly:

```bash
python -m trigger_guard prepare-vllm \
  --source /path/to/.venv/lib/python3.12/site-packages/vllm \
  --output .runtime/vllm017
```

Git is required for patch application. The command rejects existing output directories, overlapping source/destination paths and source files that differ from the pinned base. All Python files are copied; installed `.so` extension binaries are linked into the copy. Keep the original virtual environment in place. An incomplete output directory is retained if preparation fails; inspect it and choose a new destination for a retry.

The expected base is `b31e9326a7d9394aab8c767f8ebe225c65594b60`. If a wheel or locally modified installation fails the source check, use an independent source build of that exact commit. Do not bypass the hash check or patch a current vLLM release blindly. The installer checks the touched Python files; it cannot establish CUDA binary compatibility on a different system.

The two patches are applied in order:

1. `vllm017.patch`: the experimental token guard, request metadata and bounded collector integration.
2. `gdn34871.patch`: the credited upstream GDN mixed-batch fix.

The unmodified installation remains available. To revert, stop the process you launched with this overlay and use the original environment normally. No global uninstall or driver change is required.

## 3. Configure and launch

Edit `examples/engine.json` to point to your local model. Its MTP configuration assumes the model supports `method=mtp`; FP8 KV cache is also model/hardware dependent. The historical model used the `Qwen3_5ForConditionalGeneration` architecture with FP8 weights, BF16 compute, FP8 KV cache and `TRITON_ATTN`. A file directory name alone does not establish that configuration.

The launcher requires explicit selection of one GPU. Run only on a device assigned to you, with sufficient free resources.

```bash
CUDA_VISIBLE_DEVICES=0 VLLM_ATTENTION_BACKEND=TRITON_ATTN \
python -m trigger_guard serve \
  --overlay .runtime/vllm017 \
  --engine-config examples/engine.json \
  --policy examples/policy.json \
  --run-dir runs/example-001 \
  --engine-limit 4 --waiting-limit 8
```

The run directory must be new. This launcher enables both checks, disables candidate tracing, uses one API worker, and binds to `127.0.0.1:18871`. It does not enable the historical experiment's private hot-reconfiguration or forced-preemption hooks. The service records per-request lifecycle timings and rule event metadata locally, but does not enable raw candidate token tracing.

The request's `model` field is the adapter identifier `trigger-guard-lab`, regardless of the underlying model directory. This is a restricted `/v1/completions` interface, not a full OpenAI chat/reasoning/tool API.

```bash
curl http://127.0.0.1:18871/health
curl -N http://127.0.0.1:18871/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"trigger-guard-lab","prompt":"Explain binary search.","max_tokens":64,"stream":true}'
```

Prompts are passed as text without automatic chat templating. Apply your model's chat template on the client if needed; the benchmark workload builder does this explicitly. The adapter has no authentication and must remain behind an appropriately configured application if used beyond a trusted local experiment.

## Policy format

See [the example](../examples/policy.json). Required metadata is `policy_version`, `notice_id` and `match_semantics="literal_substring"`. Each rule has a unique `rule_id`, nonempty `text` and optional integer `priority` (smaller first). The notice must not contain a registered literal or complete one across the published-prefix boundary.

Limits: 100,000 rules, 4,096 Unicode characters per rule, 16,000,000 total characters, 8,000,000 compiled rule tokens, and 4,000,000 automaton states. These are budgets, not universal tested capacities. The longest successfully replayed rule in the historical study was 156 characters. Rejection is explicit rather than silently dropping rules.
