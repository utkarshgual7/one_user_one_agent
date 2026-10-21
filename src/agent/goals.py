"""Goal eligibility, goal choice and success conditions.

Eligibility is decided here from facts only. A model may suggest a goal elsewhere, but nothing a model says can
make a viewer eligible: the verifier re-runs `eligibility()` independently.

Interpretations of underspecified goals (documented in ARCHITECTURE.md):
- G06 "recently lapsed": a subscription_lapsed event within the last 30 days. A profile saying 'lapsed' with no
  lapse event cannot show *when*, so it is not evidence of 'recent'.
- G07 "unfinished series": a show/micro-drama with a started episode, episodes never started left, last play
  3+ days ago.
- G12 "stopped visiting": no app_open in the 14 days before the trigger AND signed up more than 14 days ago
  (a brand-new viewer also has no earlier open, but has not 'stopped').
- G10 (no success event) and G11 (success event `tv_app_open` never appears in the data) are unmeasurable and
  never chosen.
"""
import copy

from . import clock
from .facts import event_time

UNMEASURABLE = {
    "G10": "success event is null ('engages meaningfully' has no signal)",
    "G11": "success event tv_app_open does not exist in the event stream",
}

# Lifecycle ladder: money goals only with hard evidence, then play -> finish -> pay.
PRIORITY = ["G04", "G06", "G01", "G03", "G07", "G12", "G02", "G05", "G09", "G08"]


class GoalBook:
    """Current goal rules. Rule corrections bump the version and return only the goals they touch."""

    def __init__(self, goals):
        self.goals = copy.deepcopy(goals)
        self.version = 1
        self.history = []

    def correct(self, ctrl):
        g = self.goals[ctrl["goal_id"]]
        g["success"] = {**g["success"], **ctrl["new_success"]}
        self.version += 1
        self.history.append({"version": self.version, "at": ctrl["at"], "goal_id": ctrl["goal_id"],
                             "new_success": ctrl["new_success"]})
        return ctrl["goal_id"]

    def rule_ref(self, goal_id):
        return f"goal_rule:{goal_id}@v{self.version}"


def _v(facts, name):
    return facts[name]["value"]


def _ev(facts, *names):
    return [r for n in names for r in facts[n]["evidence"]]


def _days_since(ts, now):
    return clock.age_s(ts, now) / 86400


def idle_series(facts, now):
    """Most recently played unfinished series, idle 3+ days, in the dialect the app shows (the G07 target).
    A series in another dialect cannot be put on this viewer's page, so it is not a goal we can work toward."""
    d = _v(facts, "dialect")
    return next((s for s in _v(facts, "unfinished_series")
                 if _days_since(s["last_played"], now) >= 3 and s.get("dialect") == d), None)


def eligibility(goal_id, facts, now):
    """Returns (eligible, reason, evidence). Unknown facts mean not eligible: no guessing."""
    if goal_id in UNMEASURABLE:
        return False, "unmeasurable: " + UNMEASURABLE[goal_id], []
    status, signup = _v(facts, "subscription_status"), _v(facts, "signup_at")
    signup_days = _days_since(signup, now) if signup else None

    if goal_id == "G01":
        if signup_days is None:
            return False, "signup date unknown", []
        ok = signup_days <= 7 and not _v(facts, "has_played")
        return ok, f"signed up {signup_days:.1f}d ago, has_played={_v(facts, 'has_played')}", _ev(facts, "signup_at", "has_played")
    if goal_id == "G02":
        ok = not _v(facts, "has_completed")
        return ok, f"has_completed={_v(facts, 'has_completed')}", _ev(facts, "has_completed")
    if goal_id == "G03":
        if signup_days is None:
            return False, "signup date unknown", []
        return signup_days <= 2, f"signed up {signup_days:.1f}d ago", _ev(facts, "signup_at")
    if goal_id in ("G04", "G05", "G06", "G08", "G09"):
        if status is None:
            return False, "subscription status unknown: " + (facts["subscription_status"]["resolution"] or ""), \
                _ev(facts, "subscription_status")
    if goal_id == "G04":
        return status == "trial_active", f"status={status}", _ev(facts, "subscription_status")
    if goal_id == "G05":
        if _v(facts, "ever_paid") is not False:
            return False, f"ever_paid={_v(facts, 'ever_paid')} (must be known false)", _ev(facts, "ever_paid")
        return status == "never", f"status={status}, never paid", _ev(facts, "subscription_status", "ever_paid")
    if goal_id == "G06":
        lapse = facts["subscription_status"]
        recent = status == "lapsed" and lapse["source"] == "billing_event" and _days_since(lapse["observed_at"], now) <= 30
        return recent, f"status={status} from {lapse['source']} (needs lapse event within 30d)", _ev(facts, "subscription_status")
    if goal_id == "G07":
        s = idle_series(facts, now)
        if not s:
            return False, "no unfinished series idle 3+ days in the app's dialect", []
        return True, f"{s['slug']}: {s['episodes_left']} episodes left, idle {_days_since(s['last_played'], now):.0f}d", [s["evidence"]]
    if goal_id == "G08":
        return status == "active", f"status={status}", _ev(facts, "subscription_status")
    if goal_id == "G09":
        days = _v(facts, "play_days_14d")
        return status == "active" and len(days) <= 1, f"status={status}, play days in 14d={len(days)}", \
            _ev(facts, "subscription_status")
    if goal_id == "G12":
        last = _v(facts, "last_app_open")
        if signup_days is None or signup_days <= 14:
            return False, "not an established viewer (signup unknown or within 14d)", []
        gap = _days_since(last, now) if last else None
        ok = gap is None or gap >= 14
        return ok, f"last app_open {'never' if gap is None else f'{gap:.0f}d ago'}", _ev(facts, "last_app_open", "signup_at")
    return False, "unknown goal", []


def choose(book, facts, now, open_goal=None):
    """Pick one goal. Keeps an open goal that is still valid (no churn); returns the audit of what was considered.

    Returns {"goal_id" | None, "keep": bool, "deadline", "reason", "evidence", "considered": [...]}.
    """
    considered = []
    if open_goal and open_goal["deadline"] > now:
        ok, why, ev = eligibility_still_valid(open_goal["goal_id"], facts, now)
        if ok:
            return {"goal_id": open_goal["goal_id"], "keep": True, "deadline": open_goal["deadline"],
                    "target": open_goal.get("target"),
                    "reason": "keeping open goal: " + why, "evidence": ev, "goal_key": open_goal["goal_key"],
                    "considered": []}
        considered.append({"goal_id": open_goal["goal_id"], "eligible": False, "why": "open goal invalidated: " + why})
    for gid in PRIORITY + sorted(UNMEASURABLE):
        ok, why, ev = eligibility(gid, facts, now)
        considered.append({"goal_id": gid, "eligible": ok, "why": why})
        if ok:
            hours = book.goals[gid]["success"]["window_hours"]
            return {"goal_id": gid, "keep": False, "deadline": clock.shift(now, hours=hours),
                    "target": idle_series(facts, now)["slug"] if gid == "G07" else None,
                    "reason": why, "evidence": ev + [book.rule_ref(gid)], "considered": considered,
                    "invalidated": open_goal["goal_key"] if open_goal and open_goal["deadline"] > now else None}
    return {"goal_id": None, "keep": False, "deadline": None, "reason": "no measurable goal is eligible",
            "evidence": [], "considered": considered,
            "invalidated": open_goal["goal_key"] if open_goal and open_goal["deadline"] > now else None}


def eligibility_still_valid(goal_id, facts, now):
    """An open goal stays while the situation it was chosen for still holds. Once-only goals (G01, G02, G05)
    stop being 'eligible' the moment they succeed; that is the judge's job, so here only status changes count."""
    if goal_id in ("G04", "G05", "G06", "G08", "G09"):
        status = _v(facts, "subscription_status")
        expected = {"G04": {"trial_active"}, "G05": {"never"}, "G06": {"lapsed"},
                    "G08": {"active"}, "G09": {"active"}}[goal_id]
        if status not in expected:
            return False, f"subscription status is now {status}", _ev(facts, "subscription_status")
    return True, "situation unchanged", []


# ---- success

def success_event(book, goal, e, pack):
    """Does event `e` satisfy the goal's *current* success rule? `goal` is a goals-table row."""
    rule = book.goals[goal["goal_id"]]["success"]
    if rule["event"] is None or e["event"] != rule["event"]:
        return False
    t = event_time(e)
    if not (goal["started_at"] <= t <= goal["deadline"]):
        return False
    gid = goal["goal_id"]
    if gid in ("G04", "G05"):
        return not e["properties"].get("is_trial")
    if gid == "G03":
        return clock.ist_day(t) > clock.ist_day(goal["started_at"])
    if gid == "G07":
        title = pack.catalogue.get(e["properties"].get("title_id"))
        return bool(title) and title["content_slug"] == goal.get("target")
    if gid == "G08":
        title = pack.catalogue.get(e["properties"].get("title_id"))
        start_day = goal["started_at"][:10]
        return bool(title) and clock.shift(goal["started_at"], days=-30)[:10] <= title["release_date"] <= start_day
    return True  # G01, G02, G06, G12: any event of the right kind; G09 counted by the judge (3 distinct days)
