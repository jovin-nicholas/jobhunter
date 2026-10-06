import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from jobhunter.models import Job, ScoreResult, score_label
from jobhunter.store import Store
from tests.helpers import status_counts


def job(i, **kw):
    return Job(f"dice_{i}", "Backend Engineer", "Acme", "Austin, TX", f"https://example.com/{i}",
               description="Java services", source="dice", ats="dice", **kw)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "data" / "jobs.db"
        self.store = Store(self.path)

    def test_terminal_statuses_are_hidden_and_retryable_ones_returned(self):
        for i, status in enumerate(["notified", "logged", "skipped", "filtered", "error_terminal",
                                    "error_429_retry", "error"]):
            self.store.save(job(i), status)
        fresh = job(99)
        unseen = self.store.filter_unseen([job(i) for i in range(7)] + [fresh])
        self.assertEqual([j.id for j in unseen], ["dice_5", "dice_6", "dice_99"])

    def test_save_records_result_resume_and_provenance_then_upserts(self):
        result = ScoreResult(score=8, model="laya:/m", decision="notify", reasoning="fit",
                             matched_skills=["java"], keyword_gaps=["go"], role_type="backend")
        self.store.save(job(1, description_is_snippet=False), "notified", result, resume_id="backend.txt")
        self.store.save(job(2), "filtered", filter_reason="seniority: requires 5+ years (max 3)")
        self.store.save(job(1), "logged", result, resume_id="fullstack.txt")
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            one = dict(conn.execute("SELECT * FROM jobs WHERE id='dice_1'").fetchone())
            two = dict(conn.execute("SELECT * FROM jobs WHERE id='dice_2'").fetchone())
        self.assertEqual((one["status"], one["match_score"], one["source_model"], one["resume_id"]),
                         ("logged", 8, "laya:/m", "fullstack.txt"))
        self.assertEqual(json.loads(one["matched_skills"]), ["java"])
        self.assertEqual((two["status"], two["filter_reason"], two["match_score"]),
                         ("filtered", "seniority: requires 5+ years (max 3)", None))
        self.assertEqual(status_counts(self.store), {"filtered": 1, "logged": 1})

    def test_old_database_gets_the_new_columns(self):
        old = Path(tempfile.mkdtemp()) / "jobs.db"
        with closing(sqlite3.connect(old)) as conn:
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, status TEXT)")
        Store(old)
        with closing(sqlite3.connect(old)) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        self.assertTrue({"resume_id", "filter_reason", "source_model", "jd_is_snippet", "match_score"} <= columns)

    def test_more_ids_than_sqlite_allows_in_one_query(self):
        jobs = [job(i) for i in range(2500)]
        self.store.save(jobs[0], "notified")
        self.assertEqual(len(self.store.filter_unseen(jobs)), 2499)



class TestUnavailableIsRetried(unittest.TestCase):
    def test_error_unavailable_is_not_terminal(self):
        store = Store(Path(tempfile.mkdtemp()) / "jobs.db")
        store.save(job(1), "error_unavailable")
        self.assertEqual([j.id for j in store.filter_unseen([job(1)])], ["dice_1"])


class TestProbability(unittest.TestCase):
    def test_probability_is_stored_and_score_left_empty(self):
        store = Store(Path(tempfile.mkdtemp()) / "jobs.db")
        store.save(job(1), "notified", ScoreResult(score=None, model="laya", decision="notify", probability=0.81))
        with closing(sqlite3.connect(store.path)) as conn:
            self.assertEqual(conn.execute("select match_score, fit_probability from jobs").fetchone(), (None, 0.81))

    def test_a_decimal_score_keeps_its_decimals(self):
        store = Store(Path(tempfile.mkdtemp()) / "jobs.db")
        store.save(job(1), "notified", ScoreResult(score=6.5432, model="laya", decision="notify", probability=0.55))
        with closing(sqlite3.connect(store.path)) as conn:
            self.assertEqual(conn.execute("select match_score from jobs").fetchone(), (6.5432,))

    def test_existing_database_gains_fit_probability(self):
        path = Path(tempfile.mkdtemp()) / "jobs.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, status TEXT)")
            conn.execute("INSERT INTO jobs VALUES ('a', 'Old', 'notified')")
            conn.commit()
        Store(path)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("select fit_probability from jobs where id='a'").fetchone(), (None,))

    def test_the_score_label_is_stored_and_read_back_for_the_test_alert(self):
        store = Store(Path(tempfile.mkdtemp()) / "jobs.db")
        store.save(job(1), "notified", ScoreResult(score=6.4231, model="laya", decision="notify", probability=0.6,
                                                   label="fit 6.42/10"), resume_id="r.txt")
        _, result, _ = store.last_notified()
        self.assertEqual(score_label(result), "fit 6.42/10")
        store.save(job(2), "notified", ScoreResult(score=8, model="ollama", decision="notify"))
        _, result, _ = store.last_notified()
        self.assertEqual(score_label(result), "8/10")                 # no label stored: shown from the score

    def test_existing_database_gains_score_label(self):
        path = Path(tempfile.mkdtemp()) / "jobs.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, status TEXT)")
            conn.execute("INSERT INTO jobs VALUES ('a', 'Old', 'notified')")
            conn.commit()
        Store(path)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("select score_label from jobs where id='a'").fetchone(), (None,))


class TestFeedback(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "jobs.db")
        self.store.save(Job("dice_1", "Backend Engineer", "Acme", "Austin", "https://e/1", source="dice"), "notified",
                        ScoreResult(score=8, model="laya", reasoning="fits", matched_skills=["go"]),
                        resume_id="backend.txt")

    def row(self, verdict, at, mid, job_id="dice_1"):
        return {"job_id": job_id, "verdict": verdict, "received_at": at, "message_id": mid}

    def test_duplicates_are_ignored_and_latest_wins(self):
        new = self.store.add_feedback([self.row("maybe", "2026-10-05T10:00:00+00:00", "<a>"),
                                       self.row("good", "2026-10-05T11:00:00+00:00", "<b>")])
        self.assertEqual(len(new), 2)
        self.assertEqual(self.store.add_feedback([self.row("maybe", "2026-10-05T10:00:00+00:00", "<a>")]), [])
        self.assertEqual(self.store.latest_feedback(), [{"job_id": "dice_1", "job_title": "Backend Engineer",
                                                         "company": "Acme", "verdict": "good", "decision": "notify",
                                                         "received_at": "2026-10-05T11:00:00+00:00"}])

    def test_since_filters(self):
        self.store.add_feedback([self.row("bad", "2026-10-01T10:00:00+00:00", "<a>")])
        self.assertEqual(self.store.latest_feedback(since="2026-10-02"), [])
        self.assertEqual(len(self.store.latest_feedback(since="2026-10-01")), 1)

    def test_table_is_added_to_an_existing_database(self):
        Store(self.store.path)                      # reopening must not fail or drop rows
        self.store.add_feedback([self.row("bad", "2026-10-01T10:00:00+00:00", "<a>")])
        self.assertEqual(len(Store(self.store.path).latest_feedback()), 1)

    def test_last_notified(self):
        job, result, resume_id = self.store.last_notified()
        self.assertEqual((job.id, job.title, result.score, result.matched_skills, resume_id),
                         ("dice_1", "Backend Engineer", 8, ["go"], "backend.txt"))
        self.assertIsNone(Store(Path(tempfile.mkdtemp()) / "jobs.db").last_notified())



if __name__ == "__main__":
    unittest.main()
