# Contributing

Keep changes focused and explain the user-visible behavior, validation and limitations. Preserve the exact matching and terminal-event semantics unless an API change is intentional and documented.

```bash
python -m pip install -e ".[test]"
python -m unittest discover -s tests -v
python -m trigger_guard demo
```

Use the independent oracle for matching regressions. New integration versions need separate base/target manifests and tests against their actual public upstream source. A passing CPU or mocked test must not be described as a successful GPU inference test.

Benchmark reports should include input/output lengths, rule distribution, concurrency versus engine admission, arrival rate, warmup, sample counts, cancellations, rejections and failures. Do not compare shortened policy responses against complete ordinary responses as a speedup. No benchmark runs automatically during installation or CI.

Do not commit credentials, model weights, private prompts, business dictionaries, machine addresses or original operational logs. Use synthetic minimal reproducers. Describe material AI assistance in the change description and review the resulting code and evidence.

Contributions are distributed under Apache-2.0. Preserve existing copyright and upstream attribution. This repository's workflow is independent of contributing a PR to the vLLM project.
