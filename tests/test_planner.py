import unittest
from pathlib import Path

from agent import planner
from agent.loader import Pack
from agent.provider import Budget, BudgetExceeded, MockProvider, ModelError

NOW = "2026-10-01T12:00:00.000Z"  # 17:30 IST


def title(tid, slug, dialect="d2", premium="false", genres="Drama", released="2026-01-01", until=""):
    return {"title_id": tid, "content_slug": slug, "title": f"Title {tid}", "dialect": dialect, "language": "hin",
            "content_type": "show", "genres": genres, "release_date": released,
            "available_from": "2026-01-01T00:00:00.000Z", "available_until": until, "is_premium": premium}


def catalog(titles):
    pack = Pack(root=Path("."), catalogue={t["title_id"]: t for t in titles}, episodes={}, profiles={},
                goals={}, interventions={}, pricing={}, arms={})
    return planner.Catalog(pack, {})


def fact(v, conflicts=()):
    return {"value": v, "source": "test", "observed_at": NOW, "age_s": 0, "evidence": [], "conflicts": list(conflicts),
            "resolution": None}


def facts(**kw):
    base = {"dialect": "d2", "language": "hin", "watched_slugs": [], "unfinished_series": [], "trigger_age_s": 2,
            "whatsapp_consent": True, "push_consent": True, "subscription_status": "never"}
    base.update(kw)
    return {k: fact(v) for k, v in base.items()}


D2 = [title(f"T{i}", f"s{i}") for i in range(12)]
GOAL = {"goal_id": "G01", "reason": "new", "target": None}


class Page(unittest.TestCase):
    def test_page_only_uses_page_dialect_and_available_titles(self):
        cat = catalog(D2 + [title("X1", "x1", dialect="d1"), title("X2", "x2", until="2026-09-01T00:00:00.000Z")])
        p = planner.plan(cat, facts(), GOAL, NOW, "agent", {})
        ids = [t for r in p["page"]["rows"] for t in r["title_ids"]] + [p["page"]["hero_title_id"]]
        self.assertNotIn("X1", ids)
        self.assertNotIn("X2", ids)
        self.assertTrue(all(len(r["title_ids"]) >= 3 for r in p["page"]["rows"]))

    def test_one_title_per_content_slug(self):
        variants = [title("A", "same"), title("B", "same")] + D2
        p = planner.plan(catalog(variants), facts(), GOAL, NOW, "agent", {})
        ids = [t for r in p["page"]["rows"] for t in r["title_ids"]]
        self.assertFalse({"A", "B"} <= set(ids))

    def test_thin_dialect_abstains(self):
        p = planner.plan(catalog([title("T1", "a", dialect="d3"), title("T2", "b", dialect="d3")]),
                         facts(dialect="d3"), GOAL, NOW, "agent", {})
        self.assertEqual(p["action"], "NO_ACTION")
        self.assertIn("not enough available titles in d3", p["reasons"][0])

    def test_withdrawn_title_never_used(self):
        cat = catalog(D2)
        cat.withdraw("T0", "2026-10-01T07:00:00.000Z")
        p = planner.plan(cat, facts(), GOAL, NOW, "agent", {})
        ids = [t for r in p["page"]["rows"] for t in r["title_ids"]] + [p["page"]["hero_title_id"]]
        self.assertNotIn("T0", ids)


class Abstain(unittest.TestCase):
    def test_holdout_gets_nothing(self):
        p = planner.plan(catalog(D2), facts(), GOAL, NOW, "holdout", {})
        self.assertEqual(p["action"], "NO_ACTION")
        self.assertIn("holdout", p["reasons"][0])

    def test_buffered_trigger_gets_nothing(self):
        p = planner.plan(catalog(D2), facts(trigger_age_s=3 * 86400), GOAL, NOW, "agent", {})
        self.assertEqual(p["action"], "NO_ACTION")
        self.assertIn("buffered", p["reasons"][0])

    def test_no_goal_gets_nothing(self):
        p = planner.plan(catalog(D2), facts(), {"goal_id": None, "reason": "no measurable goal"}, NOW, "agent", {})
        self.assertEqual(p["action"], "NO_ACTION")


class Message(unittest.TestCase):
    def test_follow_up_is_next_evening_and_prefers_whatsapp(self):
        p = planner.plan(catalog(D2), facts(), GOAL, NOW, "agent", {})
        m = p["message"]
        self.assertEqual(m["type"], "whatsapp")
        self.assertEqual(m["send_at"], "2026-10-02T13:30:00.000Z")  # 19:00 IST next day
        self.assertEqual(m["template_id"], "wa_recommend_title_v1")

    def test_no_consent_no_message(self):
        p = planner.plan(catalog(D2), facts(whatsapp_consent=False, push_consent=False), GOAL, NOW, "agent", {})
        self.assertIsNone(p["message"])

    def test_day_already_used_no_message(self):
        p = planner.plan(catalog(D2), facts(), GOAL, NOW, "agent", {"2026-10-02": 1})
        self.assertIsNone(p["message"])

    def test_degraded_mode_drops_messages_keeps_page(self):
        p = planner.plan(catalog(D2), facts(), GOAL, NOW, "agent", {}, allow_messages=False)
        self.assertEqual(p["action"], "HOME_PAGE")
        self.assertIsNone(p["message"])


class Provider(unittest.TestCase):
    REQ = {"eligible_goals": ["G02", "G05"]}

    def test_timeout_fault_raises_and_counts_wait(self):
        m = MockProvider([{"trigger_event_id": "t1", "fault": "timeout", "seconds": 45}])
        b = Budget()
        with self.assertRaises(ModelError):
            m.advise(self.REQ, "t1", NOW, b, timeout_s=5)
        self.assertEqual(b.waited_s, 5)

    def test_malformed_output_is_error_not_partial(self):
        m = MockProvider([{"trigger_event_id": "t1", "fault": "malformed_output",
                           "model_output": "{\"goal_id\": \"G02\", \"eligible\": tr"}])
        with self.assertRaises(ModelError):
            m.advise(self.REQ, "t1", NOW, Budget(), timeout_s=5)

    def test_outage_raises(self):
        m = MockProvider(health=lambda dep, now: "unavailable")
        with self.assertRaises(ModelError):
            m.advise(self.REQ, "t9", NOW, Budget(), timeout_s=5)

    def test_unsupported_claim_is_returned_as_is_for_the_caller_to_reject(self):
        out = {"goal_id": "G04", "eligible": True, "confidence": 0.97, "reason": "confirmed in billing"}
        m = MockProvider([{"trigger_event_id": "t1", "fault": "unsupported_claim", "model_output": out}])
        self.assertEqual(m.advise(self.REQ, "t1", NOW, Budget(), timeout_s=5)["goal_id"], "G04")

    def test_budget_is_enforced(self):
        b = Budget(max_model_calls=1)
        m = MockProvider()
        m.advise(self.REQ, "t1", NOW, b, timeout_s=5)
        with self.assertRaises(BudgetExceeded):
            m.advise(self.REQ, "t2", NOW, b, timeout_s=5)


if __name__ == "__main__":
    unittest.main()
