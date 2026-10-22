"""Durable per-viewer memory in SQLite.

Rules live in the database where they can't be bypassed:
- an event_id is stored once (duplicates are ignored),
- a trigger gets at most one decision (replays are idempotent),
- a viewer has at most one open goal (single owner, single plan).
"""
import json
import sqlite3

from .loader import iter_jsonl

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, event TEXT NOT NULL,
    received_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_by_user ON events(user_id, received_at);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, trigger_event_id TEXT NOT NULL UNIQUE,
    decided_at TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS decisions_by_user ON decisions(user_id, decided_at);

CREATE TABLE IF NOT EXISTS goals (
    goal_key TEXT PRIMARY KEY, user_id TEXT NOT NULL, goal_id TEXT NOT NULL, decision_id TEXT NOT NULL,
    started_at TEXT NOT NULL, deadline TEXT NOT NULL, status TEXT NOT NULL,
    verdict_reason TEXT, judged_at TEXT, rule_version TEXT, target TEXT, arm TEXT, success_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_goal ON goals(user_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, user_id TEXT NOT NULL, type TEXT NOT NULL,
    published_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS actions_by_user ON actions(user_id, published_at);

CREATE TABLE IF NOT EXISTS learned (user_id TEXT NOT NULL, at TEXT NOT NULL, note TEXT NOT NULL);
"""


class State:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- events
    def add_event(self, event):
        """Store one event. Returns False if this event_id was already stored."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
            (event["event_id"], event["user_id"], event["event"], event["received_at"], json.dumps(event)))
        return cur.rowcount == 1

    def ingest_history(self, path):
        """Bulk-load the 30-day history. Returns (lines read, duplicates skipped)."""
        before = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        lines_read = 0
        with self.db:
            for event in iter_jsonl(path):
                lines_read += 1
                self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                    (event["event_id"], event["user_id"], event["event"], event["received_at"], json.dumps(event)))
        added = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] - before
        return lines_read, lines_read - added

    def history(self, user_id, as_of):
        """Everything the server had received for this viewer by `as_of`, in server-received order.
        Phone clocks are not trusted for ordering."""
        rows = self.db.execute(
            "SELECT body FROM events WHERE user_id = ? AND received_at <= ? ORDER BY received_at",
            (user_id, as_of))
        return [json.loads(row["body"]) for row in rows]

    # ---- decisions
    def has_decision(self, trigger_event_id):
        return self.db.execute(
            "SELECT 1 FROM decisions WHERE trigger_event_id = ?", (trigger_event_id,)).fetchone() is not None

    def save_decision(self, decision):
        with self.db:
            self.db.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                (decision["decision_id"], decision["viewer_id"], decision["trigger"]["event_id"], decision["decided_at"], decision["status"],
                 json.dumps(decision)))

    def decisions(self, user_id):
        rows = self.db.execute("SELECT body FROM decisions WHERE user_id = ? ORDER BY decided_at", (user_id,))
        return [json.loads(row["body"]) for row in rows]

    # ---- goals
    def open_goal(self, user_id):
        row = self.db.execute("SELECT * FROM goals WHERE user_id = ? AND status = 'open'", (user_id,)).fetchone()
        return dict(row) if row else None

    def start_goal(self, user_id, goal_id, decision_id, started_at, deadline, rule_version, target=None, arm=None):
        """Holdout viewers get the same goal (a shadow goal, never acted on) so both arms are judged alike."""
        key = f"{user_id}:{goal_id}:{started_at}"
        with self.db:
            self.db.execute(
                "INSERT INTO goals VALUES (?,?,?,?,?,?, 'open', NULL, NULL, ?, ?, ?, NULL)",
                (key, user_id, goal_id, decision_id, started_at, deadline, rule_version, target, arm))
        return key

    def close_goal(self, goal_key, status, reason, judged_at, success_at=None):
        with self.db:
            self.db.execute(
                "UPDATE goals SET status = ?, verdict_reason = ?, judged_at = ?, success_at = ? WHERE goal_key = ?",
                (status, reason, judged_at, success_at, goal_key))

    def goals(self, user_id=None, status=None):
        sql, args = "SELECT * FROM goals WHERE 1=1", []
        if user_id:
            sql, args = sql + " AND user_id = ?", args + [user_id]
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        return [dict(row) for row in self.db.execute(sql, args)]

    # ---- actions
    def save_action(self, action):
        with self.db:
            self.db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?)",
                (action["action_id"], action["decision_id"], action["user_id"], action["type"], action["published_at"], json.dumps(action)))

    def update_action(self, action):
        with self.db:
            self.db.execute("UPDATE actions SET body = ? WHERE action_id = ?", (json.dumps(action), action["action_id"]))

    def all_actions(self):
        return [json.loads(row["body"]) for row in self.db.execute("SELECT body FROM actions ORDER BY published_at, action_id")]

    def actions(self, user_id):
        rows = self.db.execute("SELECT body FROM actions WHERE user_id = ? ORDER BY published_at", (user_id,))
        return [json.loads(row["body"]) for row in rows]

    # ---- learning
    def learn(self, user_id, noted_at, note):
        with self.db:
            self.db.execute("INSERT INTO learned VALUES (?,?,?)", (user_id, noted_at, note))

    def learned(self, user_id):
        return [dict(row) for row in self.db.execute(
            "SELECT at, note FROM learned WHERE user_id = ? ORDER BY at", (user_id,))]
