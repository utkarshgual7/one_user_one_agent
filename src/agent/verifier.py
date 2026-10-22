"""Independent checks run on every planned action before it is published.

Deliberately does not import planner.py: rules are re-read from the pack config (interventions, pricing, arms,
catalogue) and from durable state, so a planner bug cannot also hide in the check. A failed check drops that part
of the plan; it never "fixes" it.
"""
import re

from . import clock, goals

MESSAGE_TYPES = ("push", "whatsapp")


class Verifier:
    def __init__(self, pack, state):
        self.pack, self.state = pack, state
        iv = pack.interventions
        self.rows_cfg = iv["home_page"]["rows"]
        self.copy_cfg = iv["push"]["copy"]
        self.quiet = iv["push"]["quiet_hours_ist"]
        self.templates = {t["template_id"]: t for t in iv["whatsapp"]["approved_templates"]}
        self.prices = {p["price_inr"] for p in pack.pricing["plans"]}
        self.withdrawn = {}  # title_id -> at; fed by the harness from control events, not by the planner

    # ---- helpers
    def _available(self, tid, at):
        t = self.pack.catalogue.get(tid)
        if not t:
            return False
        if t["available_from"] > at or (t["available_until"] and at >= t["available_until"]):
            return False
        return not (tid in self.withdrawn and at >= self.withdrawn[tid])

    def _quiet(self, at):
        h = clock.parse(at).astimezone(clock.IST).strftime("%H:%M")
        start, end = self.quiet
        return h >= start or h < end

    # ---- checks
    def check(self, user_id, facts, goal, plan, now, advice=None):
        """Returns {"passed": [...], "failed": [{"check", "detail"}], "page_ok", "message_ok", "goal_ok"}."""
        passed, failed = [], []

        def rec(name, ok, detail=""):
            (passed.append(name) if ok else failed.append({"check": name, "detail": detail}))
            return ok

        arm_ok = rec("holdout", self.pack.arms.get(user_id) == "agent",
                     f"arm is {self.pack.arms.get(user_id)!r}: no action allowed")

        goal_ok = True
        if goal.get("goal_id"):
            ok, why, _ = goals.eligibility(goal["goal_id"], facts, now)
            goal_ok = rec("eligibility", ok or goal.get("keep", False), f"{goal['goal_id']}: {why}")
            goal_ok &= rec("measurable_goal", goal["goal_id"] not in goals.UNMEASURABLE,
                           goals.UNMEASURABLE.get(goal["goal_id"], ""))
        if advice and advice.get("goal_id") and advice["goal_id"] != goal.get("goal_id"):
            ok, why, _ = goals.eligibility(advice["goal_id"], facts, now)
            rec("model_claim", ok, f"model proposed {advice['goal_id']} ({advice.get('reason', '')!r}) "
                                   f"but rules say: {why}")

        page_ok = bool(plan.get("page")) and arm_ok and goal_ok
        if plan.get("page"):
            page_ok &= self._check_page(plan["page"], facts, now, rec)
            page_ok &= rec("duplicate_page", not self._same_live_page(user_id, plan["page"], now),
                           "identical page already live for this viewer")

        msg = plan.get("message")
        message_ok = bool(msg) and arm_ok and goal_ok
        if msg:
            message_ok &= self._check_message(user_id, msg, facts, rec)
        return {"passed": passed, "failed": failed, "page_ok": page_ok, "message_ok": message_ok,
                "goal_ok": goal_ok and arm_ok}

    def _check_page(self, page, facts, at, rec):
        d = page["dialect"]
        ok = rec("dialect", d is not None and d == facts["dialect"]["value"] and d in self.pack.dialects,
                 f"page dialect {d!r} vs app setting {facts['dialect']['value']!r}")
        rows = page["rows"]
        ok &= rec("row_count", len(rows) <= self.rows_cfg["max_rows"], f"{len(rows)} rows")
        seen, bad = set(), []
        ids = ([page["hero_title_id"]] if page.get("hero_title_id") else []) + [t for r in rows for t in r["title_ids"]]
        for tid in ids:
            t = self.pack.catalogue.get(tid)
            if not t or t["dialect"] != d:
                bad.append(f"{tid}: not in {d}")
            elif not self._available(tid, at):
                bad.append(f"{tid}: not available")
            elif t["content_slug"] in seen:
                bad.append(f"{tid}: content repeated")
            else:
                seen.add(t["content_slug"])
        ok &= rec("titles_valid", not bad, "; ".join(bad[:5]))
        short = [r["row_type"] for r in rows if not (self.rows_cfg["min_titles_per_row"] <= len(r["title_ids"])
                                                   <= self.rows_cfg["max_titles_per_row"])]
        ok &= rec("row_size", not short, f"rows outside 3-10 titles: {short}")
        ok &= rec("row_type", all(r["row_type"] in self.rows_cfg["row_types"] for r in rows), "unknown row type")
        return ok

    def _same_live_page(self, user_id, page, at):
        ttl = self.pack.interventions["home_page"]["ttl_minutes"]
        for a in self.state.actions(user_id):
            if a["type"] == "home_page" and clock.age_s(a["published_at"], at) < ttl * 60 \
                    and a["dialect"] == page["dialect"] and a["rows"] == page["rows"] \
                    and a.get("hero_title_id") == page.get("hero_title_id"):
                return True
        return False

    def _check_message(self, user_id, msg, facts, rec):
        ch, send_at, tid = msg["type"], msg["send_at"], msg.get("title_id")
        ok = rec("channel", ch in MESSAGE_TYPES, f"unknown channel {ch}")
        ok &= rec("consent", bool(facts[f"{ch}_consent"]["value"]),
                  f"{ch} consent is {facts[f'{ch}_consent']['value']} ({facts[f'{ch}_consent']['source']})")
        ok &= rec("quiet_hours", not self._quiet(send_at), f"send_at {send_at} is inside 22:00-08:00 IST")
        day = clock.ist_day(send_at)
        sent = [a for a in self.state.actions(user_id) if a["type"] in MESSAGE_TYPES and clock.ist_day(a["send_at"]) == day]
        ok &= rec("frequency_cap", not sent, f"already {len(sent)} message(s) on {day}")
        t = self.pack.catalogue.get(tid)
        ok &= rec("message_title", bool(t) and t["dialect"] == facts["dialect"]["value"] and self._available(tid, send_at),
                  f"{tid} must exist, be in {facts['dialect']['value']} and be available at {send_at}")
        if ch == "whatsapp":
            tpl = self.templates.get(msg.get("template_id"))
            ok &= rec("template_approved", bool(tpl), f"{msg.get('template_id')} is not approved")
            if tpl:
                ok &= rec("template_variables", set(msg.get("variables", {})) == set(tpl["variables"]),
                          f"variables {sorted(msg.get('variables', {}))} vs {tpl['variables']}")
                ok &= rec("template_values", self._values_from_data(msg, t), "variable values not from catalogue/pricing")
        else:
            text = msg.get("text", {})
            ok &= rec("copy_length", len(text.get("title", "")) <= self.copy_cfg["title_max_chars"]
                      and len(text.get("body", "")) <= self.copy_cfg["body_max_chars"], "push copy too long")
            joined = (text.get("title", "") + " " + text.get("body", "")).lower()
            hits = [p for p in self.copy_cfg["forbidden_phrases"] if p in joined]
            ok &= rec("forbidden_phrases", not hits, f"contains {hits}")
            prices = {int(n) for n in re.findall(r"(?:₹|rs\.?|inr)\s*(\d+)", joined)}
            ok &= rec("price_claims", prices <= self.prices, f"unknown prices {prices - self.prices}")
            ok &= rec("deeplink", msg.get("deeplink") == f"ott://title/{tid}", "deeplink must point at the named title")
        return ok

    def _values_from_data(self, msg, t):
        v = msg.get("variables", {})
        allowed = {"title_name": t and t["title"], "dialect_name": t and t["dialect"],
                   "plan_price": None, "episode_number": None}
        for k, val in v.items():
            if k == "plan_price":
                if not any(str(p) in str(val) for p in self.prices):
                    return False
            elif k == "episode_number" or allowed.get(k) != val:
                return False  # episode_number: no evidence of a new episode is ever available, so never allowed
        return True
