import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from jobhunter.models import Job, ScoreResult
from jobhunter.store import Store


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
        self.assertEqual(self.store.status_counts(), {"filtered": 1, "logged": 1})

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


if __name__ == "__main__":
    unittest.main()
