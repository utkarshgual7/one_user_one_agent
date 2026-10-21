import unittest
from pathlib import Path

from agent import facts
from agent.loader import Pack

NOW = "2026-10-01T12:00:00.000Z"


def ev(eid, kind, received, props=None, occurred=None, sent=None, dialect="d2"):
    return {"event_id": eid, "user_id": "U-1", "event": kind, "received_at": received,
            "occurred_at": occurred or received, "sent_at": sent or received,
            "app_dialect": dialect, "app_language": "hin", "properties": props or {}}


def profile(**kw):
    row = {"user_id": "U-1", "signup_at": "2026-01-01T00:00:00.000Z", "subscription_status": "never",
           "preferred_dialect": "d2", "notifications_opt_in": "true", "whatsapp_opt_in": "true",
           "updated_at": "2026-09-29T00:00:00.000Z", "snapshot_at": "2026-09-30T00:00:00.000Z"}
    return {**row, **kw}


def pack(rows):
    return Pack(root=Path("."), catalogue={}, episodes={}, profiles={"U-1": rows}, goals={},
                interventions={}, pricing={}, arms={})


def trigger(link=None, dialect="d2"):
    t = ev("T", "app_open", "2026-10-01T11:59:50.000Z", {"deeplink": link} if link else {}, dialect=dialect)
    t["deliver_at"] = t["received_at"]
    return t


class ConflictingSources(unittest.TestCase):
    def test_three_dialects_uses_app_setting_and_records_others(self):
        f = facts.build_facts(pack([profile(preferred_dialect="d1")]), [],
                              trigger("ott://c/cmp1/d5/title/T9", dialect="d3"), NOW)
        self.assertEqual(f["dialect"]["value"], "d3")
        self.assertEqual(f["dialect"]["source"], "app_setting")
        self.assertEqual(f["dialect"]["conflicts"], ["profile:d1", "campaign_link:d5"])

    def test_purchase_after_snapshot_beats_profile_never(self):
        buy = ev("B", "purchase_completed", "2026-09-30T20:00:00.000Z", {"plan_id": "P_MONTH", "is_trial": False})
        f = facts.build_facts(pack([profile()]), [buy], trigger(), NOW)
        self.assertEqual(f["subscription_status"]["value"], "active")
        self.assertEqual(f["subscription_status"]["conflicts"], ["profile:never"])
        self.assertTrue(f["ever_paid"]["value"])

    def test_duplicate_profile_rows_without_events_stay_unknown(self):
        rows = [profile(subscription_status="never"),
                profile(subscription_status="lapsed", updated_at="2026-09-12T00:00:00.000Z")]
        f = facts.build_facts(pack(rows), [], trigger(), NOW)
        self.assertIsNone(f["subscription_status"]["value"])
        self.assertIsNone(f["ever_paid"]["value"])

    def test_opt_out_event_overrides_profile_consent(self):
        out = ev("O", "notification_opt_out", "2026-09-30T10:00:00.000Z", {"channel": "push"})
        f = facts.build_facts(pack([profile()]), [out], trigger(), NOW)
        self.assertFalse(f["push_consent"]["value"])
        self.assertTrue(f["whatsapp_consent"]["value"])

    def test_new_viewer_without_profile_has_no_consent(self):
        f = facts.build_facts(pack([]), [], trigger(), NOW)
        self.assertFalse(f["push_consent"]["value"])
        self.assertIsNone(f["subscription_status"]["value"])


class WrongPhoneClock(unittest.TestCase):
    def test_trigger_age_ignores_phone_clock_offset(self):
        # Phone clock 5 h fast; open buffered offline for 2 days, then sent.
        t = ev("T", "app_open", "2026-10-01T10:00:00.000Z",
               occurred="2026-09-29T15:00:00.000Z", sent="2026-10-01T15:00:00.000Z")
        t["deliver_at"] = "2026-10-01T10:00:01.000Z"
        self.assertEqual(round(facts.trigger_age_s(t)), 2 * 86400 + 1)
        self.assertEqual(facts.event_time(t), "2026-09-29T10:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
