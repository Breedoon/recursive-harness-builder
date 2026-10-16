"""Recognize provider credit-limit (quota) failures and when they reset (bead vault-ns5).

A scheduled fire that lands during a credit-limit outage ends in a ~5 s error turn whose
only text is the provider's limit line, e.g. ``You've hit your limit · resets 11am (UTC)``.
The scheduler uses :func:`parse_credit_limit` on the same error text that becomes the
schedule's ``last_error`` to decide that the fire should not count against ``max_runs``
and when to re-arm it. Pure functions; no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Seconds added after a parsed reset time so the re-armed fire lands after the pool refills.
RESET_SLACK_SECONDS = 60
# Bounded backoff when the error is a recognized limit but carries no usable reset time.
BACKOFF_BASE_SECONDS = 300
BACKOFF_MAX_SECONDS = 3600
# A parsed reset further away than this is treated as unparsed (weekly pools reset within 7 days).
MAX_RESET_HORIZON_SECONDS = 8 * 24 * 3600

_LIMIT_PATTERNS = (
    re.compile(r"\bhit your (?:\w+ )?limit\b", re.IGNORECASE),
    re.compile(r"\busage limit (?:reached|exceeded)\b", re.IGNORECASE),
    re.compile(r"\b(?:5-hour|weekly|session|opus|sonnet|daily) limit reached\b", re.IGNORECASE),
    re.compile(r"\bout of (?:extra )?usage\b", re.IGNORECASE),
    re.compile(r"\busage_limit_reached\b", re.IGNORECASE),
    re.compile(r"\brate limit reached\b", re.IGNORECASE),
    re.compile(r"\"type\"\s*:\s*\"rate_limit_error\"", re.IGNORECASE),
)

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}

# "resets 4:10am (UTC)", "resets 11am (UTC)", "resets Oct 17, 3pm (Europe/Warsaw)",
# "resets Oct 17 at 3:30pm"
_RESET_CLOCK = re.compile(
    r"\bresets?\s+(?:on\s+)?"
    r"(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
# "Claude AI usage limit reached|1760000000"
_RESET_EPOCH = re.compile(r"limit reached\|(?P<epoch>\d{10})\b", re.IGNORECASE)
# "resets_in_seconds": 3600
_RESET_IN_SECONDS = re.compile(r"resets?_in_seconds\"?\s*[:=]\s*(?P<seconds>\d+)", re.IGNORECASE)
# "resets in 2h 5m", "try again in 45m"
_RESET_IN_DURATION = re.compile(
    r"\b(?:resets?|try again)\s+in\s+(?P<duration>(?:\d+\s*(?:d|h|m|s)[a-z]*[\s,]*)+)",
    re.IGNORECASE,
)
_DURATION_PART = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


@dataclass(frozen=True)
class CreditLimit:
    """A recognized credit-limit failure; ``reset_at`` is epoch seconds or None if unparsed."""

    text: str
    reset_at: float | None


def is_credit_limit_error(text: str | None) -> bool:
    return bool(text) and any(pattern.search(text) for pattern in _LIMIT_PATTERNS)


def _zone(name: str | None) -> timezone | ZoneInfo:
    raw = (name or "").strip()
    if not raw or raw.upper() in {"UTC", "GMT", "Z"}:
        return timezone.utc
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _parse_clock_reset(match: re.Match[str], now: float) -> float | None:
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = (match.group("ampm") or "").lower().replace(".", "")
    if ampm and match.group("minute") is None and not 1 <= hour <= 12:
        return None
    if not ampm and match.group("minute") is None:
        # A bare number ("resets 3") is too ambiguous to trust.
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    zone = _zone(match.group("tz"))
    local_now = datetime.fromtimestamp(now, tz=zone)
    month_name = (match.group("mon") or "").lower()
    if month_name:
        month = _MONTHS.get(month_name)
        if month is None:
            return None
        day = int(match.group("day"))
        for year in (local_now.year, local_now.year + 1):
            try:
                candidate = datetime(year, month, day, hour, minute, tzinfo=zone)
            except ValueError:
                return None
            if candidate.timestamp() > now - 3600:
                return candidate.timestamp()
        return None
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now - 3600:
        # The wall time already passed today, so the message means tomorrow.
        candidate = candidate + timedelta(days=1)
    # A time that passed less than an hour ago means the pool has (just) reset; the
    # caller clamps it to "now".
    return candidate.timestamp()


def parse_reset_at(text: str, now: float) -> float | None:
    """Return the epoch time the limit resets, or None when the text has no usable time."""
    reset: float | None = None
    epoch = _RESET_EPOCH.search(text)
    seconds = _RESET_IN_SECONDS.search(text)
    duration = _RESET_IN_DURATION.search(text)
    clock = _RESET_CLOCK.search(text)
    if epoch:
        reset = float(epoch.group("epoch"))
    elif seconds:
        reset = now + int(seconds.group("seconds"))
    elif duration:
        total = sum(
            int(amount) * _UNIT_SECONDS[unit.lower()]
            for amount, unit in _DURATION_PART.findall(duration.group("duration"))
        )
        reset = now + total if total > 0 else None
    elif clock:
        reset = _parse_clock_reset(clock, now)
    if reset is None or reset <= now - 3600 or reset > now + MAX_RESET_HORIZON_SECONDS:
        return None
    return reset


# A successful-looking turn whose whole visible reply is a limit line (the CLI's silent
# completion path surfaces the JSONL API-error tail as plain assistant text) is longer
# than this only when it is a real reply that merely mentions a limit.
MAX_LIMIT_REPLY_CHARS = 400


def turn_limit_text(*, failed: bool, error_text: str | None, assistant_text: str | None) -> str | None:
    """Return the limit text of a finished turn that hit a credit limit, else None.

    Failed turns are judged by their error text. A turn that did not fail counts only when
    its entire (short) visible reply is a recognized limit line.
    """
    if failed:
        return error_text if is_credit_limit_error(error_text) else None
    reply = (assistant_text or "").strip()
    if reply and len(reply) <= MAX_LIMIT_REPLY_CHARS and is_credit_limit_error(reply):
        return reply
    return None


def parse_credit_limit(text: str | None, now: float) -> CreditLimit | None:
    """Return a :class:`CreditLimit` when *text* is a recognized credit-limit failure."""
    if not text or not is_credit_limit_error(text):
        return None
    return CreditLimit(text=text, reset_at=parse_reset_at(text, now))


def backoff_seconds(consecutive_limit_fires: int) -> int:
    """Bounded exponential backoff (5, 10, 20, 40, 60, 60 ... minutes) for unparsed resets."""
    exponent = max(int(consecutive_limit_fires) - 1, 0)
    return min(BACKOFF_BASE_SECONDS * (2 ** min(exponent, 16)), BACKOFF_MAX_SECONDS)


def rearm_at(limit: CreditLimit, *, now: float, consecutive_limit_fires: int) -> float:
    """When a limit-deferred fire should run again: just after the reset, else after backoff."""
    if limit.reset_at is not None:
        return max(limit.reset_at, now) + RESET_SLACK_SECONDS
    return now + backoff_seconds(consecutive_limit_fires)
