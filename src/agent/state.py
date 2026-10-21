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
    verdict_reason TEXT, judged_at TEXT, rule_version TEXT);
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
    def add_event(self, e):
        """Store one event. Returns False if this event_id was already stored."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
            (e["event_id"], e["user_id"], e["event"], e["received_at"], json.dumps(e)))
        return cur.rowcount == 1

    def ingest_history(self, path):
        """Bulk-load the 30-day history. Returns (lines read, duplicates skipped)."""
        before = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        n = 0
        with self.db:
            for e in iter_jsonl(path):
                n += 1
                self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                    (e["event_id"], e["user_id"], e["event"], e["received_at"], json.dumps(e)))
        added = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] - before
        return n, n - added

    def history(self, user_id, as_of):
        """Everything the server had received for this viewer by `as_of`, in server-received order.
        Phone clocks are not trusted for ordering."""
        rows = self.db.execute(
            "SELECT body FROM events WHERE user_id = ? AND received_at <= ? ORDER BY received_at",
            (user_id, as_of))
        return [json.loads(r["body"]) for r in rows]

    # ---- decisions
    def has_decision(self, trigger_event_id):
        return self.db.execute(
            "SELECT 1 FROM decisions WHERE trigger_event_id = ?", (trigger_event_id,)).fetchone() is not None

    def save_decision(self, d):
        with self.db:
            self.db.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                (d["decision_id"], d["viewer_id"], d["trigger"]["event_id"], d["decided_at"], d["status"],
                 json.dumps(d)))

    def decisions(self, user_id):
        rows = self.db.execute("SELECT body FROM decisions WHERE user_id = ? ORDER BY decided_at", (user_id,))
        return [json.loads(r["body"]) for r in rows]

    # ---- goals
    def open_goal(self, user_id):
        r = self.db.execute("SELECT * FROM goals WHERE user_id = ? AND status = 'open'", (user_id,)).fetchone()
        return dict(r) if r else None

    def start_goal(self, user_id, goal_id, decision_id, started_at, deadline, rule_version):
        key = f"{user_id}:{goal_id}:{started_at}"
        with self.db:
            self.db.execute(
                "INSERT INTO goals VALUES (?,?,?,?,?,?, 'open', NULL, NULL, ?)",
                (key, user_id, goal_id, decision_id, started_at, deadline, rule_version))
        return key

    def close_goal(self, goal_key, status, reason, judged_at):
        with self.db:
            self.db.execute(
                "UPDATE goals SET status = ?, verdict_reason = ?, judged_at = ? WHERE goal_key = ?",
                (status, reason, judged_at, goal_key))

    def goals(self, user_id=None, status=None):
        sql, args = "SELECT * FROM goals WHERE 1=1", []
        if user_id:
            sql, args = sql + " AND user_id = ?", args + [user_id]
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        return [dict(r) for r in self.db.execute(sql, args)]

    # ---- actions
    def save_action(self, a):
        with self.db:
            self.db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?)",
                (a["action_id"], a["decision_id"], a["user_id"], a["type"], a["published_at"], json.dumps(a)))

    def actions(self, user_id):
        rows = self.db.execute("SELECT body FROM actions WHERE user_id = ? ORDER BY published_at", (user_id,))
        return [json.loads(r["body"]) for r in rows]

    # ---- learning
    def learn(self, user_id, at, note):
        with self.db:
            self.db.execute("INSERT INTO learned VALUES (?,?,?)", (user_id, at, note))

    def learned(self, user_id):
        return [dict(r) for r in self.db.execute(
            "SELECT at, note FROM learned WHERE user_id = ? ORDER BY at", (user_id,))]
