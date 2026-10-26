"""CPU demo and explicitly selected, isolated vLLM integration commands."""
import argparse
import json
from pathlib import Path

from .core import load_policy, validate_notice
from .egress import EgressProgram
from .protocol import Publication


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run a deterministic CPU text-guard example")
    demo.add_argument("--policy", type=Path)
    demo.add_argument("--text", default="A safe introduction. SECRET later text.")
    demo.add_argument("--chunk-size", type=positive, default=3)
    prep = sub.add_parser("prepare-vllm", help="Create a new copy with the pinned vLLM patches")
    prep.add_argument("--source", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    launch = sub.add_parser("serve", help="Run the pinned text-completion adapter on loopback")
    launch.add_argument("--overlay", type=Path, required=True)
    launch.add_argument("--engine-config", type=Path, required=True)
    launch.add_argument("--policy", type=Path, required=True)
    launch.add_argument("--run-dir", type=Path, required=True)
    launch.add_argument("--port", type=positive, default=18871)
    launch.add_argument("--engine-limit", type=positive, default=4)
    launch.add_argument("--waiting-limit", type=positive, default=8)
    args = parser.parse_args(argv)
    if args.command == "demo":
        policy = load_policy(args.policy) if args.policy else {
            "rules": [{"rule_id": "demo-secret", "text": "SECRET", "priority": 0}],
            "notice": "\n[Stopped by policy.]"}
        validate_notice([r["text"] for r in policy["rules"]], policy["notice"])
        program = EgressProgram(policy["rules"])
        request = Publication(policy, "egress", program)
        events = []
        for i in range(0, len(args.text), args.chunk_size):
            events.extend(request.process(args.text[i:i + args.chunk_size]))
        if not request.done:
            events.extend(request.process("", finish_reason="stop"))
        print(json.dumps({"evidence": "synthetic_cpu_text_only", "events": events,
                          "visible_text": "".join(value for kind, value in events if kind != "finish"),
                          "notice_count": request.notice_count}, ensure_ascii=False, indent=2))
    elif args.command == "prepare-vllm":
        from .overlay import prepare_overlay
        print(prepare_overlay(args.source, args.output))
    else:
        from .launch import serve
        serve(args)
    return 0
