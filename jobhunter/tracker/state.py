"""What the page shows: applications grouped by what needs the user, the review queue, the pipeline counts and the
numbers. Pure: the server passes today's date in, so tests pin it."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

from jobhunter.settings import TrackerSettings
from jobhunter.tracker import CLOSED_STAGES, OPEN_STAGES, STAGES
from jobhunter.tracker.numbers import reply_numbers

SOON_DAYS = 3          # a screen or interview dated this soon puts the application under "needs you"


def last_activity(app: dict) -> date:
    return max([date.fromisoformat(e["at"]) for e in app["events"]] or [date.fromisoformat(app["applied_at"])])


def _follow_up_due(app: dict, today: date, settings: TrackerSettings) -> str | None:
    """The follow-up date: the one set, else for an application still in `applied` with no follow-up event at all
    (added by review, quick add, an email or the import) the applied date plus the `applied` follow-up days. A derived
    date more than ghost_after_days past is dropped, so old imported rows become ghost suggestions instead."""
    if app.get("follow_up_at"):
        return app["follow_up_at"]
    days = settings.follow_up_days.get("applied")
    if app["stage"] != "applied" or not days or any(e["kind"] in ("follow_up_set", "follow_up_done")
                                                    for e in app["events"]):
        return None
    due = date.fromisoformat(app["applied_at"]) + timedelta(days=days)
    return due.isoformat() if (today - due).days <= settings.ghost_after_days else None


def attention(app: dict, today: date, settings: TrackerSettings) -> tuple[int, str, str | None] | None:
    """(priority, reason, due date) when the application needs the user, lowest priority first; None when it can
    wait. Order: an offer, a screen or interview in the next SOON_DAYS, a follow-up due, then a ghost suggestion."""
    if app["stage"] in CLOSED_STAGES:
        return None
    if app["stage"] == "offer":
        return 0, "offer", None
    soon = today + timedelta(days=SOON_DAYS)
    upcoming = sorted((e["at"], e["value"]) for e in app["events"] if e["kind"] == "stage"
                      and e["value"] in ("screen", "interview") and today <= date.fromisoformat(e["at"]) <= soon)
    if upcoming:
        at, value = upcoming[0]
        return 1, value, at
    follow_up = _follow_up_due(app, today, settings)
    if follow_up and date.fromisoformat(follow_up) <= today:
        return 2, "follow up", follow_up
    if app["stage"] == "applied" and (today - last_activity(app)).days >= settings.ghost_after_days:
        return 3, "ghost?", None
    return None


def follow_up_for(stage: str, at: date, settings: TrackerSettings) -> str | None:
    """The follow-up date set when an application moves to `stage` on `at`; None for stages without one."""
    days = settings.follow_up_days.get(stage)
    return (at + timedelta(days=days)).isoformat() if days else None


def build_state(apps: list[dict], review: list[dict], settings: TrackerSettings, today: date) -> dict:
    needs, waiting, closed = [], [], []
    for app in apps:
        row = {**app, "age_days": max(0, (today - last_activity(app)).days)}
        why = attention(app, today, settings)
        if app["stage"] in CLOSED_STAGES:
            closed.append(row)
        elif why:
            row["priority"], row["reason"], row["due"] = why
            needs.append(row)
        else:
            waiting.append(row)
    needs.sort(key=lambda r: (r["priority"], r["due"] or "", -r["age_days"]))
    waiting.sort(key=lambda r: (-r["age_days"], r["id"]))
    closed.sort(key=lambda r: (r["stage_at"], r["id"]), reverse=True)
    counts = {stage: 0 for stage in STAGES}
    for app in apps:
        counts[app["stage"]] += 1
    return {"today": today.isoformat(), "groups": {"needs": needs, "waiting": waiting, "closed": closed},
            "review": review, "counts": counts, "numbers": reply_numbers(apps), "stages": list(STAGES),
            "open_stages": list(OPEN_STAGES), "closed_stages": list(CLOSED_STAGES), "settings": asdict(settings)}
