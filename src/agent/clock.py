"""Time helpers. All stored times are UTC ISO strings 'YYYY-MM-DDTHH:MM:SS.mmmZ' so they sort as text."""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def shift(s, **delta):
    return fmt(parse(s) + timedelta(**delta))


def ist_day(s):
    """IST calendar day of a UTC timestamp, e.g. '2026-10-01'. Used for 1-message-per-day and 'later day' rules."""
    return parse(s).astimezone(IST).date().isoformat()


def age_s(then, now):
    return (parse(now) - parse(then)).total_seconds()
