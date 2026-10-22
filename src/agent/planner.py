"""Decide the intervention for one viewer: home page, one follow-up message, or nothing.

Only catalogue fields are used to pick content (dialect, availability, genres, premium flag, release date,
popularity from past plays). Titles and descriptions are never read as instructions: descriptions are not used
at all.
"""
from collections import Counter

from . import clock

RETURN_GOALS = {"G01", "G03", "G07", "G12"}   # goals that need the viewer to come back: worth one follow-up
FREE_FIRST = {"G01", "G02", "G03", "G07", "G12"}  # non-paying stages: avoid sending them into a paywall
PREMIUM_FIRST = {"G04", "G05", "G06"}          # purchase goals: show what paying unlocks
MAX_TRIGGER_AGE_S = 600   # older than this, the viewer is no longer in that session
FOLLOW_UP_HOUR_IST = 19   # evening, well outside quiet hours (22:00-08:00)
ROW_LEN = 10


class Catalog:
    """Catalogue index with live availability (withdrawals arrive during the replay)."""

    def __init__(self, pack, play_counts):
        self.pack = pack
        self.titles = pack.catalogue
        self.withdrawn = {}                      # title_id -> withdrawn at
        self.popularity = play_counts            # content_slug -> plays in history
        self.slug_genres = {t["content_slug"]: t["genres"].split("|") for t in pack.catalogue.values()}

    def withdraw(self, title_id, at):
        self.withdrawn[title_id] = at

    def available(self, title_id, at):
        t = self.titles.get(title_id)
        if not t:
            return False, "unknown title"
        if t["available_from"] > at:
            return False, "not yet available"
        if t["available_until"] and at >= t["available_until"]:
            return False, "availability window ended"
        if title_id in self.withdrawn and at >= self.withdrawn[title_id]:
            return False, "withdrawn"
        return True, "available"

    def pool(self, dialect, language, at):
        """One title per content_slug in this dialect, available at `at`; the app-language variant preferred."""
        best = {}
        for tid, t in self.titles.items():
            if t["dialect"] != dialect or not self.available(tid, at)[0]:
                continue
            cur = best.get(t["content_slug"])
            if cur is None or (t["language"] == language and cur["language"] != language):
                best[t["content_slug"]] = t
        return sorted(best.values(), key=lambda t: (-self.popularity.get(t["content_slug"], 0), t["title_id"]))

    def first_episode_minutes(self, slug):
        eps = self.pack.episodes.get(slug)
        return eps[0]["minutes"] if eps else 999


def play_counts(pack, play_events):
    """Popularity per content_slug from past play_started events (language variants counted together)."""
    c = Counter()
    for e in play_events:
        t = pack.catalogue.get(e["properties"].get("title_id"))
        if t:
            c[t["content_slug"]] += 1
    return c


def build_page(cat, facts, goal, at):
    dialect, language = facts["dialect"]["value"], facts["language"]["value"]
    pool = cat.pool(dialect, language, at)
    gid = goal["goal_id"]
    watched = set(facts["watched_slugs"]["value"])
    if gid in FREE_FIRST:
        pool.sort(key=lambda t: t["is_premium"] == "true")      # stable: popularity order kept within groups
    elif gid in PREMIUM_FIRST:
        pool.sort(key=lambda t: t["is_premium"] != "true")
    if gid == "G02":                                           # first full episode: short first episodes first
        pool.sort(key=lambda t: cat.first_episode_minutes(t["content_slug"]) > 30)

    used, rows, hero = set(), [], None
    by_slug = {t["content_slug"]: t for t in pool}
    resume = [s["slug"] for s in facts["unfinished_series"]["value"] if s["slug"] in by_slug]
    if goal.get("target") in by_slug:
        hero = by_slug[goal["target"]]["title_id"]
        used.add(goal["target"])

    def add_row(row_type, titles):
        ids = []
        for t in titles:
            if t["content_slug"] not in used and len(ids) < ROW_LEN:
                ids.append(t["title_id"])
                used.add(t["content_slug"])
        if len(ids) >= 3:            # platform drops shorter rows; don't publish them
            rows.append({"row_type": row_type, "title_ids": ids})
        else:
            for tid in ids:
                used.discard(cat.titles[tid]["content_slug"])

    fresh = [t for t in pool if t["content_slug"] not in watched]
    if hero is None and fresh:   # reserve the hero first so no row repeats it
        hero = fresh[0]["title_id"]
        used.add(fresh[0]["content_slug"])
    add_row("next_episode", [by_slug[s] for s in resume])
    genres = Counter(g for s in watched for g in cat.slug_genres.get(s, []))
    new = [t for t in fresh if clock.shift(at, days=-30)[:10] <= t["release_date"] <= at[:10]]
    if gid == "G08":
        add_row("new_releases", new)
    if genres:
        top = genres.most_common(1)[0][0]
        add_row("genre", [t for t in fresh if top in t["genres"].split("|")])
    add_row("popular_in_dialect", fresh)
    if gid != "G08":
        add_row("new_releases", new)
    return {"dialect": dialect, "hero_title_id": hero, "rows": rows[:6]}


def follow_up(cat, facts, goal, page, at, messages_by_day, avoid=()):
    """At most one evening message for goals that need the viewer to return. Returns (message | None, reason)."""
    if goal["goal_id"] not in RETURN_GOALS:
        return None, f"no message: {goal['goal_id']} is served by the home page alone"
    wa = facts["whatsapp_consent"]["value"] and "whatsapp" not in avoid
    push = facts["push_consent"]["value"] and "push" not in avoid   # avoid: channels learned undeliverable
    if not (wa or push):
        return None, "no message: no consented channel that is known to deliver"
    send_day = clock.ist_day(clock.shift(at, days=1))
    send_at = clock.fmt(clock.parse(f"{send_day}T{FOLLOW_UP_HOUR_IST:02d}:00:00+05:30"))
    if messages_by_day.get(send_day):
        return None, f"no message: already one planned for {send_day} (1 per day)"
    title_id = page["hero_title_id"]
    if not title_id or not cat.available(title_id, send_at)[0]:
        return None, "no message: no title that will still be available at send time"
    t = cat.titles[title_id]
    if wa:
        return {"type": "whatsapp", "send_at": send_at, "template_id": "wa_recommend_title_v1",
                "variables": {"title_name": t["title"], "dialect_name": t["dialect"]}, "title_id": title_id}, \
            "WhatsApp: consented, approved template, no device-token risk"
    body = (f"{t['title']}: aapka agla episode aapka intezaar kar raha hai." if goal["goal_id"] == "G07"
            else f"Aaj shaam dekhiye {t['title']}.")
    return {"type": "push", "send_at": send_at, "title_id": title_id, "deeplink": f"ott://title/{title_id}",
            "text": {"title": t["title"][:40], "body": body[:120]}}, "push: consented, no WhatsApp consent"


def plan(cat, facts, goal, now, arm, messages_by_day, allow_messages=True, avoid=()):
    """Returns {"action": HOME_PAGE | NO_ACTION, "page", "message", "reasons", "assumptions"}."""
    reasons, assumptions = [], []
    if arm != "agent":
        return {"action": "NO_ACTION", "page": None, "message": None,
                "reasons": [f"viewer is in the {arm or 'unknown'} arm: no action of any kind"], "assumptions": []}
    if not goal["goal_id"]:
        return {"action": "NO_ACTION", "page": None, "message": None, "reasons": [goal["reason"]], "assumptions": []}
    age = facts["trigger_age_s"]["value"]
    if age > MAX_TRIGGER_AGE_S:
        return {"action": "NO_ACTION", "page": None, "message": None,
                "reasons": [f"trigger is {age}s old (buffered offline): the viewer has left that session; "
                            "history updated, nothing published"], "assumptions": []}

    if facts["dialect"]["value"] is None:
        return {"action": "NO_ACTION", "page": None, "message": None,
                "reasons": [facts["dialect"]["resolution"]], "assumptions": []}

    page = build_page(cat, facts, goal, now)
    if not page["rows"] and not page["hero_title_id"]:
        return {"action": "NO_ACTION", "page": None, "message": None,
                "reasons": [f"not enough available titles in {page['dialect']} for a valid row"], "assumptions": []}
    assumptions.append(f"app will request the {page['dialect']} page (dialect at trigger time)")
    if facts["watched_slugs"]["value"]:
        assumptions.append("titles already started are excluded except in next_episode/hero")
    msg, why = (follow_up(cat, facts, goal, page, now, messages_by_day, avoid) if allow_messages
                else (None, "no message: degraded mode, irreversible actions suppressed"))
    reasons.append(why)
    return {"action": "HOME_PAGE", "page": page, "message": msg, "reasons": reasons, "assumptions": assumptions}


def confidence(facts, goal, plan_):
    """Confidence in the decision (not a success probability), with the reasons that moved it."""
    score, why = 0.8, []
    if facts["dialect"]["conflicts"]:
        score -= 0.15
        why.append("dialect sources disagree (" + ", ".join(facts["dialect"]["conflicts"]) + ")")
    if facts["subscription_status"]["value"] is None:
        score -= 0.15
        why.append("subscription status unknown")
    elif facts["subscription_status"]["source"] == "profile":
        score -= 0.05
        why.append("status from day-old snapshot")
    if plan_["page"] and len(plan_["page"]["rows"]) < 2:
        score -= 0.1
        why.append("thin page (fewer than 2 rows)")
    if goal["goal_id"] in ("G04", "G05", "G06"):
        score -= 0.1
        why.append("purchase goals have low base rates")
    return round(max(score, 0.1), 2), why or ["fresh, consistent evidence"]
