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
    parser = argparse.ArgumentParser(prog="agent")
    subcommands = parser.add_subparsers(dest="cmd", required=True)
    command_parsers = {name: subcommands.add_parser(name, help=help_text) for name, help_text in [
        ("run", "replay the trigger script, run the simulator, judge goals, measure impact"),
        ("replay", "replay the trigger script and write decisions/actions"),
        ("evaluate", "judge goals and measure impact from simulator output")]}
    for name, subparser in command_parsers.items():
        subparser.add_argument("--pack", default="pack")
        subparser.add_argument("--out", default="sample_run")
        if name in ("run", "replay"):
            for setting, default in DEFAULTS.items():
                subparser.add_argument("--" + setting.replace("_", "-"), type=type(default), default=default)
        if name == "run":
            subparser.add_argument("--seed", default="oua-practice", help="simulator seed")
        if name == "evaluate":
            subparser.add_argument("--sim", required=True)
    args = parser.parse_args()

    if args.cmd in ("run", "replay"):
        report = Replay(args.pack, args.out, {setting: getattr(args, setting) for setting in DEFAULTS}).run()
        print("replay:", json.dumps(report))
    if args.cmd == "run":
        print("evaluate:", json.dumps(evaluate(args.pack, args.out, simulate(args.pack, args.out, args.seed)), indent=2))
    if args.cmd == "evaluate":
        print(json.dumps(evaluate(args.pack, args.out, args.sim), indent=2))


if __name__ == "__main__":
    main()
