"""Helpers shared by the built-in boards."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Iterator

import requests

from jobhunter.models import Job, SearchContext


def cutoff(ctx: SearchContext) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=ctx.max_age_hours)


def parse_time(value: Any) -> datetime | None:
    """ISO 8601 text, a Unix timestamp in seconds or milliseconds, or "September 24, 2026"; None when unreadable."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        seconds = float(value)
        if seconds > 1e11:            # milliseconds (Lever)
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(text, "%B %d, %Y")          # Amazon
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_LONG_DATE = re.compile(r"^[A-Za-z]+ +\d{1,2}, \d{4}$")         # "September 24, 2026" (Amazon)


def posted_iso(value: Any) -> str:
    """A posting date as jobs.db stores it: ISO text. A Unix timestamp (Lever) becomes UTC ISO and "September 24,
    2026" a date; ISO text and text that is not a date are kept as given."""
    if value is None or value == "":
        return ""
    text = str(value).strip()
    if isinstance(value, (int, float)) or text.isdigit():
        when = parse_time(value)
        return when.isoformat() if when else text
    if _LONG_DATE.match(text):
        when = parse_time(text)
        return when.date().isoformat() if when else text
    return str(value)


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$|^[A-Za-z]+ +\d{1,2}, \d{4}$")


def is_fresh(value: Any, cutoff_at: datetime) -> bool:
    """Jobs without a readable date are kept, as job-notifier did. A date without a time ("2026-09-28",
    "September 28, 2026") is fresh while its day is inside the window: read as midnight, a job posted late yesterday
    would otherwise look older than it is."""
    dt = parse_time(value)
    if dt is None:
        return True
    if isinstance(value, str) and _DATE_ONLY.match(value.strip()):
        return dt.date() >= cutoff_at.date()
    return dt >= cutoff_at


def is_gone(error: BaseException) -> bool:
    """True for an HTTP 404 or 410: the posting no longer exists."""
    response = getattr(error, "response", None)
    return isinstance(error, requests.HTTPError) and getattr(response, "status_code", None) in (404, 410)


def each_listing(ctx: SearchContext, board: str, listings: Iterable[Any],
                 convert: Callable[[Any], Job | None]) -> Iterator[Job]:
    """convert(listing) for every listing; one that cannot be read is logged and skipped, and the rest carry on."""
    for listing in listings:
        try:
            job = convert(listing)
        except Exception as e:
            ctx.log(f"{board}: skipped a listing it could not read ({type(e).__name__}: {e})")
            continue
        if job is not None:
            yield job
