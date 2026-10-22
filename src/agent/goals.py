"""Goal eligibility, choice and success rules.

Eligibility comes from facts only; a model can never make a viewer eligible (the verifier re-runs `eligibility`).
Interpretations of the underspecified goals are in ARCHITECTURE.md.
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

# Goals that apply to one subscription status only. Also what keeps an open goal valid.
REQUIRED_STATUS = {"G04": "trial_active", "G05": "never", "G06": "lapsed", "G08": "active", "G09": "active"}


class GoalBook:
    """Current goal rules. A correction replaces one goal's success rule and bumps that goal's version."""

    def __init__(self, goals):
        self.goals = copy.deepcopy(goals)
        self.version = 1     # bumps on any correction (control log)
        self.versions = {}   # goal_id -> its own rule version
        self.history = []

    def correct(self, ctrl):
        gid = ctrl["goal_id"]
        self.goals[gid]["success"] = {**self.goals[gid]["success"], **ctrl["new_success"]}
        self.version += 1
        self.versions[gid] = self.versions.get(gid, 1) + 1
        self.history.append({"version": self.version, "at": ctrl["at"], "goal_id": gid,
                             "new_success": ctrl["new_success"]})
        return gid

    def rule_ref(self, goal_id):
        return f"goal_rule:{goal_id}@v{self.versions.get(goal_id, 1)}"


def _fact_value(facts, name):
    return facts[name]["value"]


def _ev(facts, *names):
    return [ref for name in names for ref in facts[name]["evidence"]]


def _days_since(timestamp, now):
    return clock.age_s(timestamp, now) / 86400


def _signup_days(facts, now):
    signup = _fact_value(facts, "signup_at")
    return _days_since(signup, now) if signup else None


def idle_series(facts, now):
    """G07 target: the most recent unfinished series, idle 3+ days, in the dialect the app shows."""
    dialect = _fact_value(facts, "dialect")
    return next((series for series in _fact_value(facts, "unfinished_series")
                 if _days_since(series["last_played"], now) >= 3 and series.get("dialect") == dialect), None)


# ---- eligibility: one rule per goal, (facts, now) -> (eligible, reason, evidence)

def _first_play(facts, now):
    days = _signup_days(facts, now)
    if days is None:
        return False, "signup date unknown", []
    played = _fact_value(facts, "has_played")
    return days <= 7 and not played, f"signed up {days:.1f}d ago, has_played={played}", _ev(facts, "signup_at", "has_played")


def _first_full_episode(facts, now):
    done = _fact_value(facts, "has_completed")
    return not done, f"has_completed={done}", _ev(facts, "has_completed")


def _next_day_return(facts, now):
    days = _signup_days(facts, now)
    if days is None:
        return False, "signup date unknown", []
    return days <= 2, f"signed up {days:.1f}d ago", _ev(facts, "signup_at")


def _status_only(facts, now):
    return True, f"status={_fact_value(facts, 'subscription_status')}", _ev(facts, "subscription_status")


def _first_paid(facts, now):
    paid = _fact_value(facts, "ever_paid")
    if paid is not False:
        return False, f"ever_paid={paid} (must be known false)", _ev(facts, "ever_paid")
    return True, "status=never, never paid", _ev(facts, "subscription_status", "ever_paid")


def _recent_lapse(facts, now):
    status_fact = facts["subscription_status"]
    is_recent = status_fact["source"] == "billing_event" and _days_since(status_fact["observed_at"], now) <= 30
    return is_recent, f"lapsed per {status_fact['source']} (needs a lapse event within 30d)", _ev(facts, "subscription_status")


def _resume_series(facts, now):
    series = idle_series(facts, now)
    if not series:
        return False, "no unfinished series idle 3+ days in the app's dialect", []
    idle = _days_since(series["last_played"], now)
    return True, f"{series['slug']}: {series['episodes_left']} episodes left, idle {idle:.0f}d", [series["evidence"]]


def _rare_watcher(facts, now):
    days_played = len(_fact_value(facts, "play_days_14d"))
    return days_played <= 1, f"status=active, play days in 14d={days_played}", _ev(facts, "subscription_status")


def _returning(facts, now):
    days = _signup_days(facts, now)
    if days is None or days <= 14:
        return False, "not an established viewer (signup unknown or within 14d)", []
    last = _fact_value(facts, "last_app_open")
    gap = _days_since(last, now) if last else None
    seen = "never" if gap is None else f"{gap:.0f}d ago"
    return gap is None or gap >= 14, f"last app_open {seen}", _ev(facts, "last_app_open", "signup_at")


RULES = {"G01": _first_play, "G02": _first_full_episode, "G03": _next_day_return, "G04": _status_only,
         "G05": _first_paid, "G06": _recent_lapse, "G07": _resume_series, "G08": _status_only,
         "G09": _rare_watcher, "G12": _returning}


def eligibility(goal_id, facts, now):
    """(eligible, reason, evidence). An unknown fact means not eligible."""
    if goal_id in UNMEASURABLE:
        return False, "unmeasurable: " + UNMEASURABLE[goal_id], []
    if goal_id not in RULES:
        return False, "unknown goal", []
    need, status = REQUIRED_STATUS.get(goal_id), _fact_value(facts, "subscription_status")
    if need and status is None:
        return False, "subscription status unknown: " + (facts["subscription_status"]["resolution"] or ""), \
            _ev(facts, "subscription_status")
    if need and status != need:
        return False, f"status={status}", _ev(facts, "subscription_status")
    return RULES[goal_id](facts, now)


# ---- choice

def _pick(book, gid, facts, now, why, evidence):
    return {"goal_id": gid, "keep": False, "reason": why, "evidence": evidence + [book.rule_ref(gid)],
            "deadline": clock.shift(now, hours=book.goals[gid]["success"]["window_hours"]),
            "target": idle_series(facts, now)["slug"] if gid == "G07" else None}


def choose(book, facts, now, open_goal=None):
    """Keep a still-valid open goal; otherwise the first eligible goal on the ladder, or none.
    `considered` lists every goal checked and why, for the decision record."""
    live = open_goal if open_goal and open_goal["deadline"] > now else None
    considered = []
    if live:
        need, status = REQUIRED_STATUS.get(live["goal_id"]), _fact_value(facts, "subscription_status")
        if need is None or status == need:
            return {"goal_id": live["goal_id"], "keep": True, "goal_key": live["goal_key"], "deadline": live["deadline"],
                    "target": live.get("target"), "reason": "keeping open goal: situation unchanged",
                    "evidence": [], "considered": []}
        considered.append({"goal_id": live["goal_id"], "eligible": False,
                           "why": f"open goal invalidated: subscription status is now {status}"})

    base = {"goal_id": None, "keep": False, "deadline": None, "target": None, "evidence": [],
            "reason": "no measurable goal is eligible", "considered": considered,
            "invalidated": live["goal_key"] if live else None}
    for gid in PRIORITY + sorted(UNMEASURABLE):
        eligible, why, evidence = eligibility(gid, facts, now)
        considered.append({"goal_id": gid, "eligible": eligible, "why": why})
        if eligible:
            return {**base, **_pick(book, gid, facts, now, why, evidence)}
    return base


def choose_specific(book, facts, now, goal_id, base):
    """Switch to another goal (the model's preference). Refused unless the rules find it eligible."""
    eligible, why, evidence = eligibility(goal_id, facts, now)
    if not eligible:
        return base
    return {**base, **_pick(book, goal_id, facts, now, why + " (model preference among rule-eligible goals)", evidence)}


# ---- success: event kind and window from the current rule, plus a per-goal condition

def _title(pack, event):
    return pack.catalogue.get(event["properties"].get("title_id"))


def _later_ist_day(goal, event, pack):
    return clock.ist_day(event_time(event)) > clock.ist_day(goal["started_at"])


def _not_trial(goal, event, pack):
    return not event["properties"].get("is_trial")


def _same_series(goal, event, pack):
    title = _title(pack, event)
    return bool(title) and goal.get("target") is not None and title["content_slug"] == goal["target"]


def _recent_release(goal, event, pack):
    title = _title(pack, event)
    since = clock.shift(goal["started_at"], days=-30)[:10]
    return bool(title) and since <= title["release_date"] <= goal["started_at"][:10]


SUCCESS_CONDITION = {"G03": _later_ist_day, "G04": _not_trial, "G05": _not_trial,
                     "G07": _same_series, "G08": _recent_release}


def success_event(book, goal, event, pack):
    """Does `e` meet the goal's current success rule inside its window? G09 (3 distinct days) is counted by the judge."""
    rule = book.goals[goal["goal_id"]]["success"]
    if rule["event"] is None or event["event"] != rule["event"]:
        return False
    if not goal["started_at"] <= event_time(event) <= goal["deadline"]:
        return False
    condition = SUCCESS_CONDITION.get(goal["goal_id"])
    return condition is None or condition(goal, event, pack)
