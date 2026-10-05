import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from jobhunter.compare import compare, report
from jobhunter.importer import import_db
from jobhunter.models import Job, ScoreResult
from jobhunter.store import Store
from tests.helpers import write_project
from tests.test_cli import SETTINGS, cli
from tests.test_pipeline import BOARDS, SCORERS

OLD_SCHEMA = ("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, url TEXT, "
              "status TEXT DEFAULT 'pending', match_score INTEGER, resume_version TEXT, run_at TEXT, source TEXT, "
              "seniority_fit TEXT)")


def old_db(path, rows):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(OLD_SCHEMA)
        conn.executemany("INSERT INTO jobs (id, title, company, status, match_score, resume_version) "
                         "VALUES (?, ?, ?, ?, ?, 'newgrad_backend')", rows)
        conn.commit()


def job(job_id):
    return Job(job_id, "Engineer", "Acme", "Remote - US", "https://example.com")


class TestImport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old = self.tmp / "old.db"
        old_db(self.old, [("linkedin_1", "A", "X", "notified", 8), ("dice_2", "B", "Y", "filtered", None),
                          ("omj_3", "C", "Z", "pending", None)])
        self.store = Store(self.tmp / "data" / "jobs.db")

    def test_seen_jobs_are_not_processed_again(self):
        summary = import_db(self.old, self.store)
        self.assertEqual((summary.copied, summary.already_present), (3, 0))
        self.assertEqual(summary.copied_by_status, {"notified": 1, "filtered": 1, "pending": 1})
        unseen = self.store.filter_unseen([job("linkedin_1"), job("dice_2"), job("omj_3"), job("new_4")])
        self.assertEqual([j.id for j in unseen], ["omj_3", "new_4"])

    def test_twice_is_harmless_and_own_rows_win(self):
        self.store.save(job("linkedin_1"), "logged", ScoreResult(6, "laya"))
        before = self.old.read_bytes()
        first = import_db(self.old, self.store)
        second = import_db(self.old, self.store)
        self.assertEqual((first.copied, first.already_present, second.copied), (2, 1, 0))
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id = 'linkedin_1'").fetchone()[0], "logged")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)
        self.assertEqual(self.old.read_bytes(), before)

    def test_not_a_job_notifier_database(self):
        other = self.tmp / "other.db"
        with closing(sqlite3.connect(other)) as conn:
            conn.execute("CREATE TABLE things (x)")
        with self.assertRaises(ValueError):
            import_db(other, self.store)
        with self.assertRaises(FileNotFoundError):
            import_db(self.tmp / "missing.db", self.store)


class TestImportAdoptsFinalStatus(unittest.TestCase):
    def test_an_unfinished_job_takes_the_final_status_from_job_notifier(self):
        tmp = Path(tempfile.mkdtemp())
        old_db(tmp / "old.db", [("linkedin_1", "A", "X", "notified", 8), ("dice_2", "B", "Y", "pending", None)])
        store = Store(tmp / "data" / "jobs.db")
        store.save(job("linkedin_1"), "error_429_retry")
        store.save(job("dice_2"), "error_unavailable")
        summary = import_db(tmp / "old.db", store)
        self.assertEqual((summary.copied, summary.already_present, summary.finished_from_old), (0, 2, 1))
        self.assertEqual([j.id for j in store.filter_unseen([job("linkedin_1"), job("dice_2")])], ["dice_2"])


class TestCompare(unittest.TestCase):
    def test_decisions_are_paired_by_job(self):
        tmp = Path(tempfile.mkdtemp())
        old_db(tmp / "old.db", [("a", "A", "X", "notified", 8), ("b", "B", "Y", "notified", 7),
                                ("c", "C", "Z", "skipped", 2), ("d", "D", "W", "pending", None)])
        store = Store(tmp / "new.db")
        store.save(job("a"), "notified", ScoreResult(9, "laya"))
        store.save(job("b"), "logged", ScoreResult(6, "laya"))
        store.save(job("d"), "skipped", ScoreResult(2, "laya"))
        store.save(job("e"), "notified", ScoreResult(9, "laya"))
        c = compare(tmp / "old.db", store.path)
        self.assertEqual(dict(c.pairs), {("notified", "notified"): 1, ("notified", "logged"): 1})
        self.assertEqual((c.total, c.agreement), (2, 0.5))
        self.assertEqual([(d["id"], d["old_score"], d["new_score"]) for d in c.disagreements], [("b", 7, 6)])
        self.assertIn("same decision for 50%", report(c))
        self.assertEqual(compare(tmp / "old.db", store.path, since="2999-01-01").total, 0)

    def test_compare_shows_probability_when_there_is_no_score(self):
        # job-notifier's database has no fit_probability column; jobhunter's row was decided by the alert method
        tmp = Path(tempfile.mkdtemp())
        old_db(tmp / "old.db", [("a", "A", "X", "logged", 6)])
        store = Store(tmp / "new.db")
        store.save(job("a"), "notified", ScoreResult(score=None, model="laya", decision="notify", probability=0.72))
        c = compare(tmp / "old.db", store.path)
        self.assertEqual([(d["old_score"], d["new_score"]) for d in c.disagreements], [(6, "fit 72%")])
        self.assertIn("(scores 6 / fit 72%)", report(c))


class TestMigrationCli(unittest.TestCase):
    def test_import_and_compare_commands(self):
        tmp = Path(tempfile.mkdtemp())
        path = write_project(tmp, SETTINGS, plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        old_db(tmp / "old.db", [("linkedin_1", "A", "X", "notified", 8)])
        code, out, _ = cli("--settings", str(path), "import-db", str(tmp / "old.db"))
        self.assertEqual(code, 0)
        self.assertIn("copied 1 job(s) (notified: 1)", out)
        code, out, _ = cli("--settings", str(path), "compare-db", str(tmp / "old.db"))
        self.assertEqual(code, 0)
        self.assertIn("1 job(s) decided by both", out)
        code, _, err = cli("--settings", str(path), "import-db", str(tmp / "missing.db"))
        self.assertEqual(code, 2)
        self.assertIn("missing.db", err)


if __name__ == "__main__":
    unittest.main()
