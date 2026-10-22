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
    def on_event(self, replay, e):
        """A live event can settle an open goal early (e.g. a purchase minutes after the decision)."""
        g = replay.state.open_goal(e["user_id"])
        if g and self._success(g, e, replay.state):
            self._close(replay.state, g, "succeeded", e, e["received_at"])
            replay._cancel(lambda a: a.get("goal_key") == g["goal_key"] and a["type"] in ("push", "whatsapp"),
                           f"goal {g['goal_id']} already reached", e["received_at"])

    def on_rule_change(self, replay, goal_id):
        """Goals of this kind that were closed as succeeded under the old rule while their window is still running
        are reopened, so the corrected rule decides them. Nothing else is touched."""
        reopened = []
        for g in replay.state.goals(status="succeeded"):
            if g["goal_id"] == goal_id and g["deadline"] > replay.book.history[-1]["at"]:
                with replay.state.db:
                    replay.state.db.execute("UPDATE goals SET status='open', verdict_reason=NULL, judged_at=NULL "
                                            "WHERE goal_key=?", (g["goal_key"],))  # success_at overwritten on re-close
                reopened.append(g["goal_key"])
        return reopened

    # ---- after the simulator
    def judge_all(self, state, judged_at):
        out = []
        for g in state.goals():
            if g["status"] == "open":
                events = state.history(g["user_id"], self.observed_until)
                hit = next((e for e in sorted(events, key=event_time) if self._success(g, e, state, events)), None)
                if hit:
                    self._close(state, g, "succeeded", hit, judged_at)
                elif g["deadline"] <= self.observed_until:
                    self._close(state, g, "failed", None, judged_at)
                else:
                    self._close(state, g, "inconclusive", None, judged_at,
                                f"window ends {g['deadline']}, data observed only until {self.observed_until}")
            out.append(self.outcome_record(state, g["goal_key"]))
        return out

    def _success(self, g, e, state, events=None):
        if g["goal_id"] == "G09":   # 3 distinct IST play days inside the window
            events = events or state.history(g["user_id"], self.observed_until)
            days = {clock.ist_day(event_time(x)) for x in events if x["event"] == "play_started"
                    and g["started_at"] <= event_time(x) <= g["deadline"] and event_time(x) <= event_time(e)}
            return e["event"] == "play_started" and len(days) >= 3
        return goals.success_event(self.book, g, e, self.pack)

    def _close(self, state, g, status, e, judged_at, reason=None):
        if e:
            reason = f"{e['event']} event:{e['event_id']} at {event_time(e)} (rule {self.book.rule_ref(g['goal_id'])})"
        elif not reason:
            reason = f"no {self.book.goals[g['goal_id']]['success']['event']} matching the rule before {g['deadline']}"
        state.close_goal(g["goal_key"], status, reason, judged_at, event_time(e) if e else None)
        if g.get("arm") == "agent":
            self.learn(state, g, status, e, judged_at)

    def exposure(self, state, g, until):
        """Agent pages actually shown / messages clicked for this viewer between goal start and `until`."""
        ids = {a["action_id"] for a in state.actions(g["user_id"])}
        shown = []
        for x in state.history(g["user_id"], until):
            if x["received_at"] < g["started_at"]:
                continue
            p = x["properties"]
            if x["event"] == "home_page_shown" and p.get("personalised") and p.get("page_action_id") in ids:
                shown.append(f"event:{x['event_id']}")
            elif x["event"] == "message_clicked" and p.get("action_id") in ids:
                shown.append(f"event:{x['event_id']}")
        return shown

    def learn(self, state, g, status, e, at):
        notes = [f"{g['goal_id']} {status}"]
        acts = state.actions(g["user_id"])
        fails = [x for x in state.history(g["user_id"], self.observed_until)
                 if x["event"] == "message_failed" and x["properties"].get("action_id") in {a["action_id"] for a in acts}]
        for x in fails:
            notes.append(f"{x['properties']['channel']} undeliverable ({x['properties']['reason']}): avoid this channel")
        if e and self.exposure(state, g, e["received_at"]):
            notes.append("success came after an agent page/message was seen")
        elif e:
            notes.append("success came before any agent exposure: organic, not credited to the agent")
        for n in notes:
            state.learn(g["user_id"], at, n)

    def outcome_record(self, state, goal_key):
        g = next(x for x in state.goals() if x["goal_key"] == goal_key)
        rec = {"goal_key": g["goal_key"], "viewer_id": g["user_id"], "arm": g["arm"], "goal_id": g["goal_id"],
               "decision_id": g["decision_id"], "started_at": g["started_at"], "deadline": g["deadline"],
               "verdict": g["status"], "reason": g["verdict_reason"], "judged_at": g["judged_at"],
               "rule": self.book.rule_ref(g["goal_id"]) if g["goal_id"] in self.book.goals else None}
        if g["status"] == "succeeded" and g["arm"] == "agent":
            rec["success_at"] = g["success_at"]
            rec["exposure_before_success"] = self.exposure(state, g, g["success_at"])
        return rec


def ingest_outcomes(state, path):
    n = new = 0
    with open(path) as f:
        for line in f:
            n += 1
            new += state.add_event(json.loads(line))
    return n, new
