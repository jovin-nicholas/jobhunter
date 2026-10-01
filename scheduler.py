"""Runs jobhunter on the schedule in jobhunter.yaml (`schedule.every`, default hourly; quiet hours and days skip runs).

Run with: .venv/bin/python scheduler.py [path/to/jobhunter.yaml]   (stop with Ctrl+C; restart after changing `every`)
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from jobhunter.__main__ import main
from jobhunter.errors import SettingsError
from jobhunter.settings import load_settings


def build_scheduler(settings_path: str | Path = "jobhunter.yaml") -> BlockingScheduler:
    path = Path(settings_path).expanduser().resolve()
    load_dotenv(path.parent / ".env")
    every = load_settings(path).schedule.every_minutes
    scheduler = BlockingScheduler()
    # --scheduled applies quiet hours and days; the interval itself comes from `every`.
    scheduler.add_job(main, args=[["--settings", str(path), "run", "--scheduled"]], trigger="interval",
                      minutes=every, next_run_time=datetime.now(), misfire_grace_time=300, coalesce=True,
                      max_instances=1)
    return scheduler


if __name__ == "__main__":
    try:
        sched = build_scheduler(sys.argv[1] if len(sys.argv) > 1 else "jobhunter.yaml")
        every = sched.get_jobs()[0].trigger.interval
        print(f"jobhunter scheduler: a run every {every} (restart after changing schedule.every)")
        sched.start()
    except SettingsError as e:
        print("jobhunter cannot start; fix these settings problems:", *(f"  - {p}" for p in e.problems), sep="\n",
              file=sys.stderr)
        sys.exit(2)
