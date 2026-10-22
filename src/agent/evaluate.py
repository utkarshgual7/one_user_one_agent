"""After the simulator: judge every goal, record learning, measure impact. Reuses the replay's durable state."""
import json
import subprocess
import sys
from pathlib import Path

from . import goals, impact
from .judge import OBSERVED_UNTIL, Judge, ingest_outcomes
from .loader import load_pack
from .replay import dump_viewers
from .state import State


def simulate(pack_dir, run_dir, seed):
    sim = Path(run_dir) / "sim"
    subprocess.run([sys.executable, str(Path(pack_dir) / "simulator/simulate_outcomes.py"), "--pack", str(pack_dir),
                    "--actions", str(Path(run_dir) / "actions.jsonl"), "--out", str(sim), "--seed", seed], check=True)
    return sim


def evaluate(pack_dir, run_dir, sim_dir):
    run, pack = Path(run_dir), load_pack(pack_dir)
    state = State(str(run / "viewer_state/state.db"))
    book = goals.GoalBook(pack.goals)
    for control in pack.replay("control_events"):          # judge by the rules as corrected during the replay
        if control["type"] == "goal_rule_corrected":
            book.correct(control)
    lines, new = ingest_outcomes(state, Path(sim_dir) / "outcome_events.jsonl")
    outcomes = Judge(book, pack).judge_all(state, OBSERVED_UNTIL)
    with open(run / "outcomes.jsonl", "w") as file:
        for outcome in outcomes:
            file.write(json.dumps(outcome) + "\n")

    decisions = [json.loads(line) for line in open(run / "decisions.jsonl")]
    res = impact.analyse(impact.viewer_outcomes(state, decisions, OBSERVED_UNTIL))
    res["pilot"] = impact.pilot_reanalysis(pack_dir)
    seed = json.loads((Path(sim_dir) / "simulator_report.json").read_text())["seed"]
    (run / "impact.json").write_text(json.dumps(res, indent=2))
    (run / "impact_report.md").write_text(impact.report_md(res, res["pilot"], seed))
    dump_viewers(state, run / "viewer_state", {decision["viewer_id"] for decision in decisions})
    verdicts = {}
    for outcome in outcomes:
        verdicts.setdefault(outcome["arm"], {}).setdefault(outcome["verdict"], 0)
        verdicts[outcome["arm"]][outcome["verdict"]] += 1
    return {"outcome_events": lines, "new_events": new, "verdicts": verdicts,
            "primary": res["primary_goal_reached"], "secondary": res["secondary"]}
