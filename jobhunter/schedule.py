"""When a scheduled run may happen: the run interval and optional quiet hours and days from `schedule:`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jobhunter.settings import DAYS, ScheduleSettings

SLACK = timedelta(minutes=1)     # a cron entry firing a few seconds early still counts as due


def local_time(settings: ScheduleSettings, now: datetime) -> datetime:
    return now.astimezone(ZoneInfo(settings.timezone)) if settings.timezone else now.astimezone()


def is_quiet(settings: ScheduleSettings, now: datetime) -> str | None:
    """Why `now` is quiet time ("quiet hours (22:00-07:00)", "quiet day (sat)"), or None when runs may happen."""
    local = local_time(settings, now)
    day = DAYS[local.weekday()]
    if day in settings.quiet_days:
        return f"quiet day ({day})"
    if settings.quiet_hours:
        start, end = settings.quiet_hours
        minute = local.hour * 60 + local.minute
        inside = start <= minute < end if start < end else (minute >= start or minute < end)
        if inside:
            return f"quiet hours ({settings.quiet_hours_text})"
    return None


def next_due(settings: ScheduleSettings, last_run: datetime) -> datetime:
    return last_run + timedelta(minutes=settings.every_minutes)


def is_due(settings: ScheduleSettings, last_run: datetime | None, now: datetime) -> bool:
    return last_run is None or now >= next_due(settings, last_run) - SLACK


def read_last_run(path: Path) -> datetime | None:
    """When the last real run started; None when there is none or the file is unreadable (the run is then due)."""
    try:
        when = datetime.fromisoformat(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def write_last_run(path: Path, when: datetime) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(when.astimezone(timezone.utc).isoformat() + "\n", encoding="utf-8")
