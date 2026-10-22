"""One decision for one viewer: facts -> goal (+ model advice) -> plan -> independent checks -> publish.

Goal choice is identical for both arms: holdout viewers get a recorded shadow goal and no action, so outcomes can be
compared arm against arm.
"""
import time
from collections import Counter

from . import clock, facts as facts_mod, goals, planner
from .provider import Budget, BudgetExceeded, ModelError

MESSAGE_TYPES = ("push", "whatsapp")


class StoreError(Exception):
    pass


class Agent:
    def __init__(self, pack, state, book, catalog, verifier, provider, store, cfg):
        self.pack, self.state, self.book, self.cat = pack, state, book, catalog
        self.verifier, self.provider, self.store, self.cfg = verifier, provider, store, cfg

    def decide(self, trigger, decision_id, now, queue_wait_s=0.0):
        """Returns (decision, actions). `now` is when processing starts (deliver_at + queue wait)."""
        start, uid = time.perf_counter(), trigger["user_id"]
        budget = Budget(self.cfg["max_model_calls"], self.cfg["max_tool_calls"])
        decision = {"decision_id": decision_id, "viewer_id": uid, "arm": self.pack.arms.get(uid),
             "trigger": {field: trigger[field] for field in ("event", "event_id", "received_at", "deliver_at")}}

        try:
            history = self.store.history(uid, now, budget, trigger["event_id"])
        except (StoreError, BudgetExceeded) as error:
            reason = f"viewer history unavailable ({error}): not enough evidence to act"
            decision.update({"goal": {"id": None, "reason": reason}, "dialect": None, "content": [], "evidence": [],
                             "confidence": 0.0, "confidence_reason": ["no viewer history could be read"],
                             "assumptions": ["nothing is assumed about a viewer whose history is unreadable"],
                             "checks": {"passed": [], "failed": []}, "conflicts": []})
            return self._finish(decision, start, budget, queue_wait_s, "NO_ACTION", reason, [str(error)]), []

        facts = facts_mod.build_facts(self.pack, history, trigger, now)
        decision["trigger"]["event_age_s"] = facts["trigger_age_s"]["value"]
        decision["dialect"] = {field: facts["dialect"][field] for field in ("source", "conflicts", "resolution")} | {"used": facts["dialect"]["value"]}
        decision["conflicts"] = facts_mod.conflicts(facts)

        goal, decision["model"], degraded = self._choose_goal(uid, facts, trigger["event_id"], now, queue_wait_s, budget)
        plan = self._plan(uid, facts, goal, now, decision["arm"], degraded)
        checks = self._verify(uid, facts, goal, plan, now, decision["model"].get("advice"))
        conf, conf_why = planner.confidence(facts, goal, plan)
        decision.update({"checks": {"passed": checks["passed"], "failed": checks["failed"]},
                  "goal": self._goal_view(goal), "goals_considered": goal["considered"],
                  "confidence": conf, "confidence_reason": conf_why,
                  "assumptions": plan["assumptions"] + [f"history complete up to {now} (late events may still arrive)"],
                  "evidence": self._evidence(facts, goal, plan), "plan_reasons": plan["reasons"]})
        return self._publish(decision, goal, plan, checks, start, budget, queue_wait_s, degraded)

    # ---- steps

    def _choose_goal(self, uid, facts, trigger_id, now, queue_wait_s, budget):
        """Rules choose. The model is asked only when 2+ goals are eligible and acting is possible, and may only
        pick among them. Returns (goal, model record, degraded reasons)."""
        goal = goals.choose(self.book, facts, now, self.state.open_goal(uid))
        if queue_wait_s >= self.cfg["shed_model_after_s"]:
            return goal, {"called": False, "skipped": f"load shedding: queued {queue_wait_s:.0f}s"}, []
        eligible = [gid for gid in goals.PRIORITY if goals.eligibility(gid, facts, now)[0]]
        actionable = facts["trigger_age_s"]["value"] <= planner.MAX_TRIGGER_AGE_S and facts["dialect"]["value"]
        if goal["keep"] or len(eligible) < 2 or not actionable:
            return goal, {"called": False}, []
        try:
            advice = self.provider.advise({"eligible_goals": eligible, "facts": _model_view(facts)},
                                          trigger_id, now, budget, self.cfg["model_timeout_s"])
        except (ModelError, BudgetExceeded) as error:
            return goal, {"called": True, "error": str(error), "fallback": "rules only, messages suppressed"}, [f"model: {error}"]
        model = {"called": True, "advice": advice}
        if advice["goal_id"] not in eligible:
            model["rejected"] = f"model proposed {advice['goal_id']}, not rule-eligible; ignored"
        elif advice["goal_id"] != goal["goal_id"]:
            goal = goals.choose_specific(self.book, facts, now, advice["goal_id"], goal)
        return goal, model, []

    def _plan(self, uid, facts, goal, now, arm, degraded):
        sent = [action for action in self.state.actions(uid) if action["type"] in MESSAGE_TYPES and action.get("status") != "cancelled"]
        per_day = Counter(clock.ist_day(action["send_at"]) for action in sent)
        avoid = {note["note"].split()[0] for note in self.state.learned(uid) if "undeliverable" in note["note"]}
        plan = planner.plan(self.cat, facts, goal, now, arm, per_day, allow_messages=not degraded, avoid=avoid)
        if avoid:
            plan["assumptions"].append(f"learned from earlier outcomes: {sorted(avoid)} undeliverable for this viewer")
        return plan

    def _verify(self, uid, facts, goal, plan, now, advice):
        if plan["action"] == "NO_ACTION" and not advice:
            return {"passed": [], "failed": [], "page_ok": False, "message_ok": False}
        return self.verifier.check(uid, facts, goal, plan, now, advice)

    def _publish(self, decision, goal, plan, checks, start, budget, queue_wait_s, degraded):
        page_ok = plan["page"] is not None and checks["page_ok"]
        decision = self._finish(decision, start, budget, queue_wait_s, "PUBLISHED" if page_ok else "NO_ACTION",
                         _no_action_reason(plan, checks, page_ok), degraded)
        self._commit_goal(decision, goal, checks)
        return decision, self._actions(decision, plan, checks, page_ok)

    def _commit_goal(self, decision, goal, checks):
        """Both arms (holdout = shadow goal), unless the independent eligibility check disagreed."""
        if not goal["goal_id"] or any(check["check"] == "eligibility" for check in checks["failed"]):
            return
        published_at = decision["published_at"]
        if goal.get("invalidated"):
            self.state.close_goal(goal["invalidated"], "superseded", "situation changed: " + goal["considered"][0]["why"], published_at)
        if goal["keep"]:
            decision["goal"]["key"] = goal["goal_key"]
            return
        hours = self.book.goals[goal["goal_id"]]["success"]["window_hours"]
        decision["goal"]["deadline"] = clock.shift(published_at, hours=hours)   # the window starts when the goal is committed
        decision["goal"]["key"] = self.state.start_goal(decision["viewer_id"], goal["goal_id"], decision["decision_id"], published_at,
                                                 decision["goal"]["deadline"], self.book.rule_ref(goal["goal_id"]),
                                                 goal.get("target"), decision["arm"])

    def _actions(self, decision, plan, checks, page_ok):
        actions = []
        if page_ok:
            page = plan["page"]
            actions.append({"type": "home_page", "dialect": page["dialect"], "hero_title_id": page["hero_title_id"],
                            "rows": page["rows"]})
            if plan["message"] and checks["message_ok"]:
                actions.append(dict(plan["message"]))
            elif plan["message"]:
                decision["plan_reasons"].append("message dropped by checks: " + "; ".join(check["check"] for check in checks["failed"]))
            decision["content"] = [f"title:{title_id}" for title_id in _page_titles(page)]
        else:
            decision["content"] = []
        for action in actions:
            action.update({"decision_id": decision["decision_id"], "user_id": decision["viewer_id"],
                      "trigger_event_id": decision["trigger"]["event_id"], "published_at": decision["published_at"],
                      "goal_key": decision.get("goal", {}).get("key")})
        decision["action"] = "+".join(action["type"].upper() for action in actions) or "NO_ACTION"
        return actions

    def _finish(self, decision, start, budget, queue_wait_s, status, reason, degraded):
        compute_s = time.perf_counter() - start
        total_s = queue_wait_s + compute_s + budget.waited_s
        decision.update({"status": status, "no_action_reason": reason, "degraded": degraded, "budget": budget.summary(),
                  "latency": {"queue_wait_ms": round(queue_wait_s * 1000), "compute_ms": round(compute_s * 1000, 2),
                              "dependency_wait_ms": round(budget.waited_s * 1000)},
                  "decided_in_ms": round(total_s * 1000),
                  "published_at": clock.shift(decision["trigger"]["deliver_at"], seconds=total_s)})
        decision.setdefault("action", "NO_ACTION")
        return decision

    # ---- decision-record views

    def _goal_view(self, goal):
        if not goal["goal_id"]:
            return {"id": None, "reason": goal["reason"]}
        rule = self.book.goals[goal["goal_id"]]
        return {"id": goal["goal_id"], "name": rule["name"], "deadline": goal["deadline"], "kept": goal["keep"],
                "target": goal.get("target"), "reason": goal["reason"],
                "success_signal": {"event": rule["success"]["event"], "condition": rule["success"]["condition"]},
                "rule": self.book.rule_ref(goal["goal_id"])}

    def _evidence(self, facts, goal, plan):
        ages = {ref: fact["age_s"] for fact in facts.values() for ref in fact["evidence"]}
        refs = dict.fromkeys(goal["evidence"] + facts["dialect"]["evidence"] + facts["subscription_status"]["evidence"])
        out = [{"ref": reference, "age_s": ages.get(reference)} for reference in refs]
        if plan.get("page"):
            out += [{"ref": f"catalogue:{title_id}", "age_s": None} for title_id in _page_titles(plan["page"])[:5]]
        return out


def _page_titles(page):
    return ([page["hero_title_id"]] if page["hero_title_id"] else []) + [title_id for row in page["rows"] for title_id in row["title_ids"]]


def _no_action_reason(plan, checks, page_ok):
    failed = [check["check"] for check in checks["failed"]]
    if plan["action"] == "NO_ACTION":
        return plan["reasons"][0]
    if page_ok:
        return None
    if failed == ["duplicate_page"]:
        return "the same page is already live for this viewer (repeat open in the same visit): nothing new to publish"
    return "plan failed independent checks: " + "; ".join(failed)


def _model_view(facts):
    """What the model may see: derived facts only. No titles, descriptions or search queries."""
    keep = ("subscription_status", "ever_paid", "has_played", "has_completed", "signup_at", "last_app_open")
    return {key: facts[key]["value"] for key in keep} | {"play_days_14d": len(facts["play_days_14d"]["value"]),
                                              "unfinished_series": len(facts["unfinished_series"]["value"])}
