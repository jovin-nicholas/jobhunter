import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jobhunter.errors import SettingsError
from jobhunter.schedule import is_due, is_quiet, read_last_run, write_last_run
from jobhunter.settings import ScheduleSettings, load_settings
from tests.helpers import RESUME_TEXT, write_project
from tests.test_cli import SETTINGS, cli
from tests.test_pipeline import BOARDS, SCORERS

NY = ZoneInfo("America/New_York")


def at(text, tz=NY):
    """An aware datetime from 'YYYY-MM-DD HH:MM' in the given zone."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=tz)


class TestScheduleSettings(unittest.TestCase):
    def load(self, schedule):
        tmp = Path(tempfile.mkdtemp())
        return load_settings(write_project(tmp, SETTINGS + schedule, plugins={"boards.py": BOARDS,
                                                                                "scorers.py": SCORERS}), env={})

    def test_defaults_are_hourly_without_quiet_time(self):
        s = self.load("").schedule
        self.assertEqual((s.every_minutes, s.quiet_hours, s.quiet_days, s.timezone), (60, None, (), None))

    def test_values_parse(self):
        s = self.load('schedule: {every: 90m, quiet_hours: "22:00-07:00", quiet_days: [sat, Sunday], '
                      'timezone: America/New_York}\n').schedule
        self.assertEqual((s.every_minutes, s.quiet_hours, s.quiet_days, s.timezone),
                         (90, (22 * 60, 7 * 60), ("sat", "sun"), "America/New_York"))
        self.assertEqual(self.load("schedule: {every: 2h}\n").schedule.every_minutes, 120)

    def test_problems_are_reported_together(self):
        with self.assertRaises(SettingsError) as err:
            self.load('schedule: {every: 5m, quiet_hours: "25:00-07:00", quiet_days: [funday], timezone: Mars/Base}\n')
        text = "\n".join(err.exception.problems)
        for expected in ("schedule.every: must be at least 15m", "schedule.quiet_hours: expected \"HH:MM-HH:MM\"",
                         "schedule.quiet_days: unknown day 'funday'", "schedule.timezone: unknown time zone 'Mars/Base'"):
            self.assertIn(expected, text)
        for bad, message in (("every: 25h", "at most 24h"), ("every: 90s", "minutes or hours"),
                             ("every: 30", "minutes or hours"), ('quiet_hours: "08:00-08:00"', "start and end")):
            with self.assertRaises(SettingsError) as err:
                self.load(f"schedule: {{{bad}}}\n")
            self.assertIn(message, "\n".join(err.exception.problems), bad)


class TestQuietTime(unittest.TestCase):
    def test_a_window_that_crosses_midnight(self):
        s = ScheduleSettings(quiet_hours=(22 * 60, 7 * 60), quiet_hours_text="22:00-07:00", timezone="America/New_York")
        self.assertIsNotNone(is_quiet(s, at("2026-09-29 23:30")))
        self.assertIsNotNone(is_quiet(s, at("2026-09-29 06:59")))
        self.assertIsNone(is_quiet(s, at("2026-09-29 07:00")))
        self.assertIsNone(is_quiet(s, at("2026-09-29 21:59")))
        self.assertIn("22:00-07:00", is_quiet(s, at("2026-09-29 22:00")))

    def test_a_daytime_window(self):
        s = ScheduleSettings(quiet_hours=(12 * 60, 13 * 60), quiet_hours_text="12:00-13:00", timezone="America/New_York")
        self.assertIsNotNone(is_quiet(s, at("2026-09-29 12:30")))
        self.assertIsNone(is_quiet(s, at("2026-09-29 13:00")))

    def test_quiet_days_use_the_configured_time_zone(self):
        s = ScheduleSettings(quiet_days=("sat", "sun"), timezone="America/New_York")
        self.assertIn("sat", is_quiet(s, at("2026-10-03 10:00")))                    # a Saturday in New York
        # Friday 23:00 in New York is already Saturday in UTC; New York's calendar decides.
        self.assertIsNone(is_quiet(s, at("2026-10-02 23:00")))
        self.assertIsNone(is_quiet(ScheduleSettings(), at("2026-10-03 10:00")))


class TestDue(unittest.TestCase):
    def test_due_after_every_with_a_minute_of_slack(self):
        s = ScheduleSettings(every_minutes=30)
        start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(is_due(s, None, start))
        self.assertFalse(is_due(s, start, start + timedelta(minutes=20)))
        self.assertTrue(is_due(s, start, start + timedelta(minutes=29, seconds=5)))   # cron fired a bit early
        self.assertTrue(is_due(s, start, start + timedelta(minutes=45)))

    def test_last_run_file(self):
        path = Path(tempfile.mkdtemp()) / "data" / "last_run"
        self.assertIsNone(read_last_run(path))
        when = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
        write_last_run(path, when)
        self.assertEqual(read_last_run(path), when)
        path.write_text("not a date")
        self.assertIsNone(read_last_run(path))


class TestScheduledRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = write_project(self.tmp, SETTINGS + 'schedule: {every: 30m, quiet_hours: "22:00-07:00", '
                                  'timezone: America/New_York}\n', plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        self.last_run = self.tmp / "data" / "last_run"

    def cli_at(self, when, *args):
        with patch("jobhunter.__main__.utc_now", return_value=when.astimezone(timezone.utc)):
            return cli("--settings", str(self.path), *args)

    def test_quiet_time_skips_the_run(self):
        code, out, _ = self.cli_at(at("2026-09-29 23:15"), "run", "--scheduled")
        self.assertEqual(code, 0)
        self.assertIn("skipped: quiet hours (22:00-07:00)", out)
        self.assertNotIn("[fake] found", out)
        self.assertFalse(self.last_run.exists())

    def test_a_run_is_recorded_and_the_next_waits_its_turn(self):
        code, out, _ = self.cli_at(at("2026-09-29 12:00"), "run", "--scheduled")
        self.assertIn("[fake] found", out)
        self.assertEqual(read_last_run(self.last_run), at("2026-09-29 12:00").astimezone(timezone.utc))
        code, out, _ = self.cli_at(at("2026-09-29 12:10"), "run", "--scheduled")
        self.assertIn("skipped: next run due at 2026-09-29 12:30", out)
        self.assertNotIn("[fake] found", out)
        _, out, _ = self.cli_at(at("2026-09-29 12:30"), "run", "--scheduled")
        self.assertIn("[fake] found", out)

    def test_a_plain_run_ignores_the_schedule_and_a_dry_run_records_nothing(self):
        _, out, _ = self.cli_at(at("2026-09-29 23:15"), "run", "--dry-run")
        self.assertIn("[fake] found", out)
        self.assertFalse(self.last_run.exists())


class TestScheduler(unittest.TestCase):
    def test_the_interval_comes_from_the_settings(self):
        import scheduler
        tmp = Path(tempfile.mkdtemp())
        path = write_project(tmp, SETTINGS + "schedule: {every: 45m}\n", plugins={"boards.py": BOARDS,
                                                                                    "scorers.py": SCORERS})
        sched = scheduler.build_scheduler(path)
        job = sched.get_jobs()[0]
        self.assertEqual(job.trigger.interval, timedelta(minutes=45))
        self.assertEqual(job.args, (["--settings", str(path.resolve()), "run", "--scheduled"],))


class TestRunLock(unittest.TestCase):
    def test_a_second_run_while_one_is_running_is_skipped(self):
        import fcntl
        tmp = Path(tempfile.mkdtemp())
        path = write_project(tmp, SETTINGS, plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        (tmp / "data").mkdir(exist_ok=True)
        with open(tmp / "data" / "run.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)             # another run holds the lock
            code, out, _ = cli("--settings", str(path), "run")
        self.assertEqual(code, 0)
        self.assertIn("skipped: another run is in progress", out)
        self.assertNotIn("[fake] found", out)
        _, out, _ = cli("--settings", str(path), "run")                 # released: runs normally
        self.assertIn("[fake] found", out)


if __name__ == "__main__":
    unittest.main()
