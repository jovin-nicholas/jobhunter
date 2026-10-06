import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from jobhunter.models import Job, ScoreResult
from jobhunter.store import Store
from jobhunter.tracker.records import Applications, iso_date, safe_url, url_key


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
        self.apps = Applications(self.store)

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

    def test_update_edits_fields_and_moves_the_first_applied_event(self):
        app_id = self.apps.add(title="", company="", applied_at="2026-10-01")
        self.apps.update(app_id, title=" Platform Engineer ", company="Globex", applied_at="2026-09-28",
                         notes="Referred by a friend", people="Jane at Globex, recruiter")
        app = self.apps.get(app_id)
        self.assertEqual((app["title"], app["company"], app["applied_at"], app["stage_at"]),
                         ("Platform Engineer", "Globex", "2026-09-28", "2026-09-28"))
        self.assertEqual(app["events"][0]["at"], "2026-09-28")
        self.assertEqual((app["notes"], app["people"]), ("Referred by a friend", "Jane at Globex, recruiter"))

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
