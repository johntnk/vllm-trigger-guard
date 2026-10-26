# Validation of the initial public package

Date: 2026-10-26. Packaging validation was performed locally on Windows with Python 3.12 in a new virtual environment. It did not access a GPU or modify a server environment.

## Completed checks

- **43 CPU/mock tests passed** with `python -m unittest discover -s tests -v` (19.110 seconds in the local run after the cancellation correction).
- Matching checks include 100,000 interleaved operations, independent brute-force comparisons, different chunk partitions, Unicode boundaries, overlapping rules, deterministic ties and terminal-state behavior.
- Adapter tests use a synthetic `FakeEngine` over real loopback TCP, plus controlled ASGI failures. They exercise disconnect before body iteration, blocked sends, cancellation, overload recovery and resource accounting. Sampling types are explicit test stubs, not a real vLLM engine.
- The CPU command-line demo produced one notice and withheld the completing character. The displayed prefix remained visible, as documented.
- `pip check` reported no broken dependencies in the local test environment.
- Source and wheel distributions built successfully. The wheel was installed without dependencies in a second clean virtual environment; the CPU demo and pinned overlay preparation both ran from the installed wheel, outside the source tree.
- Both integration patches applied cleanly to the corresponding files fetched from the public pinned vLLM commit. Ten existing source files and eleven resulting files were checked. The Trigger Guard runtime output matched the archived experimental implementation after line-ending normalization.
- The new overlay preparation command was executed against that public-source fixture and verified every patched output hash. A test also covers an enclosing Git repository, unchanged source files, incompatible bases, existing destinations and overlapping paths.

## Evidence distinctions

The historical GPU results under `benchmarks/results/20261026/` came from the original pinned experimental runtime. Source extraction retains the matching logic with packaging/import changes; the package also adds an installer, launcher and the admission-cancellation correction described below. **No GPU inference was rerun against the exported package or new launcher.**

The original study's 69 CPU/mock tests, 8 GDN metadata tests and 7 load-driver tests belong to that historical study. This repository's current 43-test suite is a curated, portable subset plus packaging tests; the counts are not interchangeable.

The patched public-source fixture contains the Python files needed for patch verification. It is not a complete installed vLLM runtime and does not validate binary extensions, CUDA compatibility or live model startup. CI performs CPU/mock checks only. Passing checks do not establish general model-output safety or production capacity.

## Python 3.11 admission-cancellation correction

The first public CI run passed on Python 3.12 but exposed a cancellation race on Python 3.11. When a queued request was granted an engine slot and cancelled in the same event-loop turn, `asyncio.wait_for` could return the completed future instead of propagating `CancelledError`. This bypassed the admission cleanup path. The failure was reproduced locally on Python 3.11.15; the [Python 3.11 implementation](https://github.com/python/cpython/blob/v3.11.17/Lib/asyncio/tasks.py) contains the corresponding completed-future branch in its cancellation handler.

Admission now uses `asyncio.timeout` around the shielded future. Its existing exception cleanup returns either the waiting budget or the granted slot. The regression test exercises 100 cancellations before admission and 100 immediately after admission, checks all queue/token counters return to zero and verifies a subsequent request can acquire a slot. All four focused budget tests pass on local Python 3.11 and 3.12. This correction does not alter matching behavior or the archived GPU measurements.

## Locally exercised dependency versions

FastAPI 0.162.0, Starlette 1.10.0, Pydantic 2.14.1, AnyIO 4.15.1, Uvicorn 0.54.0 and HTTPX 0.28.1. These describe the packaging/mock validation, not the historical GPU environment. The package declares bounded compatible dependency ranges; subsequent dependency updates still need regression testing.
