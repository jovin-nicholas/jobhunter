import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from email import message_from_string
from unittest import mock
from datetime import date, datetime, timezone
from pathlib import Path

from jobhunter.notify import send_email
from jobhunter.settings import NotifySettings, ScheduleSettings, TrackerSettings
from jobhunter.store import Store
from jobhunter.tracker.digest import STATE_FILE, compose, digest_address, due_items, send_digest
from jobhunter.tracker.records import Applications

TODAY = date(2026, 10, 7)


def app(i, stage="applied", applied="2026-10-01", follow_up=None, events=(), people="", notes="",
        url="https://jobs.example.com/1"):
    evs = [{"kind": "stage", "value": "applied", "at": applied, "created_at": applied + "T09:00:00+00:00"}]
    evs += [{"kind": "stage", "value": v, "at": at, "created_at": made + "T09:00:00+00:00"} for v, at, made in events]
    if follow_up:
        evs.append({"kind": "follow_up_set", "value": follow_up, "at": applied, "created_at": applied})
    return {"id": i, "company": f"Co{i}", "title": "Engineer", "url": url, "stage": stage, "stage_at": evs[-1]["at"],
            "applied_at": applied, "follow_up_at": follow_up, "people": people, "notes": notes, "events": evs}


@contextmanager
def local_zone(name):
    """Run with the computer's zone set to `name`, so tests don't depend on the machine's zone."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


class TestDueItems(unittest.TestCase):
    def test_groups(self):
        apps = [app(1, follow_up="2026-10-07"),                                       # follow up today
                app(2, "interview", events=[("interview", "2026-10-07", "2026-10-02")]),   # interview today
                app(3, follow_up="2026-10-03"),                                        # 4 days late
                app(4, "screen", events=[("screen", "2026-10-09", "2026-10-05")]),       # call this week
                app(5, "screen", events=[("screen", "2026-10-15", "2026-10-05")]),       # 8 days out: not this week
                app(6, "offer", events=[("offer", "2026-10-06", "2026-10-06")]),         # offers are never listed
                app(7, "rejected", follow_up="2026-10-07", events=[("rejected", "2026-10-06", "2026-10-06")]),
                app(8, applied="2026-08-01")]                                          # quiet for 30+ days
        items = due_items(apps, TrackerSettings(), TODAY)
        self.assertEqual([(e["id"], e["reason"], e["when"]) for e in items["today"]],
                         [(1, "follow up", "today"), (2, "interview", "today")])
        self.assertEqual([(e["id"], e["when"]) for e in items["overdue"]], [(3, "4 days late")])
        self.assertEqual([(e["id"], e["reason"], e["when"]) for e in items["this_week"]], [(4, "screen", "Fri Oct 9")])
        self.assertEqual(items["quiet"], 1)

    def test_a_new_application_is_due_its_default_follow_up(self):
        items = due_items([app(1, applied="2026-09-30")], TrackerSettings(), TODAY)          # 7 days ago
        self.assertEqual([e["id"] for e in items["today"]], [1])

    def test_a_call_logged_for_today_on_today_is_not_scheduled_ahead(self):
        items = due_items([app(1, "screen", events=[("screen", "2026-10-07", "2026-10-07")])], TrackerSettings(), TODAY)
        self.assertEqual(items["today"], [])

    def test_a_call_entered_in_the_evening_for_tomorrow_is_scheduled_ahead(self):
        evening = app(1, "interview")
        evening["events"].append({"kind": "stage", "value": "interview", "at": "2026-10-08",
                                  "created_at": "2026-10-08T00:30:00+00:00"})     # 20:30 EDT on the 7th
        same_day = app(2, "interview")
        same_day["events"].append({"kind": "stage", "value": "interview", "at": "2026-10-08",
                                   "created_at": "2026-10-08T13:00:00+00:00"})    # 09:00 EDT on the 8th
        with local_zone("America/New_York"):
            items = due_items([evening, same_day], TrackerSettings(), TODAY)
            self.assertEqual([(e["id"], e["when"]) for e in items["this_week"]], [(1, "tomorrow")])
            self.assertEqual(due_items([same_day], TrackerSettings(), date(2026, 10, 8))["today"], [])

    def test_an_offer_gives_nothing_even_with_a_call_scheduled_today(self):
        offer = app(1, "offer", follow_up="2026-10-07",
                    events=[("interview", "2026-10-07", "2026-10-02"), ("offer", "2026-10-06", "2026-10-06")])
        items = due_items([offer], TrackerSettings(), TODAY)
        self.assertEqual((items["today"], items["overdue"], items["this_week"]), ([], [], []))
        self.assertIsNone(compose(items, TODAY))

    def test_one_day_late_and_tomorrow_read_naturally(self):
        items = due_items([app(1, follow_up="2026-10-06"),
                           app(2, "screen", events=[("screen", "2026-10-08", "2026-10-01")])], TrackerSettings(), TODAY)
        self.assertEqual(items["overdue"][0]["when"], "1 day late")
        self.assertEqual(items["this_week"][0]["when"], "tomorrow")

    def test_quiet_days_reflects_settings(self):
        items = due_items([app(1, applied="2026-09-01")], TrackerSettings(ghost_after_days=21), TODAY)
        self.assertEqual(items["quiet_days"], 21)


class TestCompose(unittest.TestCase):
    def items(self, *apps, settings=None):
        return due_items(list(apps), settings or TrackerSettings(), TODAY)

    def test_nothing_due_today_gives_nothing_to_send(self):
        self.assertIsNone(compose(self.items(app(3, follow_up="2026-10-03"),
                                             app(4, "screen", events=[("screen", "2026-10-09", "2026-10-05")])), TODAY))

    def test_subject_and_entries(self):
        subject, plain, html = compose(self.items(
            app(1, follow_up="2026-10-07", people="Jane at Co1, recruiter", notes="Call back\nafter 2pm"),
            app(2, "interview", events=[("interview", "2026-10-07", "2026-10-02")]),
            app(3, follow_up="2026-10-03"),
            app(4, "screen", events=[("screen", "2026-10-09", "2026-10-05")]),
            app(5, "screen", events=[("screen", "2026-10-10", "2026-10-05")])), TODAY)
        self.assertEqual(subject, "[jobhunter] Due today: Co1, Co2 · 1 overdue · 2 calls this week")
        for text in ("Due today", "Overdue", "Calls this week", "People: Jane at Co1, recruiter", "Call back", "after 2pm",
                     "python -m jobhunter tracker"):
            self.assertIn(text, plain)
        self.assertIn('<a href="https://jobs.example.com/1">Engineer</a>', html)
        self.assertIn("Call back<br>after 2pm", html)
        self.assertNotIn("mailto:", html)                                   # no buttons
        self.assertEqual(html.count("<a "), 5)                              # only the five job links

    def test_markup_is_escaped_and_bad_links_dropped(self):
        bad = app(1, follow_up="2026-10-07", url="javascript:alert(1)", notes="<b>bold</b> & co")
        bad["title"] = "<script>x</script>"
        _, plain, html = compose(self.items(bad), TODAY)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; co", html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("<a ", html)

    def test_a_line_break_in_a_company_or_title_stays_on_one_line(self):
        broken = app(1, follow_up="2026-10-07")
        broken["company"], broken["title"] = "Acme\nCorp", "Senior\r\nEngineer"
        subject, plain, html = compose(self.items(broken), TODAY)
        self.assertEqual(subject, "[jobhunter] Due today: Acme Corp")
        self.assertIn("- Acme Corp: Senior Engineer (applied), follow up today", plain)
        env = {"GMAIL_ADDRESS": "sender@example.com", "GMAIL_APP_PASSWORD": "x"}
        with mock.patch("jobhunter.notify.smtplib.SMTP") as smtp:
            self.assertTrue(send_email(EMAIL, env, subject, plain, html, self.fail, to="sender+followup@example.com"))
        raw = smtp.return_value.__enter__.return_value.sendmail.call_args.args[2]
        self.assertEqual(message_from_string(raw)["Subject"], subject)

    def test_one_call_is_singular(self):
        subject, _, _ = compose(self.items(app(1, follow_up="2026-10-07"),
                                           app(4, "screen", events=[("screen", "2026-10-09", "2026-10-05")])), TODAY)
        self.assertEqual(subject, "[jobhunter] Due today: Co1 · 1 call this week")

    def test_footer_uses_configured_ghost_after_days(self):
        quiet_app = app(9, applied="2026-08-01")
        due_app = app(1, follow_up="2026-10-07")
        _, plain, html = compose(self.items(due_app, quiet_app, settings=TrackerSettings(ghost_after_days=21)), TODAY)
        self.assertIn("quiet for 21+ days", plain)
        self.assertIn("quiet for 21+ days", html)


EMAIL = {"from_env": "GMAIL_ADDRESS", "password_env": "GMAIL_APP_PASSWORD"}


class FakeSettings:
    def __init__(self, folder, on=True, at="08:00", tz="America/New_York"):
        self.tracker = TrackerSettings(follow_up_email=on, digest_at=at)
        self.notify = NotifySettings(email=dict(EMAIL))
        self.schedule = ScheduleSettings(timezone=tz)
        self.data_dir = Path(folder)


class TestSendDigest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.store = Store(self.folder / "jobs.db")
        Applications(self.store).add(title="Engineer", company="Acme", applied_at="2026-09-30")   # due 2026-10-07
        self.sent = []

    def send(self, ok=True):
        def sender(subject, plain, html):
            self.sent.append(subject)
            return ok
        return sender

    def at(self, local):          # a New York wall-clock time on 2026-10-07 (UTC-4)
        hour, minute = map(int, local.split(":"))
        return datetime(2026, 10, 7, hour + 4, minute, tzinfo=timezone.utc) if hour < 20 else \
            datetime(2026, 10, 8, hour + 4 - 24, minute, tzinfo=timezone.utc)

    def test_not_before_digest_at_then_once_that_day(self):
        s = FakeSettings(self.folder)
        self.assertIsNone(send_digest(s, self.store, self.at("07:59"), self.send()))
        self.assertIn("sent", send_digest(s, self.store, self.at("08:05"), self.send()))
        self.assertIsNone(send_digest(s, self.store, self.at("09:00"), self.send()))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual((self.folder / STATE_FILE).read_text().strip(), "2026-10-07")

    def test_the_local_date_decides_late_in_the_evening(self):
        s = FakeSettings(self.folder)
        send_digest(s, self.store, self.at("08:05"), self.send())
        self.assertIsNone(send_digest(s, self.store, self.at("23:30"), self.send()))   # 03:30 UTC on the 8th
        self.assertEqual(len(self.sent), 1)

    def test_off_or_nothing_due_sends_nothing(self):
        self.assertIsNone(send_digest(FakeSettings(self.folder, on=False), self.store, self.at("09:00"), self.send()))
        empty = Path(tempfile.mkdtemp())
        self.assertIsNone(send_digest(FakeSettings(empty), Store(empty / "jobs.db"), self.at("09:00"), self.send()))
        self.assertEqual((empty / STATE_FILE).read_text().strip(), "2026-10-07")     # recorded: not recomputed
        self.assertEqual(self.sent, [])

    def test_a_failed_send_is_retried_once_then_stops_for_the_day(self):
        s = FakeSettings(self.folder)
        self.assertIn("tried again", send_digest(s, self.store, self.at("08:05"), self.send(ok=False)))
        self.assertIn("not tried again today", send_digest(s, self.store, self.at("09:05"), self.send(ok=False)))
        self.assertIsNone(send_digest(s, self.store, self.at("10:05"), self.send(ok=False)))
        self.assertEqual(len(self.sent), 2)

    def test_the_address_adds_the_tag_to_the_alerts_recipient(self):
        env = {"GMAIL_ADDRESS": "sender@example.com", "NOTIFY_EMAIL": "john.doe@example.com"}
        self.assertEqual(digest_address(EMAIL, env, "followup"), "sender+followup@example.com")
        self.assertEqual(digest_address({**EMAIL, "to_env": "NOTIFY_EMAIL"}, env, "followup"),
                         "john.doe+followup@example.com")
