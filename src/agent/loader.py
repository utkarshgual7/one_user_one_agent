"""Read the data pack. Reading only: no decisions, no cleaning beyond parsing.

Conflicts (duplicate profile rows, language variants) are kept as-is so that facts.py can resolve them
explicitly and record how.
"""
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path


def iter_jsonl(path):
    with open(path) as file:
        for line in file:
            if line.strip():
                yield json.loads(line)


def _csv(path):
    with open(path, newline="") as file:
        return list(csv.DictReader(file))


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

    @cached_property
    def dialects(self):
        """Dialects that actually have content. App settings outside this set are invalid."""
        return {title["dialect"] for title in self.catalogue.values()}

    def events_path(self):
        return self.root / "data" / "events.jsonl"

    def replay(self, name):
        """Replay files: triggers, live_events, control_events, model_faults."""
        return list(iter_jsonl(self.root / "replay" / f"{name}.jsonl"))


def load_pack(root):
    root = Path(root)
    episodes = defaultdict(list)
    for row in _csv(root / "data/episodes.csv"):
        row["season"], row["episode"], row["minutes"] = int(row["season"]), int(row["episode"]), float(row["minutes"])
        episodes[row["content_slug"]].append(row)
    for eps in episodes.values():
        eps.sort(key=lambda row: (row["season"], row["episode"]))

    profiles = defaultdict(list)
    for row in _csv(root / "data/profiles_snapshot.csv"):
        profiles[row["user_id"]].append(row)

    goals = json.loads((root / "config/goals.json").read_text())
    return Pack(
        root=root,
        catalogue={row["title_id"]: row for row in _csv(root / "data/catalogue.csv")},
        episodes=dict(episodes),
        profiles=dict(profiles),
        goals={goal["goal_id"]: goal for goal in goals["goals"]},
        interventions=json.loads((root / "config/interventions.json").read_text()),
        pricing=json.loads((root / "config/pricing.json").read_text()),
        arms={row["user_id"]: row["arm"] for row in _csv(root / "replay/arms.csv")},
    )
