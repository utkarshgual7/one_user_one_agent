# One User, One Agent

A per-viewer lifecycle agent for the OTT dialect platform. For each viewer, on each app open, it chooses one
measurable goal, decides what to do (a personalised home page, one follow-up message, or nothing) from evidence it
can cite, checks that decision independently, publishes within the latency budget, and later judges the goal and
measures impact against the randomized holdout.

Python 3.9+ standard library only. No API key, no network, no installs.

## Setup

The repository includes the complete synthetic data pack, simulator, and configuration. No external data, API key,
network access, or package installation is required.

## Run

One command replays the trigger script, runs the outcome simulator, judges every goal and writes the impact report:

```
PYTHONPATH=src python3 -m agent run --pack pack --out sample_run
```

About 15 seconds. Other entry points:

```
PYTHONPATH=src python3 -m agent replay   --pack pack --out sample_run          # decisions + actions only
PYTHONPATH=src python3 -m agent evaluate --pack pack --out sample_run --sim sample_run/sim
PYTHONPATH=src python3 -m agent run --pack pack --out run2 --seed my-seed       # another simulator seed
PYTHONPATH=src python3 -m agent run --help                                     # all limits are flags
```

Configurable limits (flags on `run` / `replay`): `--workers 4`, `--max-model-calls 8`, `--max-tool-calls 20`,
`--model-timeout-s 5`, `--store-timeout-s 5`, `--shed-model-after-s 2`.

For a different replay day, point `--pack` at a pack containing that day's `replay/` files. Nothing is hardcoded to
the sample day except the simulator horizon used for judging (`judge.OBSERVED_UNTIL`).

## Tests

```
PYTHONPATH=src python3 -m unittest discover tests
```

74 tests. The four the brief asks for:

| Required                  | Test                                                                                                                                             |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Late or duplicate trigger | `test_replay.py`: `test_duplicate_trigger_gets_one_decision`, `test_late_buffered_trigger_is_recorded_but_not_acted_on`                          |
| Conflicting source        | `test_facts.py`: `test_three_dialects_uses_app_setting_and_records_others` (+ 5 more conflict tests)                                             |
| Tool failure              | `test_replay.py`: `test_data_store_down_means_no_action`, `test_model_outage_keeps_page_drops_message`, `test_model_timeout_is_cut_at_our_limit` |
| Corrected goal rule       | `test_goals.py`: `test_g03_correction_changes_success_for_open_goals`; `test_judge.py`: `test_corrected_rule_applies_when_judging`               |

## Output (`sample_run/`)

| File                  | What                                                                                                                                                                                                                                           |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `decisions.jsonl`     | Every decision: trigger and its age, goal with deadline and success signal, action or the reason for none, dialect and conflicts, content, confidence with reasons, evidence with age, assumptions, check results, model call, budget, latency |
| `actions.jsonl`       | Published actions (simulator input). `actions_all.jsonl` also has cancelled and superseded ones                                                                                                                                                |
| `outcomes.jsonl`      | One verdict per goal (succeeded / failed / inconclusive / superseded) citing the deciding event, plus whether an agent page or message was seen first                                                                                          |
| `control_log.jsonl`   | Each surprise, the viewers and decisions it affected, and the response                                                                                                                                                                         |
| `latency_report.json` | p50/p95/p99/max, over-60s and over-5min counts, peak hour, dependency-outage windows                                                                                                                                                           |
| `impact_report.md`    | Arm vs arm, why served-vs-unserved misleads, the pilot re-analysed                                                                                                                                                                             |
| `final_report.md`     | Run summary and per-viewer walkthroughs                                                                                                                                                                                                        |
| `viewer_state/`       | Per-viewer state (goals, decisions, actions, learned). Eight examples committed; a full run writes all 1,746 plus `state.db`                                                                                                                   |

## Where to read the code

```
src/agent/
  loader.py     read the pack (no cleaning, conflicts kept)
  state.py      durable per-viewer memory (SQLite); dedupe, one decision per trigger, one open goal per viewer
  facts.py      facts with source, age, evidence and conflicts; precedence rules
  goals.py      eligibility, goal choice, success rules, rule corrections
  planner.py    home page / follow-up / nothing
  provider.py   model interface, mock provider (replays model_faults.jsonl), call budget
  verifier.py   independent checks; does not import the planner
  agent.py      one decision: facts -> goal -> model advice -> plan -> verify -> publish
  replay.py     harness: event ordering, fault injection, worker queue, surprises, outputs
  judge.py      goal verdicts, attribution, per-viewer learning
  impact.py     arm-vs-arm measurement, misleading comparisons, pilot re-analysis
  evaluate.py   simulator -> judge -> impact
```

Design: [ARCHITECTURE.md](ARCHITECTURE.md). Limits: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).
AI use: [AI_COLLABORATION.md](AI_COLLABORATION.md). Data quirks: appendix of [ARCHITECTURE.md](ARCHITECTURE.md).
