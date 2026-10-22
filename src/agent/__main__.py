"""CLI.

    PYTHONPATH=src python -m agent run --pack pack --out sample_run        # replay -> simulator -> judge -> impact
    PYTHONPATH=src python -m agent replay --pack pack --out sample_run     # decisions + actions only
    PYTHONPATH=src python -m agent evaluate --pack pack --out sample_run --sim sample_run/sim
"""
import argparse
import json

from .evaluate import evaluate, simulate
from .replay import DEFAULTS, Replay


def main():
    ap = argparse.ArgumentParser(prog="agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    cmds = {name: sub.add_parser(name, help=h) for name, h in [
        ("run", "replay the trigger script, run the simulator, judge goals, measure impact"),
        ("replay", "replay the trigger script and write decisions/actions"),
        ("evaluate", "judge goals and measure impact from simulator output")]}
    for name, p in cmds.items():
        p.add_argument("--pack", default="pack")
        p.add_argument("--out", default="sample_run")
        if name in ("run", "replay"):
            for k, v in DEFAULTS.items():
                p.add_argument("--" + k.replace("_", "-"), type=type(v), default=v)
        if name == "run":
            p.add_argument("--seed", default="oua-practice", help="simulator seed")
        if name == "evaluate":
            p.add_argument("--sim", required=True)
    a = ap.parse_args()

    if a.cmd in ("run", "replay"):
        report = Replay(a.pack, a.out, {k: getattr(a, k) for k in DEFAULTS}).run()
        print("replay:", json.dumps(report))
    if a.cmd == "run":
        print("evaluate:", json.dumps(evaluate(a.pack, a.out, simulate(a.pack, a.out, a.seed)), indent=2))
    if a.cmd == "evaluate":
        print(json.dumps(evaluate(a.pack, a.out, a.sim), indent=2))


if __name__ == "__main__":
    main()
