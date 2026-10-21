"""Turn raw history + profile + trigger into facts with source, time, age and visible conflicts.

Every fact has the same shape:
    {"value", "source", "observed_at", "age_s", "evidence": [...], "conflicts": [...], "resolution"}

Precedence rules (no source wins just because it is biggest or newest):
- subscription: a billing event received after the profile row was written beats the profile;
  disagreeing duplicate profile rows are settled by billing events, otherwise the value is unknown.
- consent: an opt-out event beats the profile; missing or conflicting profile means no consent.
- page dialect: the app setting on the trigger, because the app only asks for the page of its own dialect.
  Profile and campaign-link dialects are recorded as conflicts, never used for content.
- time of an event: phone clocks are wrong by hours, so real time is estimated as
  received_at - (sent_at - occurred_at); the phone's offset cancels out.
"""
from . import clock

BILLING = ("purchase_completed", "subscription_renewed", "subscription_lapsed")


def event_time(e):
    """Best estimate of when an event really happened (server clock)."""
    buffered = clock.age_s(e["occurred_at"], e["sent_at"])
    return clock.shift(e["received_at"], seconds=-max(buffered, 0))


def trigger_age_s(t):
    """How old the app open is when it reaches the agent: time buffered on the phone + delivery delay."""
    return max(clock.age_s(t["occurred_at"], t["sent_at"]), 0) + clock.age_s(t["received_at"], t["deliver_at"])


def _fact(value, source, observed_at, now, evidence=(), conflicts=(), resolution=None):
    return {"value": value, "source": source, "observed_at": observed_at,
            "age_s": round(clock.age_s(observed_at, now)) if observed_at else None,
            "evidence": list(evidence), "conflicts": list(conflicts), "resolution": resolution}


def _profile_ref(r):
    return f"profile:{r['user_id']}@{r['updated_at']}"


def _ev_ref(e):
    return f"event:{e['event_id']}"


def _status_from_event(e):
    if e["event"] == "subscription_lapsed":
        return "lapsed"
    if e["event"] == "purchase_completed" and e["properties"].get("is_trial"):
        return "trial_active"
    return "active"


def subscription(rows, history, now):
    billing = [e for e in history if e["event"] in BILLING]
    last = billing[-1] if billing else None
    statuses = sorted({r["subscription_status"] for r in rows})
    newest_row = max(rows, key=lambda r: r["updated_at"]) if rows else None
    row_refs = [_profile_ref(r) for r in rows]

    if last and (not rows or last["received_at"] > newest_row["updated_at"]):
        conflicts = [f"profile:{s}" for s in statuses if s != _status_from_event(last)]
        return _fact(_status_from_event(last), "billing_event", last["received_at"], now, [_ev_ref(last)],
                     conflicts, "billing event is newer than the profile snapshot" if rows else "no profile row")
    if len(statuses) == 1:
        return _fact(statuses[0], "profile", newest_row["snapshot_at"], now, row_refs)
    if last:
        return _fact(_status_from_event(last), "billing_event", last["received_at"], now,
                     [_ev_ref(last)] + row_refs, [f"profile:{s}" for s in statuses],
                     "duplicate profile rows disagree; latest billing event decides")
    if statuses:
        return _fact(None, "unresolved", newest_row["snapshot_at"], now, row_refs,
                     [f"profile:{s}" for s in statuses], "duplicate profile rows disagree and no billing event")
    return _fact(None, "unknown", None, now, resolution="no profile row and no billing event")


def ever_paid(rows, history, now):
    paid = [e for e in history if e["event"] == "subscription_renewed"
            or (e["event"] == "purchase_completed" and not e["properties"].get("is_trial"))]
    if paid:
        return _fact(True, "billing_event", paid[-1]["received_at"], now, [_ev_ref(paid[-1])])
    statuses = {r["subscription_status"] for r in rows}
    if not rows:
        return _fact(None, "unknown", None, now, resolution="no profile row; no paid event seen")
    if statuses <= {"never", "trial_active"}:
        return _fact(False, "profile", rows[0]["snapshot_at"], now, [_profile_ref(r) for r in rows])
    if statuses & {"active", "lapsed"} and statuses & {"never", "trial_active"}:
        return _fact(None, "unresolved", rows[0]["snapshot_at"], now, [_profile_ref(r) for r in rows],
                     sorted(f"profile:{s}" for s in statuses), "profile rows disagree on whether the viewer paid")
    return _fact(True, "profile", rows[0]["snapshot_at"], now, [_profile_ref(r) for r in rows])


def consent(rows, history, field, channel, now):
    opt_out = [e for e in history if e["event"] == "notification_opt_out" and e["properties"].get("channel") == channel]
    if opt_out:
        return _fact(False, "opt_out_event", opt_out[-1]["received_at"], now, [_ev_ref(opt_out[-1])],
                     resolution="opt-out event overrides the profile")
    values = {r[field] for r in rows}
    if not rows:
        return _fact(False, "default", None, now, resolution="no profile row: consent unknown, treated as no")
    if len(values) > 1:
        return _fact(False, "unresolved", rows[0]["snapshot_at"], now, [_profile_ref(r) for r in rows],
                     [f"profile:{v}" for v in sorted(values)], "profile rows disagree: treated as no")
    return _fact(values.pop() == "true", "profile", rows[0]["snapshot_at"], now, [_profile_ref(r) for r in rows])


def dialect(rows, history, trigger, now):
    used = trigger["app_dialect"]
    conflicts = [f"profile:{d}" for d in sorted({r["preferred_dialect"] for r in rows}) if d != used]
    link = trigger["properties"].get("deeplink")
    if link and link.split("/")[4] != used:
        conflicts.append(f"campaign_link:{link.split('/')[4]}")
    later = [e for e in history if e["received_at"] > trigger["received_at"] and e["app_dialect"] != used]
    if later:
        conflicts.append(f"later_event:{later[-1]['app_dialect']}")
    return _fact(used, "app_setting", trigger["received_at"], now, [f"trigger:{trigger['event_id']}"], conflicts,
                 "app shows only the page of its current dialect; others are hints" if conflicts else None)


def signup(rows, history, now):
    if rows and len({r["signup_at"] for r in rows}) == 1:
        return _fact(rows[0]["signup_at"], "profile", rows[0]["snapshot_at"], now, [_profile_ref(rows[0])])
    ev = [e for e in history if e["event"] == "signup_completed"]
    if ev:
        return _fact(event_time(ev[0]), "signup_event", ev[0]["received_at"], now, [_ev_ref(ev[0])])
    return _fact(None, "unknown", None, now, resolution="no profile row and no signup event")


def viewing(pack, history, now):
    """Plays, completions and unfinished series. Times use event_time (phone offset removed)."""
    plays = [e for e in history if e["event"] == "play_started"]
    completions = [e for e in history if e["event"] == "episode_completed"]
    series = {}
    for e in plays:
        t = pack.catalogue.get(e["properties"].get("title_id"))
        if not t or t["content_type"] == "film":
            continue
        s = series.setdefault(t["content_slug"], {"slug": t["content_slug"], "episodes": set(), "last": None,
                                                  "evidence": None})
        s["episodes"].add(e["properties"].get("episode_id"))
        s["last"], s["evidence"] = event_time(e), _ev_ref(e)
    unfinished = []
    for s in series.values():
        total = {ep["episode_id"] for ep in pack.episodes.get(s["slug"], [])}
        left = len(total - s["episodes"])
        if left:
            unfinished.append({"slug": s["slug"], "episodes_left": left, "last_played": s["last"],
                               "evidence": s["evidence"]})
    unfinished.sort(key=lambda s: s["last_played"], reverse=True)

    cutoff = clock.shift(now, days=-14)
    play_days = sorted({clock.ist_day(event_time(e)) for e in plays if event_time(e) >= cutoff})
    watched = []
    for e in plays:
        t = pack.catalogue.get(e["properties"].get("title_id"))
        if t and t["content_slug"] not in watched:
            watched.append(t["content_slug"])
    return {
        "has_played": _fact(bool(plays), "events", plays[-1]["received_at"] if plays else None, now,
                            [_ev_ref(plays[-1])] if plays else []),
        "has_completed": _fact(bool(completions), "events",
                               completions[-1]["received_at"] if completions else None, now,
                               [_ev_ref(completions[-1])] if completions else []),
        "play_days_14d": _fact(play_days, "events", now, now),
        "unfinished_series": _fact(unfinished, "events", now, now, [s["evidence"] for s in unfinished]),
        "watched_slugs": _fact(watched, "events", now, now),
    }


def last_app_open(history, trigger, now):
    opens = [e for e in history if e["event"] == "app_open" and e["event_id"] != trigger["event_id"]
             and event_time(e) < event_time(trigger)]
    if not opens:
        return _fact(None, "events", None, now, resolution="no earlier app_open seen")
    last = max(opens, key=event_time)
    return _fact(event_time(last), "events", last["received_at"], now, [_ev_ref(last)])


def build_facts(pack, history, trigger, now):
    """Everything the agent knows about one viewer at `now`. `history` must already exclude events received after now."""
    uid = trigger["user_id"]
    rows = pack.profiles.get(uid, [])
    facts = {
        "profile_rows": _fact(len(rows), "profile", rows[0]["snapshot_at"] if rows else None, now,
                              [_profile_ref(r) for r in rows],
                              resolution="duplicate rows kept, conflicts resolved per fact" if len(rows) > 1 else None),
        "trigger_age_s": _fact(round(trigger_age_s(trigger)), "trigger", trigger["received_at"], now,
                               [f"trigger:{trigger['event_id']}"]),
        "signup_at": signup(rows, history, now),
        "subscription_status": subscription(rows, history, now),
        "ever_paid": ever_paid(rows, history, now),
        "push_consent": consent(rows, history, "notifications_opt_in", "push", now),
        "whatsapp_consent": consent(rows, history, "whatsapp_opt_in", "whatsapp", now),
        "dialect": dialect(rows, history, trigger, now),
        "language": _fact(trigger["app_language"], "app_setting", trigger["received_at"], now,
                          [f"trigger:{trigger['event_id']}"]),
        "last_app_open": last_app_open(history, trigger, now),
    }
    facts.update(viewing(pack, history, now))
    return facts


def conflicts(facts):
    """Flat list of every conflict, for the decision record."""
    return [{"fact": k, "used": f["value"] if not isinstance(f["value"], list) else None,
             "source": f["source"], "conflicts": f["conflicts"], "resolution": f["resolution"]}
            for k, f in facts.items() if f["conflicts"]]
