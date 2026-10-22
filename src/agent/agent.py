"""One decision for one viewer: facts -> goal -> (model advice) -> plan -> independent checks -> publish.

The agent owns the viewer's single open goal (enforced in state), every decision and every action. Goal choice
is identical for both arms (holdout viewers get a recorded shadow goal and no action), so outcomes can be compared
arm against arm.
"""
import time

from . import clock, facts as facts_mod, goals, planner
from .provider import Budget, BudgetExceeded, ModelError


class StoreError(Exception):
    pass


class Agent:
    def __init__(self, pack, state, book, catalog, verifier, provider, store, cfg):
        self.pack, self.state, self.book, self.cat = pack, state, book, catalog
        self.verifier, self.provider, self.store, self.cfg = verifier, provider, store, cfg

    def decide(self, trigger, decision_id, now, queue_wait_s=0.0):
        """Returns (decision, actions). `now` is when processing starts (deliver_at + queue wait)."""
        t0 = time.perf_counter()
        uid = trigger["user_id"]
        budget = Budget(self.cfg["max_model_calls"], self.cfg["max_tool_calls"])
        degraded = []
        d = {"decision_id": decision_id, "viewer_id": uid, "arm": self.pack.arms.get(uid),
             "trigger": {"event": trigger["event"], "event_id": trigger["event_id"],
                         "received_at": trigger["received_at"], "deliver_at": trigger["deliver_at"]}}

        # 1. Understand the viewer (data-store read can fail: then there is not enough evidence to act).
        try:
            history = self.store.history(uid, now, budget, trigger["event_id"])
        except (StoreError, BudgetExceeded) as e:
            return self._finish(d, t0, budget, now, queue_wait_s, status="NO_ACTION",
                                reason=f"viewer history unavailable ({e}): not enough evidence to act",
                                degraded=[str(e)]), []
        f = facts_mod.build_facts(self.pack, history, trigger, now)
        d["trigger"]["event_age_s"] = f["trigger_age_s"]["value"]
        d["dialect"] = {"used": f["dialect"]["value"], "source": f["dialect"]["source"],
                        "conflicts": f["dialect"]["conflicts"], "resolution": f["dialect"]["resolution"]}
        d["conflicts"] = facts_mod.conflicts(f)

        # 2. Choose a goal (rules), asking the model only when several goals are eligible and acting is possible.
        open_goal = self.state.open_goal(uid)
        g = goals.choose(self.book, f, now, open_goal)
        eligible = [gid for gid in goals.PRIORITY if goals.eligibility(gid, f, now)[0]]
        advice, model = None, {"called": False}
        actionable = f["trigger_age_s"]["value"] <= planner.MAX_TRIGGER_AGE_S and f["dialect"]["value"]
        if not g["keep"] and len(eligible) > 1 and actionable and queue_wait_s < self.cfg["shed_model_after_s"]:
            try:
                advice = self.provider.advise({"eligible_goals": eligible, "facts": _model_view(f)},
                                              trigger["event_id"], now, budget, self.cfg["model_timeout_s"])
                model = {"called": True, "advice": advice}
                if advice["goal_id"] in eligible and advice["goal_id"] != g["goal_id"]:
                    g = goals.choose_specific(self.book, f, now, advice["goal_id"], g)
                elif advice["goal_id"] not in eligible:
                    model["rejected"] = f"model proposed {advice['goal_id']}, not rule-eligible; ignored"
            except (ModelError, BudgetExceeded) as e:
                model = {"called": True, "error": str(e), "fallback": "rules only, messages suppressed"}
                degraded.append(f"model: {e}")
        elif queue_wait_s >= self.cfg["shed_model_after_s"]:
            model = {"called": False, "skipped": f"load shedding: queued {queue_wait_s:.0f}s"}
        d["model"] = model

        # 3. Decide the intervention.
        msgs_by_day = {}
        for a in self.state.actions(uid):
            if a["type"] in ("push", "whatsapp") and a.get("status") != "cancelled":
                msgs_by_day[clock.ist_day(a["send_at"])] = msgs_by_day.get(clock.ist_day(a["send_at"]), 0) + 1
        avoid = {n["note"].split()[0] for n in self.state.learned(uid) if "undeliverable" in n["note"]}
        p = planner.plan(self.cat, f, g, now, d["arm"], msgs_by_day, allow_messages=not degraded, avoid=avoid)
        if avoid:
            p["assumptions"].append(f"learned from earlier outcomes: {sorted(avoid)} undeliverable for this viewer")

        # 4. Verify independently.
        checks = self.verifier.check(uid, f, g, p, now, advice) if (p["action"] != "NO_ACTION" or advice) else \
            {"passed": [], "failed": [], "page_ok": False, "message_ok": False}
        d["checks"] = {"passed": checks["passed"], "failed": checks["failed"]}

        conf, conf_why = planner.confidence(f, g, p)
        d.update({
            "goal": self._goal_view(g),
            "goals_considered": g["considered"],
            "confidence": conf, "confidence_reason": conf_why,
            "assumptions": p["assumptions"] + ["history complete up to " + now + " (late events may still arrive)"],
            "evidence": self._evidence(f, g, p),
            "plan_reasons": p["reasons"],
        })
        return self._publish(d, f, g, p, checks, t0, budget, now, queue_wait_s, degraded)

    # ---- helpers
    def _goal_view(self, g):
        if not g["goal_id"]:
            return {"id": None, "reason": g["reason"]}
        rule = self.book.goals[g["goal_id"]]
        return {"id": g["goal_id"], "name": rule["name"], "deadline": g["deadline"], "kept": g["keep"],
                "target": g.get("target"), "reason": g["reason"],
                "success_signal": {"event": rule["success"]["event"], "condition": rule["success"]["condition"]},
                "rule": self.book.rule_ref(g["goal_id"])}

    def _evidence(self, f, g, p):
        ages = {ref: fact["age_s"] for fact in f.values() for ref in fact["evidence"]}
        refs = list(dict.fromkeys(g["evidence"] + f["dialect"]["evidence"] + f["subscription_status"]["evidence"]))
        out = [{"ref": r, "age_s": ages.get(r)} for r in refs]
        if p.get("page"):
            ids = ([p["page"]["hero_title_id"]] if p["page"]["hero_title_id"] else []) + \
                  [t for r in p["page"]["rows"] for t in r["title_ids"]]
            out += [{"ref": f"catalogue:{t}", "age_s": None} for t in ids[:5]]
        return out

    def _publish(self, d, f, g, p, checks, t0, budget, now, queue_wait_s, degraded):
        uid = d["viewer_id"]
        page_ok, msg_ok = p["page"] is not None and checks["page_ok"], p["message"] is not None and checks["message_ok"]
        if p["action"] == "NO_ACTION":
            reason = p["reasons"][0]
        elif not page_ok and [x["check"] for x in checks["failed"]] == ["duplicate_page"]:
            reason = "the same page is already live for this viewer (repeat open in the same visit): nothing new to publish"
        elif not page_ok:
            reason = "plan failed independent checks: " + "; ".join(x["check"] for x in checks["failed"])
        else:
            reason = None
        d = self._finish(d, t0, budget, now, queue_wait_s, status="PUBLISHED" if page_ok else "NO_ACTION",
                         reason=reason, degraded=degraded)
        published_at = d["published_at"]

        # Commit the goal for both arms (holdout = shadow goal, never acted on) unless the independent
        # eligibility check disagreed with the rules that chose it.
        if g["goal_id"] and not any(x["check"] == "eligibility" for x in checks["failed"]):
            if g.get("invalidated"):
                self.state.close_goal(g["invalidated"], "superseded", "situation changed: " + g["considered"][0]["why"],
                                      published_at)
            if not g["keep"]:
                g["deadline"] = clock.shift(published_at, hours=self.book.goals[g["goal_id"]]["success"]["window_hours"])
                d["goal"]["deadline"] = g["deadline"]
                d["goal"]["key"] = self.state.start_goal(uid, g["goal_id"], d["decision_id"], published_at,
                                                         g["deadline"], self.book.rule_ref(g["goal_id"]),
                                                         g.get("target"), d["arm"])
            else:
                d["goal"]["key"] = g["goal_key"]

        actions = []
        if page_ok:
            pg = p["page"]
            actions.append({"type": "home_page", "dialect": pg["dialect"], "hero_title_id": pg["hero_title_id"],
                            "rows": pg["rows"]})
            if msg_ok:
                actions.append(dict(p["message"]))
            elif p["message"]:
                d["plan_reasons"].append("message dropped by checks: " +
                                         "; ".join(x["check"] for x in checks["failed"]))
        content = []
        for a in actions:
            a.update({"decision_id": d["decision_id"], "user_id": uid, "trigger_event_id": d["trigger"]["event_id"],
                      "published_at": published_at, "goal_key": d.get("goal", {}).get("key")})
            if a["type"] == "home_page":
                content = ([f"title:{a['hero_title_id']}"] if a["hero_title_id"] else []) + \
                          [f"title:{t}" for r in a["rows"] for t in r["title_ids"]]
        d["action"] = "+".join(a["type"].upper() for a in actions) or "NO_ACTION"
        d["content"] = content
        return d, actions

    def _finish(self, d, t0, budget, now, queue_wait_s, status, reason, degraded):
        compute_s = time.perf_counter() - t0
        total_s = queue_wait_s + compute_s + budget.waited_s
        d.update({"status": status, "no_action_reason": reason, "degraded": degraded,
                  "budget": budget.summary(),
                  "latency": {"queue_wait_ms": round(queue_wait_s * 1000), "compute_ms": round(compute_s * 1000, 2),
                              "dependency_wait_ms": round(budget.waited_s * 1000)},
                  "decided_in_ms": round(total_s * 1000),
                  "published_at": clock.shift(d["trigger"]["deliver_at"], seconds=total_s)})
        d.setdefault("action", "NO_ACTION")
        return d


def _model_view(f):
    """What the model may see: derived, non-free-text facts only. No titles, descriptions or search queries."""
    keep = ("subscription_status", "ever_paid", "has_played", "has_completed", "signup_at", "last_app_open")
    return {k: f[k]["value"] for k in keep} | {"play_days_14d": len(f["play_days_14d"]["value"]),
                                              "unfinished_series": len(f["unfinished_series"]["value"])}
