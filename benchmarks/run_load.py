"""Explicit loopback load generation; never starts or reconfigures a GPU server."""
import argparse
import asyncio
import json
from pathlib import Path

from trigger_guard.core import load_policy
from benchmarks.driver import run_load, workload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profile", choices=["short", "S", "M", "L_bound", "O_bound"], default="S")
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--rate", type=float)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--arrival", choices=["fixed", "poisson", "burst"], default="fixed")
    parser.add_argument("--slow-fraction", type=float, default=0)
    parser.add_argument("--disconnect-fraction", type=float, default=0)
    parser.add_argument("--trigger-every", type=int, default=0)
    args = parser.parse_args()
    if not Path(args.model).is_dir():
        parser.error("--model must be a local tokenizer/model directory")
    if not 1 <= args.concurrency <= 64 or not 1 <= args.count <= 10000:
        parser.error("Use concurrency 1..64 and count 1..10000")
    if args.rate is not None and args.rate <= 0:
        parser.error("--rate must be positive")
    if args.duration is not None and (args.rate is None or not 0 < args.duration <= 3600):
        parser.error("--duration requires a rate and must be in (0,3600]")
    if any(not 0 <= x <= 1 for x in [args.slow_fraction, args.disconnect_fraction]):
        parser.error("fractions must be in [0,1]")
    if args.trigger_every < 0:
        parser.error("--trigger-every must be nonnegative")
    from vllm.tokenizers import get_tokenizer
    tokenizer = get_tokenizer(args.model)
    policy = load_policy(args.policy)
    if args.trigger_every and not any(r["text"] == "花朵" for r in policy["rules"]):
        parser.error("The synthetic trigger workload requires the example rule 花朵")
    cycle_length = min(args.trigger_every, args.count) if args.trigger_every else 3
    items = [workload(tokenizer, args.profile,
                      "early" if args.trigger_every and i % args.trigger_every == 0 else "no_trigger", i)
             for i in range(cycle_length)]
    result = asyncio.run(run_load("http://127.0.0.1:18871", policy, items, args.out,
        mode="open-loop" if args.rate else "closed-loop", concurrency=args.concurrency,
        count=args.count, rate=args.rate, duration=args.duration, arrival=args.arrival,
        slow_fraction=args.slow_fraction, disconnect_fraction=args.disconnect_fraction))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
