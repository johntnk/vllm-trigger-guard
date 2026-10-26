"""Single-worker, loopback-only launcher for the pinned experimental adapter."""
import copy
import json
import os
from pathlib import Path
import sys

from .core import load_policy
from .overlay import verify_overlay


def create_isolated_engine(config, args_factory, engine_factory):
    return engine_factory(args_factory(**copy.deepcopy(config["engine_args"])))


def validate_engine_args(engine_args):
    if not isinstance(engine_args, dict):
        raise ValueError("engine config must contain a JSON object")
    if not Path(engine_args.get("model", "")).is_dir() or not engine_args.get("model"):
        raise ValueError("Set engine_args.model to a local model directory")
    if engine_args.get("trust_remote_code", False):
        raise ValueError("This launcher requires trust_remote_code=false")
    if any(engine_args.get(key, 1) != 1 for key in ["tensor_parallel_size", "pipeline_parallel_size"]):
        raise ValueError("Only single-GPU integration has been tested")
    selected = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    if not selected or "," in selected or selected == "-1":
        raise ValueError("Select exactly one GPU explicitly with CUDA_VISIBLE_DEVICES")


def serve(args):
    engine_args = json.loads(Path(args.engine_config).read_text(encoding="utf-8"))
    validate_engine_args(engine_args)
    policy_path = Path(args.policy).resolve()
    policy = load_policy(policy_path)
    overlay = verify_overlay(args.overlay)
    if "vllm" in sys.modules:
        raise RuntimeError("Start a fresh process before activating an overlay")
    sys.path.insert(0, str(overlay))
    # Spawned workers must import the same isolated package as this process.
    os.environ["PYTHONPATH"] = str(overlay) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    os.environ["TG_EXPERIMENT_ADAPTER"] = "1"
    os.environ["TG_CORE_POLICY"] = str(policy_path)
    os.environ["TG_POLICY_REGISTRY_DIR"] = str(run_dir / "policy-registry")
    os.environ["TG_COLLECTOR_REQUEST_BYTES"] = "262144"
    os.environ["TG_COLLECTOR_GLOBAL_BYTES"] = "8388608"
    os.environ.pop("TG_CANDIDATE_TRACE", None)
    os.environ.setdefault("VLLM_ATTENTION_BACKEND", "TRITON_ATTN")
    import vllm
    if Path(vllm.__file__).resolve().parent != overlay / "vllm":
        raise RuntimeError("vLLM did not load from the verified overlay")
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.v1.engine.async_llm import AsyncLLM
    from .server import make_app
    import uvicorn
    config = {"engine_args": engine_args, "trace_dir": str(run_dir / "traces"),
              "mode": "core_egress", "evidence": "user_run",
              "bounds": {"admission": {"engine_limit": args.engine_limit,
                                         "waiting_limit": args.waiting_limit,
                                         "stream_limit": 64}}}
    (run_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    engine = create_isolated_engine(config, AsyncEngineArgs, AsyncLLM.from_engine_args)
    app = make_app(engine, config, policy)
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, limit_concurrency=128)
