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
            "store_timeout_s": 5.0, "shed_model_after_s": 2.0}  # a page must land before the viewer reaches home (3-40 s)


def _h01(*parts):
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


class Health:
    """Dependency status at a moment, from control events. Active only inside each stated window."""

    def __init__(self, controls):
        self.windows = [(control["dependency"], control["type"], control["at"], control["until"]) for control in controls
                        if control["type"] in ("dependency_unavailable", "dependency_degraded")]

    def __call__(self, dep, now):
        for window_dependency, kind, start, until in self.windows:
            if window_dependency == dep and start <= now < until:
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
        plays = [json.loads(row[0]) for row in self.state.db.execute("SELECT body FROM events WHERE event='play_started'")]
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
        for action in actions:
            self.n_act += 1
            action["action_id"] = f"A-{self.n_act:06d}"
            action["status"] = "scheduled" if action["type"] in ("push", "whatsapp") else "published"
            self.state.save_action(action)

    # ---- stream
    def stream(self):
        items = [(control["at"], 0, "control", control) for control in self.controls]
        items += [(event["deliver_at"], 1, "live", event) for event in self.pack.replay("live_events")]
        items += [(trigger["deliver_at"], 2, "trigger", trigger) for trigger in self.pack.replay("triggers")]
        return sorted(items, key=lambda item: (item[0], item[1]))

    def run(self):
        free = [0.0] * self.cfg["workers"]       # epoch seconds when each worker is next free
        heapq.heapify(free)
        for _time, _priority, kind, item in self.stream():
            if kind == "control":
                self.on_control(item)
            elif kind == "live":
                self.on_live(item)
            else:
                self.on_trigger(item, free)
        return self.write()

    def on_trigger(self, trigger, free):
        if self.state.has_decision(trigger["event_id"]):
            self.duplicates += 1
            self.log.append({"at": trigger["deliver_at"], "type": "duplicate_trigger", "event_id": trigger["event_id"],
                             "user_id": trigger["user_id"], "response": "ignored: decision already exists for this trigger"})
            return
        self.state.add_event({key: value for key, value in trigger.items() if key != "deliver_at"})
        deliver = clock.parse(trigger["deliver_at"]).timestamp()
        start = max(deliver, heapq.heappop(free))
        now = clock.shift(trigger["deliver_at"], seconds=start - deliver)
        decision, actions = self.agent.decide(trigger, self._decision_id(), now, queue_wait_s=start - deliver)
        heapq.heappush(free, deliver + decision["decided_in_ms"] / 1000)
        self._save_actions(actions)
        decision["action_ids"] = [action["action_id"] for action in actions]
        self.state.save_decision({**decision, "decided_at": decision["published_at"]})
        self.decisions.append(decision)

    def on_live(self, event):
        event = {key: value for key, value in event.items() if key != "deliver_at"}
        if not self.state.add_event(event):
            return
        if event["event"] == "notification_opt_out":
            self._cancel(lambda action: action["user_id"] == event["user_id"] and action["type"] == event["properties"].get("channel"),
                         f"viewer opted out of {event['properties'].get('channel')}", event["received_at"])
        self.judge.on_event(self, event)

    def on_control(self, control):
        """Each surprise touches only the viewers and decisions it affects."""
        handler = {"title_withdrawn": self._on_withdrawn, "goal_rule_corrected": self._on_rule_corrected,
                   "dependency_unavailable": self._on_dependency, "dependency_degraded": self._on_dependency
                   }.get(control["type"], lambda control: {"response": "unknown control type: logged, not acted on"})
        self.log.append({"at": control["at"], "type": control["type"], "detail": control, **handler(control)})

    def _on_withdrawn(self, control):
        tid, withdrawn_at = control["title_id"], control["at"]
        self.cat.withdraw(tid, withdrawn_at)
        self.verifier.withdrawn[tid] = withdrawn_at
        return {"affected_pages": self._repair_pages(tid, withdrawn_at),
                "cancelled_messages": self._cancel(lambda action: action["type"] in ("push", "whatsapp") and action.get("title_id") == tid,
                                                   f"title {tid} withdrawn", withdrawn_at)}

    def _on_rule_corrected(self, control):
        gid = control["goal_id"]
        self.book.correct(control)
        touched = [goal for goal in self.state.goals(status="open") if goal["goal_id"] == gid]
        return {"affected_open_goals": len(touched), "rule_version": self.book.version,
                "response": f"{gid} success rule replaced; {len(touched)} open {gid} goals will be judged by the new "
                            "rule; no other goal or viewer reprocessed",
                "reopened_for_rejudging": self.judge.on_rule_change(self, gid)}

    def _on_dependency(self, control):
        # The fault itself is injected through Health; here it is only noted.
        return {"response": f"{control['dependency']} {control['type'].split('_')[1]} until {control['until']}: decisions fall back "
                            "(model: rules only, no messages; store: retry once, else no action)"}

    def _repair_pages(self, tid, withdrawn_at):
        """Republish live pages that contain a withdrawn title, without it. Rows that drop below 3 are removed."""
        ttl = self.pack.interventions["home_page"]["ttl_minutes"] * 60
        repaired = []
        for action in self.state.all_actions():
            if action["type"] != "home_page" or action.get("status") != "published" or clock.age_s(action["published_at"], withdrawn_at) >= ttl:
                continue
            ids = [action["hero_title_id"]] + [title_id for row in action["rows"] for title_id in row["title_ids"]]
            if tid not in ids:
                continue
            rows = [{**row, "title_ids": [title_id for title_id in row["title_ids"] if title_id != tid]} for row in action["rows"]]
            rows = [row for row in rows if len(row["title_ids"]) >= 3]
            hero = None if action["hero_title_id"] == tid else action["hero_title_id"]
            action["status"] = "superseded"
            self.state.update_action(action)
            new = {key: value for key, value in action.items() if key not in ("action_id", "status")}
            new.update({"rows": rows, "hero_title_id": hero, "published_at": withdrawn_at, "replaces": action["action_id"],
                        "reason": f"title {tid} withdrawn"})
            if rows or hero:
                self._save_actions([new])
            repaired.append({"user_id": action["user_id"], "old_action": action["action_id"],
                             "new_action": new.get("action_id", "none: page empty after removal")})
        return repaired

    def _cancel(self, match, reason, cancelled_at):
        out = []
        for action in self.state.all_actions():
            if action.get("status") == "scheduled" and match(action) and action["send_at"] > cancelled_at:
                action.update({"status": "cancelled", "cancel_reason": reason, "cancelled_at": cancelled_at})
                self.state.update_action(action)
                out.append(action["action_id"])
        return out

    # ---- outputs
    def write(self):
        self.out.mkdir(parents=True, exist_ok=True)
        with open(self.out / "decisions.jsonl", "w") as file:
            for decision in self.decisions:
                file.write(json.dumps(decision) + "\n")
        all_actions = self.state.all_actions()
        live = [action for action in all_actions if action.get("status") in ("published", "scheduled", "superseded")]
        with open(self.out / "actions.jsonl", "w") as file:       # simulator input
            for action in live:
                file.write(json.dumps(action) + "\n")
        with open(self.out / "actions_all.jsonl", "w") as file:   # audit, including cancelled
            for action in all_actions:
                file.write(json.dumps(action) + "\n")
        with open(self.out / "control_log.jsonl", "w") as file:
            for entry in self.log:
                file.write(json.dumps(entry) + "\n")
        report = latency_report(self.decisions, self.controls)
        report["duplicate_triggers_ignored"] = self.duplicates
        (self.out / "latency_report.json").write_text(json.dumps(report, indent=2))
        dump_viewers(self.state, self.out / "viewer_state", {decision["viewer_id"] for decision in self.decisions})
        return report


def _pct(values, fraction):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * fraction))] if values else None


def latency_report(decisions, controls):
    durations_ms = [decision["decided_in_ms"] for decision in decisions]
    windows = [(control["dependency"], control["at"], control["until"]) for control in controls if "until" in control]
    by_window = {}
    for dep, start, until in windows:
        window_durations = [decision["decided_in_ms"] for decision in decisions if start <= decision["trigger"]["deliver_at"] < until]
        by_window[f"{dep} {start}..{until}"] = {"decisions": len(window_durations), "p95_ms": _pct(window_durations, .95), "max_ms": max(window_durations, default=None)}
    hours = {}
    for decision in decisions:
        hours.setdefault(decision["trigger"]["deliver_at"][:13], []).append(decision["decided_in_ms"])
    peak = max(hours, key=lambda hour: len(hours[hour]))
    return {"decisions": len(durations_ms), "p50_ms": _pct(durations_ms, .5), "p95_ms": _pct(durations_ms, .95), "p99_ms": _pct(durations_ms, .99),
            "max_ms": max(durations_ms), "over_60s": sum(duration > 60000 for duration in durations_ms), "over_5min": sum(duration > 300000 for duration in durations_ms),
            "peak_hour_utc": {"hour": peak, "decisions": len(hours[peak]), "p95_ms": _pct(hours[peak], .95)},
            "dependency_windows": by_window,
            "note": "decided_in_ms = queue wait + measured compute + simulated dependency waits"}


def dump_viewers(state, folder, users):
    """One readable JSON per viewer: everything the agent knew, decided, did and learned."""
    for user_id in sorted(users):
        (folder / f"{user_id}.json").write_text(json.dumps({
            "viewer_id": user_id, "goals": state.goals(user_id=user_id), "decisions": state.decisions(user_id),
            "actions": state.actions(user_id), "learned": state.learned(user_id)}, indent=1))
