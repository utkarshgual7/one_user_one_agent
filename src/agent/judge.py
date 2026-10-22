"""Close the loop: judge each goal from attributable signals, and record what was learned per viewer.

Verdicts:
- succeeded: a success event (current rule version) inside the goal window; the event is cited.
- failed: the whole window was observed and no success event appeared.
- inconclusive: the window runs past the data we can see, so absence of success proves nothing.
- superseded: closed earlier because the viewer's situation changed (set during the replay).

Attribution is separate from success: a success counts as "after exposure" only if an agent page was actually
shown, or a message clicked, before it. Causation is never claimed per viewer; it comes from arm vs arm (impact.py).
"""
import json

from . import clock, goals
from .facts import event_time

OBSERVED_UNTIL = "2026-10-15T18:30:00.000Z"   # simulator horizon: replay day + 14 days (IST midnight)


class Judge:
    def __init__(self, book, pack, observed_until=OBSERVED_UNTIL):
        self.book, self.pack, self.observed_until = book, pack, observed_until

    # ---- during the replay
    def on_event(self, replay, event):
        """A live event can settle an open goal early (e.g. a purchase minutes after the decision)."""
        goal = replay.state.open_goal(event["user_id"])
        if goal and self._success(goal, event, replay.state):
            self._close(replay.state, goal, "succeeded", event, event["received_at"])
            replay._cancel(lambda action: action.get("goal_key") == goal["goal_key"] and action["type"] in ("push", "whatsapp"),
                           f"goal {goal['goal_id']} already reached", event["received_at"])

    def on_rule_change(self, replay, goal_id):
        """Goals of this kind that were closed as succeeded under the old rule while their window is still running
        are reopened, so the corrected rule decides them. Nothing else is touched."""
        reopened = []
        for goal in replay.state.goals(status="succeeded"):
            if goal["goal_id"] == goal_id and goal["deadline"] > replay.book.history[-1]["at"]:
                with replay.state.db:
                    replay.state.db.execute("UPDATE goals SET status='open', verdict_reason=NULL, judged_at=NULL "
                                            "WHERE goal_key=?", (goal["goal_key"],))  # success_at overwritten on re-close
                reopened.append(goal["goal_key"])
        return reopened

    # ---- after the simulator
    def judge_all(self, state, judged_at):
        out = []
        for goal in state.goals():
            if goal["status"] == "open":
                events = state.history(goal["user_id"], self.observed_until)
                hit = next((event for event in sorted(events, key=event_time) if self._success(goal, event, state, events)), None)
                if hit:
                    self._close(state, goal, "succeeded", hit, judged_at)
                elif goal["deadline"] <= self.observed_until:
                    self._close(state, goal, "failed", None, judged_at)
                else:
                    self._close(state, goal, "inconclusive", None, judged_at,
                                f"window ends {goal['deadline']}, data observed only until {self.observed_until}")
            out.append(self.outcome_record(state, goal["goal_key"]))
        return out

    def _success(self, goal, event, state, events=None):
        if goal["goal_id"] == "G09":   # 3 distinct IST play days inside the window
            events = events or state.history(goal["user_id"], self.observed_until)
            days = {clock.ist_day(event_time(candidate)) for candidate in events if candidate["event"] == "play_started"
                    and goal["started_at"] <= event_time(candidate) <= goal["deadline"] and event_time(candidate) <= event_time(event)}
            return event["event"] == "play_started" and len(days) >= 3
        return goals.success_event(self.book, goal, event, self.pack)

    def _close(self, state, goal, status, event, judged_at, reason=None):
        if event:
            reason = f"{event['event']} event:{event['event_id']} at {event_time(event)} (rule {self.book.rule_ref(goal['goal_id'])})"
        elif not reason:
            reason = f"no {self.book.goals[goal['goal_id']]['success']['event']} matching the rule before {goal['deadline']}"
        state.close_goal(goal["goal_key"], status, reason, judged_at, event_time(event) if event else None)
        if goal.get("arm") == "agent":
            self.learn(state, goal, status, event, judged_at)

    def exposure(self, state, goal, until):
        """Agent pages actually shown / messages clicked for this viewer between goal start and `until`."""
        ids = {action["action_id"] for action in state.actions(goal["user_id"])}
        shown = []
        for event in state.history(goal["user_id"], until):
            if event["received_at"] < goal["started_at"]:
                continue
            props = event["properties"]
            if event["event"] == "home_page_shown" and props.get("personalised") and props.get("page_action_id") in ids:
                shown.append(f"event:{event['event_id']}")
            elif event["event"] == "message_clicked" and props.get("action_id") in ids:
                shown.append(f"event:{event['event_id']}")
        return shown

    def learn(self, state, goal, status, event, judged_at):
        notes = [f"{goal['goal_id']} {status}"]
        acts = state.actions(goal["user_id"])
        fails = [record for record in state.history(goal["user_id"], self.observed_until)
                 if record["event"] == "message_failed" and record["properties"].get("action_id") in {action["action_id"] for action in acts}]
        for record in fails:
            notes.append(f"{record['properties']['channel']} undeliverable ({record['properties']['reason']}): avoid this channel")
        if event and self.exposure(state, goal, event["received_at"]):
            notes.append("success came after an agent page/message was seen")
        elif event:
            notes.append("success came before any agent exposure: organic, not credited to the agent")
        for note in notes:
            state.learn(goal["user_id"], judged_at, note)

    def outcome_record(self, state, goal_key):
        goal = next(candidate for candidate in state.goals() if candidate["goal_key"] == goal_key)
        rec = {"goal_key": goal["goal_key"], "viewer_id": goal["user_id"], "arm": goal["arm"], "goal_id": goal["goal_id"],
               "decision_id": goal["decision_id"], "started_at": goal["started_at"], "deadline": goal["deadline"],
               "verdict": goal["status"], "reason": goal["verdict_reason"], "judged_at": goal["judged_at"],
               "rule": self.book.rule_ref(goal["goal_id"]) if goal["goal_id"] in self.book.goals else None}
        if goal["status"] == "succeeded" and goal["arm"] == "agent":
            rec["success_at"] = goal["success_at"]
            rec["exposure_before_success"] = self.exposure(state, goal, goal["success_at"])
        return rec


def ingest_outcomes(state, path):
    lines = new = 0
    with open(path) as file:
        for line in file:
            lines += 1
            new += state.add_event(json.loads(line))
    return lines, new
