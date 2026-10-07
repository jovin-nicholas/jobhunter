"""The follow-up email: when a follow-up, screen or interview has fallen due since the last digest day, one email
listing what is due today, overdue follow-ups and calls this week, with each job's link, people and notes. No buttons:
only the job links."""
from __future__ import annotations

import html as html_lib
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping

from jobhunter.feedback import plus_address
from jobhunter.notify import email_addresses
from jobhunter.schedule import local_time
from jobhunter.settings import TrackerSettings
from jobhunter.tracker import CLOSED_STAGES
from jobhunter.tracker.records import Applications, safe_url
from jobhunter.tracker.state import follow_up_due, last_activity, scheduled_ahead

CALL_DAYS = 7                     # "calls this week": screens and interviews in the next 7 days after today
CALLS = ("screen", "interview")
GROUPS = (("today", "Due today"), ("overdue", "Overdue"), ("this_week", "Calls this week"))


def _scheduled_calls(app: dict) -> list[tuple[str, str]]:
    """(date, stage) of each screen or interview entered ahead of its date; one logged on its own day has happened."""
    return [(e["at"], e["value"]) for e in app["events"] if e["kind"] == "stage" and e["value"] in CALLS
            and scheduled_ahead(e)]


def _on(day: date, today: date) -> str:
    return "tomorrow" if day == today + timedelta(days=1) else f"{day:%a} {day:%b} {day.day}"


def due_items(apps: list[dict], settings: TrackerSettings, today: date, since: date | None = None) -> dict:
    """What the email lists. `new_since` holds the listed entries that fell due after `since`, the last digest day,
    up to today (today only when `since` is None): those make the email go, so each item triggers it once."""
    items: dict = {"today": [], "overdue": [], "this_week": [], "new_since": [], "quiet": 0,
                   "quiet_days": settings.ghost_after_days}
    iso, week_end = today.isoformat(), today + timedelta(days=CALL_DAYS)

    def entry(app: dict, reason: str, when: str, sort: str) -> dict:
        return {"id": app["id"], "company": app.get("company") or "", "title": app.get("title") or "",
                "url": safe_url(app.get("url") or ""), "stage": app["stage"], "people": app.get("people") or "",
                "notes": app.get("notes") or "", "reason": reason, "when": when, "sort": sort}
    for app in apps:
        if app["stage"] in CLOSED_STAGES or app["stage"] == "offer":     # offers are never in the email
            continue
        follow_up = follow_up_due(app, today, settings)
        if follow_up == iso:
            items["today"].append(entry(app, "follow up", "today", iso))
        elif follow_up and follow_up < iso:
            late = (today - date.fromisoformat(follow_up)).days
            items["overdue"].append(entry(app, "follow up", f"{late} day{'s' if late != 1 else ''} late", follow_up))
        for at, stage in _scheduled_calls(app):
            day = date.fromisoformat(at)
            if day == today:
                items["today"].append(entry(app, stage, "today", at))
            elif today < day <= week_end:
                items["this_week"].append(entry(app, stage, _on(day, today), at))
        # Counted as the page's "ghost?": quiet long enough, and no follow-up due yet.
        if (app["stage"] == "applied" and not (follow_up and follow_up <= iso)
                and (today - last_activity(app)).days >= settings.ghost_after_days):
            items["quiet"] += 1
    for key in ("today", "overdue", "this_week"):
        items[key].sort(key=lambda e: (e["sort"], e["company"].lower(), e["id"]))
    after = (since or today - timedelta(days=1)).isoformat()
    items["new_since"] = [e for e in items["overdue"] + items["today"] if after < e["sort"] <= iso]
    return items


def _one_line(text: str) -> str:
    """A line break in a company or title would break the Subject header and the plain-text entry's first line."""
    return " ".join(text.split())


def _subject(items: dict, today: date) -> str:
    def companies(entries: list[dict]) -> str:
        return ", ".join(dict.fromkeys(_one_line(e["company"]) or "—" for e in entries))
    if items["today"]:
        parts = [f"Due today: {companies(items['today'])}"]
    else:
        first = date.fromisoformat(min(e["sort"] for e in items["new_since"]))
        parts = [f"Due since {first:%a} {first:%b} {first.day}: {companies(items['new_since'])}"]
    if items["overdue"]:
        parts.append(f"{len(items['overdue'])} overdue")
    if items["this_week"]:
        n = len(items["this_week"])
        parts.append(f"{n} call{'s' if n != 1 else ''} this week")
    return "[jobhunter] " + " · ".join(parts)


def _plain_entry(e: dict) -> str:
    lines = [f"- {_one_line(e['company']) or '—'}: {_one_line(e['title']) or '—'} ({e['stage']}), {e['reason']} "
             f"{e['when']}"]
    if e["url"]:
        lines.append(f"  {e['url']}")
    if e["people"]:
        lines.append(f"  People: {e['people']}")
    if e["notes"]:
        lines.append("  Notes: " + e["notes"].replace("\n", "\n         "))
    return "\n".join(lines)


def _html_entry(e: dict) -> str:
    esc = lambda s: html_lib.escape(str(s or ""), quote=True)    # noqa: E731
    title = f'<a href="{esc(e["url"])}">{esc(e["title"] or "—")}</a>' if e["url"] else esc(e["title"] or "—")
    extra = ""
    if e["people"]:
        extra += f'<div style="color:#555">People: {esc(e["people"])}</div>'
    if e["notes"]:
        extra += f'<div style="color:#555">Notes: {esc(e["notes"]).replace(chr(10), "<br>")}</div>'
    return (f'<li style="margin:0 0 10px"><strong>{esc(e["company"] or "—")}</strong>: {title} '
            f'<span style="color:#555">({esc(e["stage"])}, {esc(e["reason"])} {esc(e["when"])})</span>{extra}</li>')


def compose(items: dict, today: date) -> tuple[str, str, str] | None:
    """(subject, plain text, HTML), or None when nothing fell due since the last digest day: older overdue items and
    calls this week alone send nothing, since they were already in an earlier email."""
    if not items["new_since"]:
        return None
    plain, body = [], []
    for key, heading in GROUPS:
        if items[key]:
            plain.append(heading + "\n" + "\n".join(_plain_entry(e) for e in items[key]))
            body.append(f'<h3 style="font-size:15px;margin:18px 0 6px">{heading}</h3><ul style="padding-left:18px">'
                        + "".join(_html_entry(e) for e in items[key]) + "</ul>")
    footer = "Open the tracker with `python -m jobhunter tracker`."
    if items["quiet"]:
        footer = (f"Also {items['quiet']} application{'s' if items['quiet'] != 1 else ''} "
                  f"quiet for {items['quiet_days']}+ days. " + footer)
    text = "\n\n".join(plain) + "\n\n" + footer
    page = ('<div style="font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#1c1b19">' + "".join(body)
            + f'<p style="color:#555;margin-top:18px">{html_lib.escape(footer)}</p></div>')
    return _subject(items, today), text, page


STATE_FILE = "tracker_digest"     # in data/: "2026-10-07" (done for that day) or "2026-10-07 failed 1"
MAX_FAILURES = 2


def digest_address(cfg: dict, env: Mapping[str, str], tag: str) -> str:
    """Where the follow-up email goes: the alerts' recipient with +tag (an existing +tag is replaced)."""
    return plus_address(email_addresses(cfg, env)[1], tag)


def _read_state(path: Any) -> tuple[str, int]:
    try:
        parts = path.read_text(encoding="utf-8").split()
    except (OSError, ValueError):
        return "", 0
    if not parts:
        return "", 0
    failures = int(parts[2]) if len(parts) == 3 and parts[1] == "failed" and parts[2].isdigit() else 0
    return parts[0], failures


def _write_state(path: Any, day: str, failures: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(day + (f" failed {failures}" if failures else "") + "\n", encoding="utf-8")


def _since(last: str, failures: int) -> date | None:
    """The last digest day: items due after it make the email go. A day whose email failed was not delivered, so its
    own items still count (the day before it is used); an unreadable day means today only."""
    try:
        day = date.fromisoformat(last)
    except ValueError:
        return None
    return day - timedelta(days=1) if failures else day


def send_digest(settings: Any, store: Any, now: datetime, send: Callable[[str, str, str], bool]) -> str | None:
    """Send today's follow-up email if it is on, it is digest_at or later locally, today's has not gone yet and
    something fell due since the last digest day. Returns a line for the run log, or None when there is nothing to
    say."""
    tracker = settings.tracker
    if not tracker.follow_up_email or not settings.notify.email:
        return None
    local = local_time(settings.schedule, now)
    if local.strftime("%H:%M") < tracker.digest_at:
        return None
    day, path = local.date().isoformat(), settings.data_dir / STATE_FILE
    last, failures = _read_state(path)
    if last == day and (failures == 0 or failures >= MAX_FAILURES):
        return None
    message = compose(due_items(Applications(store).all(), tracker, local.date(), _since(last, failures)),
                      local.date())
    if message is None:
        _write_state(path, day)
        return None
    if send(*message):
        _write_state(path, day)
        return "follow-up email: sent"
    failures = failures + 1 if last == day else 1
    _write_state(path, day, failures)
    if failures >= MAX_FAILURES:
        return "follow-up email: not sent again; not tried again today"
    return "follow-up email: not sent; tried again on the next run"
