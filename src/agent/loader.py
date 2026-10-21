"""Read the data pack. Reading only: no decisions, no cleaning beyond parsing.

Conflicts (duplicate profile rows, language variants) are kept as-is so that facts.py can resolve them
explicitly and record how.
"""
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


def iter_jsonl(path):
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Pack:
    root: Path
    catalogue: dict      # title_id -> row
    episodes: dict       # content_slug -> [row] sorted by season, episode
    profiles: dict       # user_id -> [row]  (more than one row = conflict, kept on purpose)
    goals: dict          # goal_id -> goal definition
    interventions: dict
    pricing: dict
    arms: dict           # user_id -> 'agent' | 'holdout'

    def events_path(self):
        return self.root / "data" / "events.jsonl"

    def replay(self, name):
        """Replay files: triggers, live_events, control_events, model_faults."""
        return list(iter_jsonl(self.root / "replay" / f"{name}.jsonl"))


def load_pack(root):
    root = Path(root)
    episodes = defaultdict(list)
    for r in _csv(root / "data/episodes.csv"):
        r["season"], r["episode"], r["minutes"] = int(r["season"]), int(r["episode"]), float(r["minutes"])
        episodes[r["content_slug"]].append(r)
    for eps in episodes.values():
        eps.sort(key=lambda r: (r["season"], r["episode"]))

    profiles = defaultdict(list)
    for r in _csv(root / "data/profiles_snapshot.csv"):
        profiles[r["user_id"]].append(r)

    goals = json.loads((root / "config/goals.json").read_text())
    return Pack(
        root=root,
        catalogue={r["title_id"]: r for r in _csv(root / "data/catalogue.csv")},
        episodes=dict(episodes),
        profiles=dict(profiles),
        goals={g["goal_id"]: g for g in goals["goals"]},
        interventions=json.loads((root / "config/interventions.json").read_text()),
        pricing=json.loads((root / "config/pricing.json").read_text()),
        arms={r["user_id"]: r["arm"] for r in _csv(root / "replay/arms.csv")},
    )
