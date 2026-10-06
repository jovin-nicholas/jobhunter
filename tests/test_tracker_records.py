import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from jobhunter.models import Job, ScoreResult
from jobhunter.store import Store
from jobhunter.tracker.records import Applications, iso_date, safe_url, url_key


TODAY = date(2026, 10, 6)


def new_store():
    return Store(Path(tempfile.mkdtemp()) / "jobs.db")


def acme(store, job_id="dice_1", url="https://jobs.example.com/acme/1"):
    store.save(Job(job_id, "Backend Engineer", "Acme", "Austin, TX", url, description="Java services", source="dice"),
               "notified", ScoreResult(score=7.4, model="m", label="fit 7.40/10"), resume_id="backend.txt")


class TestTables(unittest.TestCase):
    def test_an_existing_database_gains_the_tables(self):
        path = Path(tempfile.mkdtemp()) / "jobs.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT)")
        apps = Applications(Store(path))
        with closing(sqlite3.connect(path)) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual({"applications", "application_events", "review_hidden"}, names)
        self.assertEqual(apps.all(), [])


class TestApplications(unittest.TestCase):
    def setUp(self):
        self.store = new_store()
        self.apps = Applications(self.store, today=lambda: TODAY)

    def test_add_starts_in_applied_with_one_event(self):
        app_id = self.apps.add(title="Backend Engineer", company="Acme", applied_at="2026-10-01")
        app = self.apps.get(app_id)
        self.assertEqual((app["stage"], app["stage_at"], app["applied_at"]), ("applied", "2026-10-01", "2026-10-01"))
        self.assertEqual([(e["kind"], e["value"], e["at"]) for e in app["events"]], [("stage", "applied", "2026-10-01")])

    def test_a_later_stage_event_moves_the_cached_stage_and_undo_moves_it_back(self):
        app_id = self.apps.add(title="Backend Engineer", company="Acme", applied_at="2026-10-01")
        event = self.apps.add_event(app_id, "stage", "screen", "2026-10-04")
        self.assertEqual((self.apps.get(app_id)["stage"], self.apps.get(app_id)["stage_at"]), ("screen", "2026-10-04"))
        self.apps.delete_events([event])
        self.assertEqual((self.apps.get(app_id)["stage"], self.apps.get(app_id)["stage_at"]), ("applied", "2026-10-01"))

    def test_follow_ups_set_and_done(self):
        app_id = self.apps.add(title="Backend Engineer", company="Acme", applied_at="2026-10-01")
        self.apps.add_event(app_id, "follow_up_set", "2026-10-08", "2026-10-01")
        self.assertEqual(self.apps.get(app_id)["follow_up_at"], "2026-10-08")
        done = self.apps.add_event(app_id, "follow_up_done", None, "2026-10-08")
        self.assertIsNone(self.apps.get(app_id)["follow_up_at"])
        self.apps.delete_events([done])
        self.assertEqual(self.apps.get(app_id)["follow_up_at"], "2026-10-08")

    def test_bad_input_is_refused(self):
        app_id = self.apps.add(title="Backend Engineer", company="Acme", applied_at="2026-10-01")
        with self.assertRaises(ValueError):
            self.apps.add(title="x", company="y", applied_at="10/05/2026")
        with self.assertRaises(ValueError):
            self.apps.add_event(app_id, "stage", "hired", "2026-10-02")
        with self.assertRaises(ValueError):
            self.apps.add_event(app_id, "note", "hello", "2026-10-02")
        with self.assertRaises(ValueError):
            self.apps.update(app_id, stage="offer")
        with self.assertRaises(KeyError):
            self.apps.add_event(999, "stage", "screen", "2026-10-02")

    def test_dates_out_of_range_are_refused(self):
        app_id = self.apps.add(title="Backend Engineer", company="Acme", applied_at="2026-10-01")
        far_future = "2029-10-06"
        for call in (lambda: self.apps.add(title="x", company="y", applied_at="0002-10-06"),
                     lambda: self.apps.update(app_id, applied_at="0002-10-06"),
                     lambda: self.apps.add(title="x", company="y", applied_at=far_future),
                     lambda: self.apps.add_event(app_id, "follow_up_set", "0002-10-06", "2026-10-01"),
                     lambda: self.apps.add_event(app_id, "follow_up_set", far_future, "2026-10-01"),
                     lambda: self.apps.add_event(app_id, "stage", "screen", "0002-10-06"),
                     lambda: self.apps.add_event(app_id, "stage", "rejected", far_future)):
            with self.subTest(), self.assertRaises(ValueError):
                call()
        self.assertEqual(len(self.apps.all()), 1)
        self.assertEqual(self.apps.get(app_id)["applied_at"], "2026-10-01")
        self.apps.add_event(app_id, "stage", "rejected", "2026-10-07")          # a day ahead: time zones differ
        self.apps.add_event(app_id, "stage", "interview", "2026-10-20")         # scheduled ahead
        with self.assertRaises(ValueError):
            self.apps.add_event(app_id, "stage", "rejected", "2026-10-08")

    def test_update_edits_fields_and_moves_the_first_applied_event(self):
        app_id = self.apps.add(title="", company="", applied_at="2026-10-01")
        self.apps.update(app_id, title=" Platform Engineer ", company="Globex", applied_at="2026-09-28",
                         notes="Referred by a friend", people="Jane at Globex, recruiter")
        app = self.apps.get(app_id)
        self.assertEqual((app["title"], app["company"], app["applied_at"], app["stage_at"]),
                         ("Platform Engineer", "Globex", "2026-09-28", "2026-09-28"))
        self.assertEqual(app["events"][0]["at"], "2026-09-28")
        self.assertEqual((app["notes"], app["people"]), ("Referred by a friend", "Jane at Globex, recruiter"))

    def test_the_current_stage_is_the_last_one_set_not_the_latest_dated(self):
        imported = self.apps.add(title="Platform Engineer", company="Globex", applied_at="2026-09-01", stage="interview")
        self.apps.update(imported, applied_at="2026-09-10")             # the date edit must not undo the interview
        self.assertEqual((self.apps.get(imported)["stage"], self.apps.get(imported)["stage_at"]),
                         ("interview", "2026-09-01"))
        app_id = self.apps.add(title="Backend Engineer", company="Acme", applied_at="2026-10-01")
        self.apps.add_event(app_id, "stage", "interview", "2026-10-09")  # scheduled ahead
        rejected = self.apps.add_event(app_id, "stage", "rejected", "2026-10-06")
        self.assertEqual((self.apps.get(app_id)["stage"], self.apps.get(app_id)["stage_at"]), ("rejected", "2026-10-06"))
        self.apps.delete_events([rejected])                               # undo goes back by the order of actions
        self.assertEqual((self.apps.get(app_id)["stage"], self.apps.get(app_id)["stage_at"]), ("interview", "2026-10-09"))

    def test_reapplying_to_a_job_makes_a_second_application(self):
        acme(self.store)
        first = self.apps.add_from_job("dice_1", "2026-09-01")
        self.apps.add_event(first, "stage", "rejected", "2026-09-10")
        second = self.apps.add_from_job("dice_1", "2026-10-01")
        self.assertNotEqual(first, second)
        self.assertEqual(self.apps.open_for_job("dice_1"), second)

    def test_add_from_job_copies_the_job(self):
        acme(self.store)
        app = self.apps.get(self.apps.add_from_job("dice_1", "2026-10-01"))
        self.assertEqual((app["job_id"], app["title"], app["company"], app["url"], app["board"], app["resume_id"],
                          app["fit"], app["fit_score"]),
                         ("dice_1", "Backend Engineer", "Acme", "https://jobs.example.com/acme/1", "dice",
                          "backend.txt", "fit 7.40/10", 7.4))
        self.assertEqual(self.apps.description(app["id"]), "Java services")
        self.assertIsNone(self.apps.add_from_job("nope_9", "2026-10-01"))

    def test_only_web_links_are_kept(self):
        app_id = self.apps.add(title="<b>Engineer</b>", company="Acme", applied_at="2026-10-01",
                               url="javascript:alert(1)")
        self.assertEqual(self.apps.get(app_id)["url"], "")
        self.assertEqual(self.apps.get(app_id)["title"], "<b>Engineer</b>")     # stored as text; the page escapes it
        self.assertEqual(safe_url(" HTTPS://jobs.example.com/1 "), "HTTPS://jobs.example.com/1")

    def test_exists_ignores_case(self):
        self.apps.add(title="Backend Engineer", company="Acme", applied_at="2026-10-01")
        self.assertTrue(self.apps.exists("acme", "backend engineer", "2026-10-01"))
        self.assertFalse(self.apps.exists("acme", "backend engineer", "2026-10-02"))


class TestLinks(unittest.TestCase):
    def test_url_key_drops_tracking_noise(self):
        base = url_key("https://jobs.example.com/acme/1")
        for noisy in ("https://JOBS.example.com/acme/1/", "https://jobs.example.com/acme/1?utm_source=x&gh_src=y",
                      "https://jobs.example.com/acme/1#apply"):
            with self.subTest(url=noisy):
                self.assertEqual(url_key(noisy), base)

    def test_find_job_matches_exact_then_without_noise(self):
        store = new_store()
        acme(store, url="https://jobs.example.com/acme/1?gh_jid=1")
        apps = Applications(store)
        self.assertEqual(apps.find_job("https://jobs.example.com/acme/1?gh_jid=1"), "dice_1")
        self.assertEqual(apps.find_job("https://jobs.example.com/acme/1/#apply"), "dice_1")
        self.assertIsNone(apps.find_job("https://jobs.example.com/acme/2"))
        self.assertIsNone(apps.find_job("https://jobs.example.com/acme/1_"))     # LIKE wildcards are escaped


class TestIsoDate(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(iso_date("2026-10-05"), "2026-10-05")
        self.assertEqual(iso_date("2026-10-05T12:00:00+00:00"), "2026-10-05")
        for bad in ("10/05/2026", "", "soon", None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                iso_date(bad)


class TestReviewQueue(unittest.TestCase):
    def setUp(self):
        self.store = new_store()
        self.apps = Applications(self.store)
        for i, name in enumerate(("Acme", "Globex", "Initech", "Hooli", "Umbrella")):
            self.store.save(Job(f"dice_{i}", "Software Engineer", name, "Remote", f"https://e.example/{i}",
                                source="dice"), "notified")
        self.store.save(Job("dice_9", "Software Engineer", "Vandelay", "Remote", "https://e.example/9"), "logged")

    def ids(self):
        return [r["job_id"] for r in self.apps.review_queue(14)]

    def test_recent_notified_jobs_newest_first(self):
        self.assertEqual(self.ids(), ["dice_4", "dice_3", "dice_2", "dice_1", "dice_0"])

    def test_applied_bad_and_hidden_jobs_leave_the_queue(self):
        self.apps.add_from_job("dice_0", "2026-10-01")
        self.apps.record_verdict("dice_1", "bad")
        self.apps.hide_from_review("dice_2")
        self.assertEqual(self.ids(), ["dice_4", "dice_3"])

    def test_jobs_older_than_review_days_are_left_out(self):
        later = datetime.now(timezone.utc) + timedelta(days=15)
        self.assertEqual(self.apps.review_queue(14, now=later), [])

    def test_verdicts_are_feedback_rows_export_feedback_reads(self):
        self.apps.record_verdict("dice_3", "applied")
        rows = self.store.latest_feedback()
        self.assertEqual([(r["job_id"], r["verdict"], r["decision"]) for r in rows], [("dice_3", "applied", "notify")])
        with self.assertRaises(ValueError):
            self.apps.record_verdict("dice_3", "maybe later")


class TestVerdictUndo(unittest.TestCase):
    def test_only_a_tracker_label_can_be_deleted(self):
        store = new_store()
        apps = Applications(store)
        message_id = apps.record_verdict("dice_1", "bad")
        self.assertTrue(message_id.startswith("tracker:"))
        store.add_feedback([{"job_id": "dice_2", "verdict": "bad", "received_at": "2026-10-05T10:00:00+00:00",
                             "message_id": "<m1@mail.example.com>"}])
        with self.assertRaises(ValueError):
            apps.delete_verdict("<m1@mail.example.com>")
        apps.delete_verdict(message_id)
        self.assertEqual([r["job_id"] for r in store.latest_feedback()], ["dice_2"])

    def test_unhide_and_delete_application(self):
        store = new_store()
        acme(store)
        apps = Applications(store)
        apps.hide_from_review("dice_1")
        apps.unhide("dice_1")
        self.assertEqual([r["job_id"] for r in apps.review_queue(14)], ["dice_1"])
        app_id = apps.add_from_job("dice_1", "2026-10-01")
        apps.delete_application(app_id)
        self.assertEqual(apps.all(), [])
        with closing(sqlite3.connect(store.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0], 0)


class TestApplyVerdicts(unittest.TestCase):
    def test_an_applied_verdict_becomes_one_application(self):
        store = new_store()
        acme(store)
        apps = Applications(store)
        row = {"job_id": "dice_1", "verdict": "applied", "received_at": "2026-10-05T10:00:00+00:00"}
        self.assertEqual(apps.apply_verdicts([row, {**row, "verdict": "good"}]), 1)
        self.assertEqual(apps.apply_verdicts([row]), 0)                          # already open: no second one
        self.assertEqual([(a["job_id"], a["applied_at"]) for a in apps.all()], [("dice_1", "2026-10-05")])
