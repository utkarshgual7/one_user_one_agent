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


def event_time(event):
    """Best estimate of when an event really happened (server clock)."""
    buffered = clock.age_s(event["occurred_at"], event["sent_at"])
    return clock.shift(event["received_at"], seconds=-max(buffered, 0))


def trigger_age_s(trigger):
    """How old the app open is when it reaches the agent: time buffered on the phone + delivery delay."""
    return max(clock.age_s(trigger["occurred_at"], trigger["sent_at"]), 0) + clock.age_s(trigger["received_at"], trigger["deliver_at"])


def _fact(value, source, observed_at, now, evidence=(), conflicts=(), resolution=None):
    return {"value": value, "source": source, "observed_at": observed_at,
            "age_s": round(clock.age_s(observed_at, now)) if observed_at else None,
            "evidence": list(evidence), "conflicts": list(conflicts), "resolution": resolution}


def _profile_ref(row):
    return f"profile:{row['user_id']}@{row['updated_at']}"


def _ev_ref(event):
    return f"event:{event['event_id']}"


def _status_from_event(event):
    if event["event"] == "subscription_lapsed":
        return "lapsed"
    if event["event"] == "purchase_completed" and event["properties"].get("is_trial"):
        return "trial_active"
    return "active"


def subscription(rows, history, now):
    billing = [event for event in history if event["event"] in BILLING]
    last = billing[-1] if billing else None
    statuses = sorted({row["subscription_status"] for row in rows})
    newest_row = max(rows, key=lambda row: row["updated_at"]) if rows else None
    row_refs = [_profile_ref(row) for row in rows]

    if last and (not rows or last["received_at"] > newest_row["updated_at"]):
        conflicts = [f"profile:{status}" for status in statuses if status != _status_from_event(last)]
        return _fact(_status_from_event(last), "billing_event", last["received_at"], now, [_ev_ref(last)],
                     conflicts, "billing event is newer than the profile snapshot" if rows else "no profile row")
    if len(statuses) == 1:
        return _fact(statuses[0], "profile", newest_row["snapshot_at"], now, row_refs)
    if last:
        return _fact(_status_from_event(last), "billing_event", last["received_at"], now,
                     [_ev_ref(last)] + row_refs, [f"profile:{status}" for status in statuses],
                     "duplicate profile rows disagree; latest billing event decides")
    if statuses:
        return _fact(None, "unresolved", newest_row["snapshot_at"], now, row_refs,
                     [f"profile:{status}" for status in statuses], "duplicate profile rows disagree and no billing event")
    return _fact(None, "unknown", None, now, resolution="no profile row and no billing event")


def ever_paid(rows, history, now):
    paid = [event for event in history if event["event"] == "subscription_renewed"
            or (event["event"] == "purchase_completed" and not event["properties"].get("is_trial"))]
    if paid:
        return _fact(True, "billing_event", paid[-1]["received_at"], now, [_ev_ref(paid[-1])])
    statuses = {row["subscription_status"] for row in rows}
    if not rows:
        return _fact(None, "unknown", None, now, resolution="no profile row; no paid event seen")
    if statuses <= {"never", "trial_active"}:
        return _fact(False, "profile", rows[0]["snapshot_at"], now, [_profile_ref(row) for row in rows])
    if statuses & {"active", "lapsed"} and statuses & {"never", "trial_active"}:
        return _fact(None, "unresolved", rows[0]["snapshot_at"], now, [_profile_ref(row) for row in rows],
                     sorted(f"profile:{status}" for status in statuses), "profile rows disagree on whether the viewer paid")
    return _fact(True, "profile", rows[0]["snapshot_at"], now, [_profile_ref(row) for row in rows])


def consent(rows, history, field, channel, now):
    opt_out = [event for event in history if event["event"] == "notification_opt_out" and event["properties"].get("channel") == channel]
    if opt_out:
        return _fact(False, "opt_out_event", opt_out[-1]["received_at"], now, [_ev_ref(opt_out[-1])],
                     resolution="opt-out event overrides the profile")
    values = {row[field] for row in rows}
    if not rows:
        return _fact(False, "default", None, now, resolution="no profile row: consent unknown, treated as no")
    if len(values) > 1:
        return _fact(False, "unresolved", rows[0]["snapshot_at"], now, [_profile_ref(row) for row in rows],
                     [f"profile:{value}" for value in sorted(values)], "profile rows disagree: treated as no")
    return _fact(values.pop() == "true", "profile", rows[0]["snapshot_at"], now, [_profile_ref(row) for row in rows])


def dialect(rows, history, trigger, now, valid):
    raw = trigger["app_dialect"]
    if raw not in valid:
        hints = sorted({f"profile:{row['preferred_dialect']}" for row in rows})
        return _fact(None, "app_setting_invalid", trigger["received_at"], now, [f"trigger:{trigger['event_id']}"],
                     [f"app_setting:{raw}"] + hints,
                     f"app reports dialect {raw!r}, which has no content: it will not show a personalised page")
    used = raw
    conflicts = [f"profile:{code}" for code in sorted({row["preferred_dialect"] for row in rows}) if code != used]
    link = trigger["properties"].get("deeplink")
    if link and link.split("/")[4] != used:
        conflicts.append(f"campaign_link:{link.split('/')[4]}")
    later = [event for event in history if event["received_at"] > trigger["received_at"] and event["app_dialect"] != used]
    if later:
        conflicts.append(f"later_event:{later[-1]['app_dialect']}")
    return _fact(used, "app_setting", trigger["received_at"], now, [f"trigger:{trigger['event_id']}"], conflicts,
                 "app shows only the page of its current dialect; others are hints" if conflicts else None)


def signup(rows, history, now):
    if rows and len({row["signup_at"] for row in rows}) == 1:
        return _fact(rows[0]["signup_at"], "profile", rows[0]["snapshot_at"], now, [_profile_ref(rows[0])])
    signup_events = [event for event in history if event["event"] == "signup_completed"]
    if signup_events:
        return _fact(event_time(signup_events[0]), "signup_event", signup_events[0]["received_at"], now, [_ev_ref(signup_events[0])])
    return _fact(None, "unknown", None, now, resolution="no profile row and no signup event")


def viewing(pack, history, now):
    """Plays, completions and unfinished series. Times use event_time (phone offset removed)."""
    plays = [event for event in history if event["event"] == "play_started"]
    completions = [event for event in history if event["event"] == "episode_completed"]
    series = {}
    for event in plays:
        title = pack.catalogue.get(event["properties"].get("title_id"))
        if not title or title["content_type"] == "film":
            continue
        entry = series.setdefault(title["content_slug"], {"slug": title["content_slug"], "dialect": title["dialect"],
                                                  "episodes": set(), "last": None, "evidence": None})
        entry["episodes"].add(event["properties"].get("episode_id"))
        entry["last"], entry["evidence"] = event_time(event), _ev_ref(event)
    unfinished = []
    for entry in series.values():
        total = {episode["episode_id"] for episode in pack.episodes.get(entry["slug"], [])}
        left = len(total - entry["episodes"])
        if left:
            unfinished.append({"slug": entry["slug"], "dialect": entry["dialect"], "episodes_left": left, "last_played": entry["last"],
                               "evidence": entry["evidence"]})
    unfinished.sort(key=lambda entry: entry["last_played"], reverse=True)

    cutoff = clock.shift(now, days=-14)
    play_days = sorted({clock.ist_day(event_time(event)) for event in plays if event_time(event) >= cutoff})
    watched = []
    for event in plays:
        title = pack.catalogue.get(event["properties"].get("title_id"))
        if title and title["content_slug"] not in watched:
            watched.append(title["content_slug"])
    return {
        "has_played": _fact(bool(plays), "events", plays[-1]["received_at"] if plays else None, now,
                            [_ev_ref(plays[-1])] if plays else []),
        "has_completed": _fact(bool(completions), "events",
                               completions[-1]["received_at"] if completions else None, now,
                               [_ev_ref(completions[-1])] if completions else []),
        "play_days_14d": _fact(play_days, "events", now, now),
        "unfinished_series": _fact(unfinished, "events", now, now, [entry["evidence"] for entry in unfinished]),
        "watched_slugs": _fact(watched, "events", now, now),
    }


def last_app_open(history, trigger, now):
    opens = [event for event in history if event["event"] == "app_open" and event["event_id"] != trigger["event_id"]
             and event_time(event) < event_time(trigger)]
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
                              [_profile_ref(row) for row in rows],
                              resolution="duplicate rows kept, conflicts resolved per fact" if len(rows) > 1 else None),
        "trigger_age_s": _fact(round(trigger_age_s(trigger)), "trigger", trigger["received_at"], now,
                               [f"trigger:{trigger['event_id']}"]),
        "signup_at": signup(rows, history, now),
        "subscription_status": subscription(rows, history, now),
        "ever_paid": ever_paid(rows, history, now),
        "push_consent": consent(rows, history, "notifications_opt_in", "push", now),
        "whatsapp_consent": consent(rows, history, "whatsapp_opt_in", "whatsapp", now),
        "dialect": dialect(rows, history, trigger, now, pack.dialects),
        "language": _fact(trigger["app_language"], "app_setting", trigger["received_at"], now,
                          [f"trigger:{trigger['event_id']}"]),
        "last_app_open": last_app_open(history, trigger, now),
    }
    facts.update(viewing(pack, history, now))
    return facts


def conflicts(facts):
    """Flat list of every conflict, for the decision record."""
    return [{"fact": name, "used": fact["value"] if not isinstance(fact["value"], list) else None,
             "source": fact["source"], "conflicts": fact["conflicts"], "resolution": fact["resolution"]}
            for name, fact in facts.items() if fact["conflicts"]]
