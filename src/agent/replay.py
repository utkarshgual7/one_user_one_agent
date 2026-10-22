"""Replay harness: feeds triggers, live events and control events in delivery order to the agent.

- Dependency faults from control events are injected here (model provider, user data store).
- A small worker pool turns bursts into queueing delay, so latency is measured honestly under the evening peak.
- published_at = deliver_at + measured processing time (+ simulated queue and dependency waits).
- Surprises are handled by finding the affected viewers/decisions only, never by reprocessing everyone.
"""
import hashlib
import heapq
import json
import shutil
from pathlib import Path

from . import clock, goals, planner
from .agent import Agent, StoreError
from .judge import Judge
from .loader import load_pack
from .provider import MockProvider
from .state import State
from .verifier import Verifier

DEFAULTS = {"workers": 4, "max_model_calls": 8, "max_tool_calls": 20, "model_timeout_s": 5.0,
            "store_timeout_s": 5.0, "shed_model_after_s": 20.0}


def _h01(*parts):
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


class Health:
    """Dependency status at a moment, from control events. Active only inside each stated window."""

    def __init__(self, controls):
        self.windows = [(c["dependency"], c["type"], c["at"], c["until"]) for c in controls
                        if c["type"] in ("dependency_unavailable", "dependency_degraded")]

    def __call__(self, dep, now):
        for d, kind, start, until in self.windows:
            if d == dep and start <= now < until:
                return "unavailable" if kind == "dependency_unavailable" else "degraded"
        return "ok"


class Store:
    """User data store with injected faults. One retry, then the caller abstains."""

    def __init__(self, state, health, timeout_s):
        self.state, self.health, self.timeout_s = state, health, timeout_s

    def history(self, user_id, as_of, budget, key):
        for attempt in (1, 2):
            budget.tool()
            status = self.health("user_data_store", as_of)
            if status == "unavailable":
                budget.waited_s += 0.1
                continue
            if status == "degraded":
                if _h01(key, attempt) < 0.2:          # 20% of reads time out
                    budget.waited_s += self.timeout_s
                    continue
                budget.waited_s += 3.0                # slow but successful read
            else:
                budget.waited_s += 0.02
            return self.state.history(user_id, as_of)
        raise StoreError("user data store read failed twice")


class Replay:
    def __init__(self, pack_dir, out_dir, cfg=None):
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.pack = load_pack(pack_dir)
        self.out = Path(out_dir)
        state_dir = self.out / "viewer_state"
        if state_dir.exists():
            shutil.rmtree(state_dir)
        state_dir.mkdir(parents=True)
        self.state = State(str(state_dir / "state.db"))
        self.state.ingest_history(self.pack.events_path())
        plays = [json.loads(r[0]) for r in self.state.db.execute("SELECT body FROM events WHERE event='play_started'")]
        self.cat = planner.Catalog(self.pack, planner.play_counts(self.pack, plays))
        self.book = goals.GoalBook(self.pack.goals)
        self.controls = self.pack.replay("control_events")
        self.health = Health(self.controls)
        self.verifier = Verifier(self.pack, self.state)
        self.agent = Agent(self.pack, self.state, self.book, self.cat, self.verifier,
                           MockProvider(self.pack.replay("model_faults"), self.health),
                           Store(self.state, self.health, self.cfg["store_timeout_s"]), self.cfg)
        self.judge = Judge(self.book, self.pack)
        self.log, self.decisions = [], []
        self.n_dec = self.n_act = 0
        self.duplicates = 0

    # ---- ids
    def _decision_id(self):
        self.n_dec += 1
        return f"D-{self.n_dec:06d}"

    def _save_actions(self, actions):
        for a in actions:
            self.n_act += 1
            a["action_id"] = f"A-{self.n_act:06d}"
            a["status"] = "scheduled" if a["type"] in ("push", "whatsapp") else "published"
            self.state.save_action(a)

    # ---- stream
    def stream(self):
        items = [(c["at"], 0, "control", c) for c in self.controls]
        items += [(e["deliver_at"], 1, "live", e) for e in self.pack.replay("live_events")]
        items += [(t["deliver_at"], 2, "trigger", t) for t in self.pack.replay("triggers")]
        return sorted(items, key=lambda x: (x[0], x[1]))

    def run(self):
        free = [0.0] * self.cfg["workers"]       # epoch seconds when each worker is next free
        heapq.heapify(free)
        for at, _, kind, item in self.stream():
            if kind == "control":
                self.on_control(item)
            elif kind == "live":
                self.on_live(item)
            else:
                self.on_trigger(item, free)
        return self.write()

    def on_trigger(self, t, free):
        if self.state.has_decision(t["event_id"]):
            self.duplicates += 1
            self.log.append({"at": t["deliver_at"], "type": "duplicate_trigger", "event_id": t["event_id"],
                             "user_id": t["user_id"], "response": "ignored: decision already exists for this trigger"})
            return
        self.state.add_event({k: v for k, v in t.items() if k != "deliver_at"})
        deliver = clock.parse(t["deliver_at"]).timestamp()
        start = max(deliver, heapq.heappop(free))
        now = clock.shift(t["deliver_at"], seconds=start - deliver)
        d, actions = self.agent.decide(t, self._decision_id(), now, queue_wait_s=start - deliver)
        heapq.heappush(free, deliver + d["decided_in_ms"] / 1000)
        self._save_actions(actions)
        d["action_ids"] = [a["action_id"] for a in actions]
        self.state.save_decision({**d, "decided_at": d["published_at"]})
        self.decisions.append(d)

    def on_live(self, e):
        e = {k: v for k, v in e.items() if k != "deliver_at"}
        if not self.state.add_event(e):
            return
        if e["event"] == "notification_opt_out":
            self._cancel(lambda a: a["user_id"] == e["user_id"] and a["type"] == e["properties"].get("channel"),
                         f"viewer opted out of {e['properties'].get('channel')}", e["received_at"])
        self.judge.on_event(self, e)

    def on_control(self, c):
        entry = {"at": c["at"], "type": c["type"], "detail": c}
        if c["type"] == "title_withdrawn":
            tid = c["title_id"]
            self.cat.withdraw(tid, c["at"])
            self.verifier.withdrawn[tid] = c["at"]
            entry["affected_pages"] = self._repair_pages(tid, c["at"])
            entry["cancelled_messages"] = self._cancel(
                lambda a: a["type"] in ("push", "whatsapp") and a.get("title_id") == tid,
                f"title {tid} withdrawn", c["at"])
        elif c["type"] == "goal_rule_corrected":
            self.book.correct(c)
            open_g = self.state.goals(status="open")
            touched = [g["goal_key"] for g in open_g if g["goal_id"] == c["goal_id"]]
            entry["affected_open_goals"] = len(touched)
            entry["rule_version"] = self.book.version
            entry["response"] = (f"{c['goal_id']} success rule replaced; {len(touched)} open {c['goal_id']} goals "
                                 "will be judged by the new rule; no other goal or viewer reprocessed")
            entry["reopened_for_rejudging"] = self.judge.on_rule_change(self, c["goal_id"])
        else:
            entry["response"] = (f"{c['dependency']} {c['type'].split('_')[1]} until {c['until']}: decisions fall "
                                 "back (model: rules only, no messages; store: retry once, else no action)")
        self.log.append(entry)

    def _repair_pages(self, tid, at):
        """Republish live pages that contain a withdrawn title, without it. Rows that drop below 3 are removed."""
        ttl = self.pack.interventions["home_page"]["ttl_minutes"] * 60
        repaired = []
        for a in self.state.all_actions():
            if a["type"] != "home_page" or a.get("status") != "published" or clock.age_s(a["published_at"], at) >= ttl:
                continue
            ids = [a["hero_title_id"]] + [t for r in a["rows"] for t in r["title_ids"]]
            if tid not in ids:
                continue
            rows = [{**r, "title_ids": [t for t in r["title_ids"] if t != tid]} for r in a["rows"]]
            rows = [r for r in rows if len(r["title_ids"]) >= 3]
            hero = None if a["hero_title_id"] == tid else a["hero_title_id"]
            a["status"] = "superseded"
            self.state.update_action(a)
            new = {k: v for k, v in a.items() if k not in ("action_id", "status")}
            new.update({"rows": rows, "hero_title_id": hero, "published_at": at, "replaces": a["action_id"],
                        "reason": f"title {tid} withdrawn"})
            if rows or hero:
                self._save_actions([new])
            repaired.append({"user_id": a["user_id"], "old_action": a["action_id"],
                             "new_action": new.get("action_id", "none: page empty after removal")})
        return repaired

    def _cancel(self, match, reason, at):
        out = []
        for a in self.state.all_actions():
            if a.get("status") == "scheduled" and match(a) and a["send_at"] > at:
                a.update({"status": "cancelled", "cancel_reason": reason, "cancelled_at": at})
                self.state.update_action(a)
                out.append(a["action_id"])
        return out

    # ---- outputs
    def write(self):
        self.out.mkdir(parents=True, exist_ok=True)
        with open(self.out / "decisions.jsonl", "w") as f:
            for d in self.decisions:
                f.write(json.dumps(d) + "\n")
        all_actions = self.state.all_actions()
        live = [a for a in all_actions if a.get("status") in ("published", "scheduled", "superseded")]
        with open(self.out / "actions.jsonl", "w") as f:       # simulator input
            for a in live:
                f.write(json.dumps(a) + "\n")
        with open(self.out / "actions_all.jsonl", "w") as f:   # audit, including cancelled
            for a in all_actions:
                f.write(json.dumps(a) + "\n")
        with open(self.out / "control_log.jsonl", "w") as f:
            for e in self.log:
                f.write(json.dumps(e) + "\n")
        report = latency_report(self.decisions, self.controls)
        report["duplicate_triggers_ignored"] = self.duplicates
        (self.out / "latency_report.json").write_text(json.dumps(report, indent=2))
        dump_viewers(self.state, self.out / "viewer_state", {d["viewer_id"] for d in self.decisions})
        return report


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None


def latency_report(decisions, controls):
    ms = [d["decided_in_ms"] for d in decisions]
    windows = [(c["dependency"], c["at"], c["until"]) for c in controls if "until" in c]
    by_window = {}
    for dep, start, until in windows:
        w = [d["decided_in_ms"] for d in decisions if start <= d["trigger"]["deliver_at"] < until]
        by_window[f"{dep} {start}..{until}"] = {"decisions": len(w), "p95_ms": _pct(w, .95), "max_ms": max(w, default=None)}
    hours = {}
    for d in decisions:
        hours.setdefault(d["trigger"]["deliver_at"][:13], []).append(d["decided_in_ms"])
    peak = max(hours, key=lambda h: len(hours[h]))
    return {"decisions": len(ms), "p50_ms": _pct(ms, .5), "p95_ms": _pct(ms, .95), "p99_ms": _pct(ms, .99),
            "max_ms": max(ms), "over_60s": sum(x > 60000 for x in ms), "over_5min": sum(x > 300000 for x in ms),
            "peak_hour_utc": {"hour": peak, "decisions": len(hours[peak]), "p95_ms": _pct(hours[peak], .95)},
            "dependency_windows": by_window,
            "note": "decided_in_ms = queue wait + measured compute + simulated dependency waits"}


def dump_viewers(state, folder, users):
    """One readable JSON per viewer: everything the agent knew, decided, did and learned."""
    for u in sorted(users):
        (folder / f"{u}.json").write_text(json.dumps({
            "viewer_id": u, "goals": state.goals(user_id=u), "decisions": state.decisions(u),
            "actions": state.actions(u), "learned": state.learned(u)}, indent=1))
