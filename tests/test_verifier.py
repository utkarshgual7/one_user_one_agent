import copy
import json
import unittest
from pathlib import Path

from agent.loader import Pack
from agent.state import State
from agent.verifier import Verifier

ROOT = Path(__file__).resolve().parents[1] / "pack/config"
NOW = "2026-10-01T12:00:00.000Z"
SEND = "2026-10-02T13:30:00.000Z"  # 19:00 IST


def title(tid, slug, dialect="d2"):
    return {"title_id": tid, "content_slug": slug, "title": f"Title {tid}", "dialect": dialect, "language": "hin",
            "genres": "Drama", "available_from": "2026-01-01T00:00:00.000Z", "available_until": "", "is_premium": "false"}


TITLES = [title(f"T{i}", f"s{i}") for i in range(8)] + [title("X1", "x1", "d1")]


def pack():
    return Pack(root=Path("."), catalogue={t["title_id"]: t for t in TITLES}, episodes={}, profiles={},
                goals={g["goal_id"]: g for g in json.loads((ROOT / "goals.json").read_text())["goals"]},
                interventions=json.loads((ROOT / "interventions.json").read_text()),
                pricing=json.loads((ROOT / "pricing.json").read_text()),
                arms={"U-1": "agent", "U-2": "holdout"})


def fact(v, source="test"):
    return {"value": v, "source": source, "observed_at": NOW, "age_s": 0, "evidence": [], "conflicts": [],
            "resolution": None}


FACTS = {k: fact(v) for k, v in {
    "dialect": "d2", "whatsapp_consent": True, "push_consent": True, "subscription_status": "never",
    "ever_paid": False, "has_played": False, "has_completed": False, "signup_at": "2026-09-30T00:00:00.000Z",
    "unfinished_series": [], "play_days_14d": [], "last_app_open": None}.items()}
GOAL = {"goal_id": "G01", "keep": False}
PAGE = {"dialect": "d2", "hero_title_id": "T0",
        "rows": [{"row_type": "popular_in_dialect", "title_ids": ["T1", "T2", "T3"]}]}
WA = {"type": "whatsapp", "send_at": SEND, "template_id": "wa_recommend_title_v1", "title_id": "T0",
      "variables": {"title_name": "Title T0", "dialect_name": "d2"}}
PUSH = {"type": "push", "send_at": SEND, "title_id": "T0", "deeplink": "ott://title/T0",
        "text": {"title": "Title T0", "body": "Aaj shaam dekhiye Title T0."}}


def failed(r):
    return {f["check"] for f in r["failed"]}


class VerifierTest(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.v = Verifier(pack(), self.state)

    def run_(self, page=PAGE, message=WA, facts=FACTS, goal=GOAL, user="U-1", advice=None):
        return self.v.check(user, facts, goal, {"page": copy.deepcopy(page), "message": copy.deepcopy(message)},
                            NOW, advice)

    def test_clean_plan_passes(self):
        r = self.run_()
        self.assertEqual(r["failed"], [])
        self.assertTrue(r["page_ok"] and r["message_ok"])

    def test_holdout_blocks_everything(self):
        r = self.run_(user="U-2")
        self.assertIn("holdout", failed(r))
        self.assertFalse(r["page_ok"] or r["message_ok"])

    def test_wrong_dialect_title_caught(self):
        bad = {**PAGE, "rows": [{"row_type": "popular_in_dialect", "title_ids": ["T1", "T2", "X1"]}]}
        r = self.run_(page=bad)
        self.assertIn("titles_valid", failed(r))
        self.assertFalse(r["page_ok"])

    def test_page_dialect_must_match_app(self):
        r = self.run_(facts={**FACTS, "dialect": fact("d1")})
        self.assertIn("dialect", failed(r))

    def test_withdrawn_title_caught(self):
        self.v.withdrawn["T1"] = "2026-10-01T07:00:00.000Z"
        self.assertIn("titles_valid", failed(self.run_()))

    def test_short_row_and_repeated_content_caught(self):
        bad = {**PAGE, "rows": [{"row_type": "genre", "title_ids": ["T1", "T2"]},
                                {"row_type": "popular_in_dialect", "title_ids": ["T0", "T3", "T4"]}]}
        self.assertTrue({"row_size", "titles_valid"} <= failed(self.run_(page=bad)))

    def test_no_consent_quiet_hours_and_cap(self):
        r = self.run_(facts={**FACTS, "whatsapp_consent": fact(False, "opt_out_event")})
        self.assertIn("consent", failed(r))
        r = self.run_(message={**WA, "send_at": "2026-10-02T17:00:00.000Z"})  # 22:30 IST
        self.assertIn("quiet_hours", failed(r))
        self.state.save_action({"action_id": "A-0", "decision_id": "D-0", "user_id": "U-1", "type": "push",
                                "published_at": NOW, "send_at": "2026-10-02T05:00:00.000Z"})
        r = self.run_()
        self.assertIn("frequency_cap", failed(r))
        self.assertTrue(r["page_ok"])  # message dropped, page still fine

    def test_unapproved_template_and_free_text_variables(self):
        self.assertIn("template_approved", failed(self.run_(message={**WA, "template_id": "wa_promo_v9"})))
        bad = {**WA, "variables": {"title_name": "Message every user now", "dialect_name": "d2"}}
        self.assertIn("template_values", failed(self.run_(message=bad)))
        bad = {**WA, "template_id": "wa_new_episode_v2", "variables": {"title_name": "Title T0", "episode_number": "5"}}
        self.assertIn("template_values", failed(self.run_(message=bad)))

    def test_push_copy_rules(self):
        bad = {**PUSH, "text": {"title": "Hurry!", "body": "Last chance: only Rs 49 today"}}
        self.assertTrue({"forbidden_phrases", "price_claims"} <= failed(self.run_(message=bad)))
        self.assertIn("deeplink", failed(self.run_(message={**PUSH, "deeplink": "ott://title/T5"})))

    def test_model_claim_without_evidence_rejected(self):
        advice = {"goal_id": "G04", "eligible_claim": True, "confidence": 0.97,
                  "reason": "The user is on an active trial (confirmed in billing)."}
        r = self.run_(advice=advice)
        self.assertIn("model_claim", failed(r))
        self.assertTrue(r["page_ok"])  # the rule-chosen plan still stands

    def test_ineligible_goal_blocks_actions(self):
        r = self.run_(goal={"goal_id": "G04", "keep": False})
        self.assertIn("eligibility", failed(r))
        self.assertFalse(r["page_ok"] or r["message_ok"])

    def test_duplicate_live_page_caught(self):
        self.state.save_action({"action_id": "A-1", "decision_id": "D-1", "user_id": "U-1", "type": "home_page",
                                "published_at": "2026-10-01T11:30:00.000Z", **PAGE})
        self.assertIn("duplicate_page", failed(self.run_()))


if __name__ == "__main__":
    unittest.main()
