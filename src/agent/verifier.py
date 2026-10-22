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
        interventions = pack.interventions
        self.rows_cfg = interventions["home_page"]["rows"]
        self.copy_cfg = interventions["push"]["copy"]
        self.quiet = interventions["push"]["quiet_hours_ist"]
        self.templates = {template["template_id"]: template for template in interventions["whatsapp"]["approved_templates"]}
        self.prices = {plan["price_inr"] for plan in pack.pricing["plans"]}
        self.withdrawn = {}  # title_id -> at; fed by the harness from control events, not by the planner

    # ---- helpers
    def _available(self, tid, now):
        title = self.pack.catalogue.get(tid)
        if not title:
            return False
        if title["available_from"] > now or (title["available_until"] and now >= title["available_until"]):
            return False
        return not (tid in self.withdrawn and now >= self.withdrawn[tid])

    def _quiet(self, send_at):
        local_time = clock.parse(send_at).astimezone(clock.IST).strftime("%H:%M")
        start, end = self.quiet
        return local_time >= start or local_time < end

    # ---- checks
    def check(self, user_id, facts, goal, plan, now, advice=None):
        """Returns {"passed": [...], "failed": [{"check", "detail"}], "page_ok", "message_ok", "goal_ok"}."""
        passed, failed = [], []

        def rec(name, is_ok, detail=""):
            (passed.append(name) if is_ok else failed.append({"check": name, "detail": detail}))
            return is_ok

        arm_ok = rec("holdout", self.pack.arms.get(user_id) == "agent",
                     f"arm is {self.pack.arms.get(user_id)!r}: no action allowed")

        goal_ok = True
        if goal.get("goal_id"):
            is_ok, why, _ = goals.eligibility(goal["goal_id"], facts, now)
            goal_ok = rec("eligibility", is_ok or goal.get("keep", False), f"{goal['goal_id']}: {why}")
            goal_ok &= rec("measurable_goal", goal["goal_id"] not in goals.UNMEASURABLE,
                           goals.UNMEASURABLE.get(goal["goal_id"], ""))
        if advice and advice.get("goal_id") and advice["goal_id"] != goal.get("goal_id"):
            is_ok, why, _ = goals.eligibility(advice["goal_id"], facts, now)
            rec("model_claim", is_ok, f"model proposed {advice['goal_id']} ({advice.get('reason', '')!r}) "
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

    def _check_page(self, page, facts, now, rec):
        dialect = page["dialect"]
        is_ok = rec("dialect", dialect is not None and dialect == facts["dialect"]["value"] and dialect in self.pack.dialects,
                 f"page dialect {dialect!r} vs app setting {facts['dialect']['value']!r}")
        rows = page["rows"]
        is_ok &= rec("row_count", len(rows) <= self.rows_cfg["max_rows"], f"{len(rows)} rows")
        seen, bad = set(), []
        ids = ([page["hero_title_id"]] if page.get("hero_title_id") else []) + [title for row in rows for title in row["title_ids"]]
        for tid in ids:
            title = self.pack.catalogue.get(tid)
            if not title or title["dialect"] != dialect:
                bad.append(f"{tid}: not in {dialect}")
            elif not self._available(tid, now):
                bad.append(f"{tid}: not available")
            elif title["content_slug"] in seen:
                bad.append(f"{tid}: content repeated")
            else:
                seen.add(title["content_slug"])
        is_ok &= rec("titles_valid", not bad, "; ".join(bad[:5]))
        short = [row["row_type"] for row in rows if not (self.rows_cfg["min_titles_per_row"] <= len(row["title_ids"])
                                                   <= self.rows_cfg["max_titles_per_row"])]
        is_ok &= rec("row_size", not short, f"rows outside 3-10 titles: {short}")
        is_ok &= rec("row_type", all(row["row_type"] in self.rows_cfg["row_types"] for row in rows), "unknown row type")
        return is_ok

    def _same_live_page(self, user_id, page, now):
        ttl = self.pack.interventions["home_page"]["ttl_minutes"]
        for action in self.state.actions(user_id):
            if action["type"] == "home_page" and clock.age_s(action["published_at"], now) < ttl * 60 \
                    and action["dialect"] == page["dialect"] and action["rows"] == page["rows"] \
                    and action.get("hero_title_id") == page.get("hero_title_id"):
                return True
        return False

    def _check_message(self, user_id, msg, facts, rec):
        channel, send_at, tid = msg["type"], msg["send_at"], msg.get("title_id")
        is_ok = rec("channel", channel in MESSAGE_TYPES, f"unknown channel {channel}")
        is_ok &= rec("consent", bool(facts[f"{channel}_consent"]["value"]),
                  f"{channel} consent is {facts[f'{channel}_consent']['value']} ({facts[f'{channel}_consent']['source']})")
        is_ok &= rec("quiet_hours", not self._quiet(send_at), f"send_at {send_at} is inside 22:00-08:00 IST")
        day = clock.ist_day(send_at)
        sent = [action for action in self.state.actions(user_id) if action["type"] in MESSAGE_TYPES and clock.ist_day(action["send_at"]) == day]
        is_ok &= rec("frequency_cap", not sent, f"already {len(sent)} message(s) on {day}")
        title = self.pack.catalogue.get(tid)
        is_ok &= rec("message_title", bool(title) and title["dialect"] == facts["dialect"]["value"] and self._available(tid, send_at),
                  f"{tid} must exist, be in {facts['dialect']['value']} and be available at {send_at}")
        if channel == "whatsapp":
            tpl = self.templates.get(msg.get("template_id"))
            is_ok &= rec("template_approved", bool(tpl), f"{msg.get('template_id')} is not approved")
            if tpl:
                is_ok &= rec("template_variables", set(msg.get("variables", {})) == set(tpl["variables"]),
                          f"variables {sorted(msg.get('variables', {}))} vs {tpl['variables']}")
                is_ok &= rec("template_values", self._values_from_data(msg, title), "variable values not from catalogue/pricing")
        else:
            text = msg.get("text", {})
            is_ok &= rec("copy_length", len(text.get("title", "")) <= self.copy_cfg["title_max_chars"]
                      and len(text.get("body", "")) <= self.copy_cfg["body_max_chars"], "push copy too long")
            joined = (text.get("title", "") + " " + text.get("body", "")).lower()
            hits = [phrase for phrase in self.copy_cfg["forbidden_phrases"] if phrase in joined]
            is_ok &= rec("forbidden_phrases", not hits, f"contains {hits}")
            prices = {int(number) for number in re.findall(r"(?:₹|rs\.?|inr)\s*(\d+)", joined)}
            is_ok &= rec("price_claims", prices <= self.prices, f"unknown prices {prices - self.prices}")
            is_ok &= rec("deeplink", msg.get("deeplink") == f"ott://title/{tid}", "deeplink must point at the named title")
        return is_ok

    def _values_from_data(self, msg, title):
        variables = msg.get("variables", {})
        allowed = {"title_name": title and title["title"], "dialect_name": title and title["dialect"],
                   "plan_price": None, "episode_number": None}
        for name, val in variables.items():
            if name == "plan_price":
                if not any(str(price) in str(val) for price in self.prices):
                    return False
            elif name == "episode_number" or allowed.get(name) != val:
                return False  # episode_number: no evidence of a new episode is ever available, so never allowed
        return True
