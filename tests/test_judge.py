import json
import unittest
from pathlib import Path

from agent import goals, impact, planner
from agent.judge import Judge
from agent.loader import Pack
from agent.state import State

GOALS = {g["goal_id"]: g for g in json.loads(
    (Path(__file__).resolve().parents[1] / "pack/config/goals.json").read_text())["goals"]}
START = "2026-10-01T06:00:00.000Z"
UNTIL = "2026-10-15T18:30:00.000Z"


def pack():
    return Pack(root=Path("."), catalogue={"T1": {"content_slug": "s1", "release_date": "2026-01-01"}}, episodes={},
                profiles={}, goals=GOALS, interventions={}, pricing={}, arms={})


def ev(eid, kind, at, user="U-1", **props):
    return {"event_id": eid, "user_id": user, "event": kind, "received_at": at, "sent_at": at, "occurred_at": at,
            "properties": props}


class JudgeTest(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.book = goals.GoalBook(GOALS)
        self.judge = Judge(self.book, pack(), observed_until=UNTIL)

    def goal(self, gid, hours, user="U-1", arm="agent"):
        from agent import clock
        return self.state.start_goal(user, gid, "D-1", START, clock.shift(START, hours=hours), "v1", None, arm)

    def verdict(self, key):
        return next(g for g in self.state.goals() if g["goal_key"] == key)

    def test_success_cites_the_event(self):
        k = self.goal("G01", 24)
        self.state.add_event(ev("p1", "play_started", "2026-10-01T09:00:00.000Z", title_id="T1"))
        self.judge.judge_all(self.state, UNTIL)
        g = self.verdict(k)
        self.assertEqual(g["status"], "succeeded")
        self.assertIn("event:p1", g["verdict_reason"])
        self.assertEqual(g["success_at"], "2026-10-01T09:00:00.000Z")

    def test_event_after_deadline_is_failure(self):
        k = self.goal("G01", 24)
        self.state.add_event(ev("p1", "play_started", "2026-10-03T09:00:00.000Z", title_id="T1"))
        self.judge.judge_all(self.state, UNTIL)
        self.assertEqual(self.verdict(k)["status"], "failed")

    def test_window_past_observed_data_is_inconclusive(self):
        judge = Judge(self.book, pack(), observed_until="2026-10-02T00:00:00.000Z")
        k = self.goal("G05", 168)
        judge.judge_all(self.state, "2026-10-02T00:00:00.000Z")
        self.assertEqual(self.verdict(k)["status"], "inconclusive")

    def test_trial_purchase_does_not_count_as_paid(self):
        k = self.goal("G05", 168)
        self.state.add_event(ev("b1", "purchase_completed", "2026-10-02T09:00:00.000Z", is_trial=True))
        self.judge.judge_all(self.state, UNTIL)
        self.assertEqual(self.verdict(k)["status"], "failed")

    def test_weekly_habit_needs_three_distinct_days(self):
        k = self.goal("G09", 336)
        for i, day in enumerate(("02", "02", "04")):
            self.state.add_event(ev(f"p{i}", "play_started", f"2026-10-{day}T09:00:00.000Z", title_id="T1"))
        self.judge.judge_all(self.state, UNTIL)
        self.assertEqual(self.verdict(k)["status"], "failed")
        k2 = self.goal("G09", 336, user="U-2")
        for i, day in enumerate(("02", "04", "06")):
            self.state.add_event(ev(f"q{i}", "play_started", f"2026-10-{day}T09:00:00.000Z", user="U-2", title_id="T1"))
        self.judge.judge_all(self.state, UNTIL)
        self.assertEqual(self.verdict(k2)["status"], "succeeded")

    def test_corrected_rule_applies_when_judging(self):
        k = self.goal("G03", 48)
        self.state.add_event(ev("o1", "app_open", "2026-10-02T06:00:00.000Z"))
        self.book.correct({"at": "2026-10-01T08:30:00.000Z", "goal_id": "G03",
                           "new_success": {"event": "play_started", "condition": "later IST day"}})
        self.judge.judge_all(self.state, UNTIL)
        g = self.verdict(k)
        self.assertEqual(g["status"], "failed")       # an app open alone no longer counts
        self.assertEqual(self.book.rule_ref("G03"), "goal_rule:G03@v2")
        self.assertEqual(self.book.rule_ref("G07"), "goal_rule:G07@v1")

    def test_organic_success_is_not_credited_and_failed_channel_is_learned(self):
        k = self.goal("G01", 24)
        self.state.save_action({"action_id": "A-1", "decision_id": "D-1", "user_id": "U-1", "type": "push",
                                "published_at": START, "send_at": "2026-10-01T13:30:00.000Z"})
        self.state.add_event(ev("p1", "play_started", "2026-10-01T07:00:00.000Z", title_id="T1"))
        self.state.add_event(ev("f1", "message_failed", "2026-10-01T13:30:00.000Z", channel="push",
                                reason="token_invalid", action_id="A-1"))
        self.judge.judge_all(self.state, UNTIL)
        notes = [n["note"] for n in self.state.learned("U-1")]
        self.assertTrue(any("organic" in n for n in notes))
        self.assertTrue(any(n.startswith("push undeliverable") for n in notes))

    def test_learned_undeliverable_channel_is_avoided(self):
        fact = lambda v: {"value": v}
        f = {"whatsapp_consent": fact(False), "push_consent": fact(True)}
        msg, why = planner.follow_up(None, f, {"goal_id": "G01"}, {}, START, {}, avoid={"push"})
        self.assertIsNone(msg)
        self.assertIn("known to deliver", why)


class ImpactTest(unittest.TestCase):
    def test_compare_difference_and_interval(self):
        c = impact.compare([True] * 30 + [False] * 70, [True] * 20 + [False] * 80)
        self.assertEqual(c["diff_pts"], 10.0)
        self.assertLess(c["ci95_pts"][0], 10.0)
        self.assertGreater(c["ci95_pts"][1], 10.0)

    def test_selection_inflates_served_comparison_with_no_real_effect(self):
        # Identical arms (no effect): engaged viewers succeed and get served; others neither.
        def arm(name):
            return {f"{name}{i}": {"arm": name, "goal_id": "G02", "goal_verdict": "succeeded" if i < 40 else "failed",
                                   "played_7d": i < 40, "completed_7d": False, "purchased_14d": False,
                                   "served": name == "agent" and i < 40} for i in range(100)}
        res = impact.analyse({**arm("agent"), **arm("holdout")})
        self.assertEqual(res["primary_goal_reached"]["diff_pts"], 0.0)
        self.assertEqual(res["secondary"]["played_7d"]["diff_pts"], 0.0)
        self.assertGreater(res["misleading"]["served_vs_everyone_else"]["diff_pts"], 50)


if __name__ == "__main__":
    unittest.main()
