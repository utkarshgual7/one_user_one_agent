import json
import unittest
from pathlib import Path

from agent import goals
from agent.loader import Pack

NOW = "2026-10-01T12:00:00.000Z"
GOALS = {g["goal_id"]: g for g in json.loads(
    (Path(__file__).resolve().parents[1] / "pack/config/goals.json").read_text())["goals"]}


def fact(value, source="test", observed_at=NOW):
    return {"value": value, "source": source, "observed_at": observed_at, "age_s": 0,
            "evidence": [f"test:{source}"], "conflicts": [], "resolution": None}


def viewer(**kw):
    base = {"subscription_status": "never", "ever_paid": False, "signup_at": "2026-01-01T00:00:00.000Z",
            "has_played": True, "has_completed": True, "play_days_14d": ["2026-09-30"],
            "unfinished_series": [], "last_app_open": "2026-09-30T10:00:00.000Z", "dialect": "d2"}
    base.update(kw)
    f = {k: fact(v) for k, v in base.items()}
    for k, v in kw.items():
        if isinstance(v, dict) and "value" in v:
            f[k] = v
    return f


def goal_row(goal_id, started=NOW, hours=48, target=None):
    from agent import clock
    return {"goal_id": goal_id, "started_at": started, "deadline": clock.shift(started, hours=hours), "target": target}


def event(kind, at, **props):
    return {"event_id": "e", "event": kind, "received_at": at, "sent_at": at, "occurred_at": at, "properties": props}


class Choose(unittest.TestCase):
    def setUp(self):
        self.book = goals.GoalBook(GOALS)

    def test_new_viewer_who_never_played_gets_first_play(self):
        g = goals.choose(self.book, viewer(signup_at="2026-09-29T12:00:00.000Z", has_played=False,
                                           has_completed=False), NOW)
        self.assertEqual(g["goal_id"], "G01")
        self.assertEqual(g["deadline"], "2026-10-02T12:00:00.000Z")
        self.assertIn("goal_rule:G01@v1", g["evidence"])

    def test_unknown_status_blocks_money_goals(self):
        f = viewer(subscription_status=None, ever_paid=None)
        for gid in ("G04", "G05", "G06", "G08", "G09"):
            ok, why, _ = goals.eligibility(gid, f, NOW)
            self.assertFalse(ok, gid)
            self.assertIn("unknown", why)

    def test_profile_lapsed_without_event_is_not_recent(self):
        ok, why, _ = goals.eligibility("G06", viewer(subscription_status=fact("lapsed", "profile")), NOW)
        self.assertFalse(ok)
        ok, _, _ = goals.eligibility("G06", viewer(
            subscription_status=fact("lapsed", "billing_event", "2026-09-20T00:00:00.000Z")), NOW)
        self.assertTrue(ok)

    def test_unmeasurable_goals_never_chosen(self):
        for gid in ("G10", "G11"):
            ok, why, _ = goals.eligibility(gid, viewer(), NOW)
            self.assertFalse(ok)
            self.assertIn("unmeasurable", why)

    def test_nothing_eligible_is_an_explained_no_goal(self):
        g = goals.choose(self.book, viewer(subscription_status="lapsed"), NOW)
        self.assertIsNone(g["goal_id"])
        self.assertEqual(len(g["considered"]), len(goals.PRIORITY) + 2)

    def test_idle_series_becomes_target(self):
        s = {"slug": "show-a", "dialect": "d2", "episodes_left": 4, "last_played": "2026-09-25T00:00:00.000Z",
             "evidence": "event:p1"}
        g = goals.choose(self.book, viewer(unfinished_series=[s], dialect="d2"), NOW)
        self.assertEqual((g["goal_id"], g["target"]), ("G07", "show-a"))

    def test_series_in_other_dialect_is_not_a_goal(self):
        s = {"slug": "show-a", "dialect": "d3", "episodes_left": 4, "last_played": "2026-09-25T00:00:00.000Z",
             "evidence": "event:p1"}
        ok, why, _ = goals.eligibility("G07", viewer(unfinished_series=[s], dialect="d2"), NOW)
        self.assertFalse(ok)
        self.assertIn("dialect", why)


class Replan(unittest.TestCase):
    def test_purchase_invalidates_open_first_paid_goal(self):
        book = goals.GoalBook(GOALS)
        open_goal = {**goal_row("G05", started="2026-09-30T12:00:00.000Z", hours=168), "goal_key": "k1"}
        g = goals.choose(book, viewer(subscription_status=fact("active", "billing_event"), ever_paid=True), NOW, open_goal)
        self.assertFalse(g["keep"])
        self.assertEqual(g["invalidated"], "k1")
        self.assertEqual(g["considered"][0]["goal_id"], "G05")

    def test_valid_open_goal_is_kept(self):
        book = goals.GoalBook(GOALS)
        open_goal = {**goal_row("G02", started="2026-09-30T12:00:00.000Z", hours=72), "goal_key": "k2"}
        g = goals.choose(book, viewer(has_completed=False), NOW, open_goal)
        self.assertTrue(g["keep"])
        self.assertEqual(g["goal_key"], "k2")


class CorrectedGoalRule(unittest.TestCase):
    def test_g03_correction_changes_success_for_open_goals(self):
        book = goals.GoalBook(GOALS)
        g = goal_row("G03", started="2026-10-01T05:00:00.000Z")
        next_day_open = event("app_open", "2026-10-02T06:00:00.000Z")
        next_day_play = event("play_started", "2026-10-02T06:00:00.000Z", title_id="T1")
        self.assertTrue(goals.success_event(book, g, next_day_open, None))

        touched = book.correct({"at": "2026-10-01T08:30:00.000Z", "goal_id": "G03",
                                "new_success": {"event": "play_started", "condition": "on a later IST calendar day"}})
        self.assertEqual(touched, "G03")
        self.assertEqual(book.version, 2)
        self.assertFalse(goals.success_event(book, g, next_day_open, None))
        self.assertTrue(goals.success_event(book, g, next_day_play, None))
        self.assertEqual(book.goals["G03"]["success"]["window_hours"], 48)  # untouched fields survive
        self.assertEqual(GOALS["G03"]["success"]["event"], "app_open")      # original config not mutated

    def test_same_day_does_not_count_as_return(self):
        book = goals.GoalBook(GOALS)
        g = goal_row("G03", started="2026-10-01T05:00:00.000Z")
        self.assertFalse(goals.success_event(book, g, event("app_open", "2026-10-01T09:00:00.000Z"), None))


if __name__ == "__main__":
    unittest.main()
