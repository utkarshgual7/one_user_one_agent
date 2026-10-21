import sqlite3
import unittest

from agent.state import State


def ev(eid, user="U-1", kind="app_open", received="2026-10-01T10:00:00.000Z"):
    return {"event_id": eid, "user_id": user, "event": kind, "received_at": received,
            "occurred_at": received, "properties": {}}


class StateTest(unittest.TestCase):
    def test_duplicate_event_stored_once(self):
        s = State()
        self.assertTrue(s.add_event(ev("e1")))
        self.assertFalse(s.add_event(ev("e1")))
        self.assertEqual(len(s.history("U-1", "2026-12-31T00:00:00.000Z")), 1)

    def test_history_only_what_server_had_received(self):
        s = State()
        s.add_event(ev("early", received="2026-10-01T09:00:00.000Z"))
        s.add_event(ev("late", received="2026-10-01T11:00:00.000Z"))
        ids = [e["event_id"] for e in s.history("U-1", "2026-10-01T10:00:00.000Z")]
        self.assertEqual(ids, ["early"])

    def test_one_open_goal_per_viewer(self):
        s = State()
        k = s.start_goal("U-1", "G01", "D-1", "2026-10-01T10:00:00.000Z", "2026-10-02T10:00:00.000Z", "v1")
        with self.assertRaises(sqlite3.IntegrityError):
            s.start_goal("U-1", "G02", "D-2", "2026-10-01T10:05:00.000Z", "2026-10-04T10:05:00.000Z", "v1")
        s.close_goal(k, "succeeded", "play_started E9", "2026-10-01T12:00:00.000Z")
        s.start_goal("U-1", "G02", "D-2", "2026-10-01T12:05:00.000Z", "2026-10-04T12:05:00.000Z", "v1")
        self.assertEqual(s.open_goal("U-1")["goal_id"], "G02")

    def test_one_decision_per_trigger(self):
        s = State()
        d = {"decision_id": "D-1", "viewer_id": "U-1", "trigger": {"event_id": "t1"},
             "decided_at": "2026-10-01T10:00:00.000Z", "status": "NO_ACTION"}
        s.save_decision(d)
        self.assertTrue(s.has_decision("t1"))
        with self.assertRaises(sqlite3.IntegrityError):
            s.save_decision({**d, "decision_id": "D-2"})


if __name__ == "__main__":
    unittest.main()
