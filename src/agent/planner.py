"""Decide the intervention for one viewer: a home page, at most one follow-up message, or nothing.

Content is picked from catalogue fields only (dialect, availability, genres, premium, release date) plus popularity
from past plays. Descriptions are never read.
"""
from collections import Counter

from . import clock

RETURN_GOALS = {"G01", "G03", "G07", "G12"}       # need the viewer to come back: worth one follow-up
FREE_FIRST = {"G01", "G02", "G03", "G07", "G12"}  # non-paying stages: don't lead into a paywall
PREMIUM_FIRST = {"G04", "G05", "G06"}             # purchase goals: show what paying unlocks
MAX_TRIGGER_AGE_S = 600                           # older: the viewer has left that session
FOLLOW_UP_HOUR_IST = 19                           # evening, outside quiet hours
ROW_LEN, MIN_ROW = 10, 3


class Catalog:
    """Catalogue index with live availability (withdrawals arrive during the replay)."""

    def __init__(self, pack, play_counts):
        self.pack = pack
        self.titles = pack.catalogue
        self.withdrawn = {}            # title_id -> withdrawn at
        self.popularity = play_counts  # content_slug -> plays in history
        self.slug_genres = {title["content_slug"]: title["genres"].split("|") for title in pack.catalogue.values()}

    def withdraw(self, title_id, withdrawn_at):
        self.withdrawn[title_id] = withdrawn_at

    def available(self, title_id, now):
        title = self.titles.get(title_id)
        if not title:
            return False, "unknown title"
        if title["available_from"] > now:
            return False, "not yet available"
        if title["available_until"] and now >= title["available_until"]:
            return False, "availability window ended"
        if title_id in self.withdrawn and now >= self.withdrawn[title_id]:
            return False, "withdrawn"
        return True, "available"

    def pool(self, dialect, language, now):
        """One title per content_slug in this dialect, available at `at`, app-language variant preferred,
        most played first."""
        best = {}
        for tid, title in self.titles.items():
            if title["dialect"] != dialect or not self.available(tid, now)[0]:
                continue
            cur = best.get(title["content_slug"])
            if cur is None or (title["language"] == language and cur["language"] != language):
                best[title["content_slug"]] = title
        return sorted(best.values(), key=lambda title: (-self.popularity.get(title["content_slug"], 0), title["title_id"]))

    def first_episode_minutes(self, slug):
        eps = self.pack.episodes.get(slug)
        return eps[0]["minutes"] if eps else 999


def play_counts(pack, play_events):
    """Popularity per content_slug from past play_started events (language variants counted together)."""
    return Counter(title["content_slug"] for event in play_events
                   if (title := pack.catalogue.get(event["properties"].get("title_id"))))


# ---- home page

def _goal_order(cat, gid):
    """Sort key that reorders the popularity-ranked pool for the goal's stage (the sort is stable)."""
    def key(title):
        premium = title["is_premium"] == "true"
        long_first_episode = gid == "G02" and cat.first_episode_minutes(title["content_slug"]) > 30
        stage = premium if gid in FREE_FIRST else (not premium if gid in PREMIUM_FIRST else False)
        return long_first_episode, stage
    return key


def build_page(cat, facts, goal, now):
    dialect, gid = facts["dialect"]["value"], goal["goal_id"]
    pool = sorted(cat.pool(dialect, facts["language"]["value"], now), key=_goal_order(cat, gid))
    by_slug = {title["content_slug"]: title for title in pool}
    watched_list = facts["watched_slugs"]["value"]
    watched = set(watched_list)
    fresh = [title for title in pool if title["content_slug"] not in watched]

    hero = by_slug.get(goal.get("target")) or (fresh[0] if fresh else None)
    used = {hero["content_slug"]} if hero else set()

    # ordered list, not the set: set order changes per process and would flip genre ties
    genres = Counter(genre for slug in watched_list for genre in cat.slug_genres.get(slug, []))
    top_genre = genres.most_common(1)[0][0] if genres else None
    since, today = clock.shift(now, days=-30)[:10], now[:10]
    new = [title for title in fresh if since <= title["release_date"] <= today]
    candidates = [
        ("next_episode", [by_slug[series["slug"]] for series in facts["unfinished_series"]["value"] if series["slug"] in by_slug]),
        ("new_releases", new if gid == "G08" else []),
        ("genre", [title for title in fresh if top_genre in title["genres"].split("|")] if top_genre else []),
        ("popular_in_dialect", fresh),
        ("new_releases", new if gid != "G08" else []),
    ]
    rows = []
    for row_type, titles in candidates:
        picked = [title for title in titles if title["content_slug"] not in used][:ROW_LEN]   # pool is one title per slug
        if len(picked) >= MIN_ROW:   # the platform drops shorter rows; don't publish them
            rows.append({"row_type": row_type, "title_ids": [title["title_id"] for title in picked]})
            used.update(title["content_slug"] for title in picked)
    return {"dialect": dialect, "hero_title_id": hero["title_id"] if hero else None, "rows": rows[:6]}


# ---- follow-up message

def _send_at(now):
    day = clock.ist_day(clock.shift(now, days=1))
    return day, clock.fmt(clock.parse(f"{day}T{FOLLOW_UP_HOUR_IST:02d}:00:00+05:30"))


def _whatsapp(title, send_at):
    return {"type": "whatsapp", "send_at": send_at, "template_id": "wa_recommend_title_v1", "title_id": title["title_id"],
            "variables": {"title_name": title["title"], "dialect_name": title["dialect"]}}


def _push(title, send_at, gid):
    body = (f"{title['title']}: aapka agla episode aapka intezaar kar raha hai." if gid == "G07"
            else f"Aaj shaam dekhiye {title['title']}.")
    return {"type": "push", "send_at": send_at, "title_id": title["title_id"], "deeplink": f"ott://title/{title['title_id']}",
            "text": {"title": title["title"][:40], "body": body[:120]}}


def follow_up(cat, facts, goal, page, now, messages_by_day, avoid=()):
    """At most one evening message, only for goals that need the viewer to return. Returns (message | None, reason).
    `avoid`: channels learned to be undeliverable for this viewer."""
    if goal["goal_id"] not in RETURN_GOALS:
        return None, f"no message: {goal['goal_id']} is served by the home page alone"
    can_whatsapp = facts["whatsapp_consent"]["value"] and "whatsapp" not in avoid
    can_push = facts["push_consent"]["value"] and "push" not in avoid
    if not (can_whatsapp or can_push):
        return None, "no message: no consented channel that is known to deliver"
    day, send_at = _send_at(now)
    if messages_by_day.get(day):
        return None, f"no message: already one planned for {day} (1 per day)"
    tid = page["hero_title_id"]
    if not tid or not cat.available(tid, send_at)[0]:
        return None, "no message: no title that will still be available at send time"
    title = cat.titles[tid]
    if can_whatsapp:
        return _whatsapp(title, send_at), "WhatsApp: consented, approved template, no device-token risk"
    return _push(title, send_at, goal["goal_id"]), "push: consented, no WhatsApp consent"


# ---- plan

def _nothing(reason):
    return {"action": "NO_ACTION", "page": None, "message": None, "reasons": [reason], "assumptions": []}


def _abstain_reason(facts, goal, arm):
    """First reason to do nothing, or None. Order matters: holdout before anything else."""
    age = facts["trigger_age_s"]["value"]
    if arm != "agent":
        return f"viewer is in the {arm or 'unknown'} arm: no action of any kind"
    if not goal["goal_id"]:
        return goal["reason"]
    if age > MAX_TRIGGER_AGE_S:
        return (f"trigger is {age}s old (buffered offline): the viewer has left that session; "
                "history updated, nothing published")
    if facts["dialect"]["value"] is None:
        return facts["dialect"]["resolution"]
    return None


def plan(cat, facts, goal, now, arm, messages_by_day, allow_messages=True, avoid=()):
    """Returns {"action": HOME_PAGE | NO_ACTION, "page", "message", "reasons", "assumptions"}."""
    reason = _abstain_reason(facts, goal, arm)
    if reason:
        return _nothing(reason)
    page = build_page(cat, facts, goal, now)
    if not page["rows"] and not page["hero_title_id"]:
        return _nothing(f"not enough available titles in {page['dialect']} for a valid row")

    assumptions = [f"app will request the {page['dialect']} page (dialect at trigger time)"]
    if facts["watched_slugs"]["value"]:
        assumptions.append("titles already started are excluded except in next_episode/hero")
    if allow_messages:
        message, why = follow_up(cat, facts, goal, page, now, messages_by_day, avoid)
    else:
        message, why = None, "no message: degraded mode, irreversible actions suppressed"
    return {"action": "HOME_PAGE", "page": page, "message": message, "reasons": [why], "assumptions": assumptions}


def confidence(facts, goal, plan_):
    """Confidence in the decision's evidence (not a success probability), with what lowered it."""
    status, conflicts = facts["subscription_status"], facts["dialect"]["conflicts"]
    penalties = []
    if conflicts:
        penalties.append((0.15, "dialect sources disagree (" + ", ".join(conflicts) + ")"))
    if status["value"] is None:
        penalties.append((0.15, "subscription status unknown"))
    elif status["source"] == "profile":
        penalties.append((0.05, "status from day-old snapshot"))
    if plan_["page"] and len(plan_["page"]["rows"]) < 2:
        penalties.append((0.1, "thin page (fewer than 2 rows)"))
    if goal["goal_id"] in PREMIUM_FIRST:
        penalties.append((0.1, "purchase goals have low base rates"))
    score = 0.8 - sum(cut for cut, _ in penalties)
    return round(max(score, 0.1), 2), [why for _, why in penalties] or ["fresh, consistent evidence"]
