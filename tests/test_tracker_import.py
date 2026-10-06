import tempfile
import unittest
from pathlib import Path

from jobhunter.models import Job
from jobhunter.store import Store
from jobhunter.tracker.importing import import_csv
from jobhunter.tracker.records import Applications


class TestImport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp / "jobs.db")
        self.store.save(Job("dice_1", "Backend Engineer", "Acme", "Remote", "https://jobs.example.com/acme/1",
                            source="dice"), "notified")
        self.apps = Applications(self.store)

    def csv(self, text, encoding="utf-8"):
        path = self.tmp / "old.csv"
        path.write_text(text, encoding=encoding)
        return path

    def test_columns_in_any_order_and_a_later_stage_adds_both_events(self):
        path = self.csv("stage,applied_at,title,company,notes\n"
                        "interview,2026-09-01,Platform Engineer,Globex,ignored\n"
                        ",2026-09-02,SWE,Initech,\n")
        report = import_csv(path, self.apps)
        self.assertEqual((report.added, report.duplicates, report.problems), (2, 0, []))
        globex = next(a for a in self.apps.all() if a["company"] == "Globex")
        self.assertEqual(globex["stage"], "interview")
        self.assertEqual([e["value"] for e in globex["events"]], ["applied", "interview"])

    def test_excel_csv_with_a_bom_odd_headers_and_blank_lines(self):
        path = self.csv("Company ,Title,Applied_At\n\nGlobex,Platform Engineer,2026-09-01\n", encoding="utf-8-sig")
        self.assertEqual(import_csv(path, self.apps).added, 1)

    def test_bad_rows_are_reported_by_line_and_skipped(self):
        path = self.csv("company,title,applied_at,stage\n"
                        "Globex,Platform Engineer,10/05/2026,\n"
                        "Initech,SWE,2026-09-02,hired\n"
                        ",SWE,2026-09-02,\n"
                        "Hooli,SWE,2026-09-03,\n")
        report = import_csv(path, self.apps)
        self.assertEqual(report.added, 1)
        self.assertEqual(len(report.problems), 3)
        self.assertTrue(report.problems[0].startswith("line 2:") and "2026-10-05" in report.problems[0])
        self.assertIn("line 3:", report.problems[1])
        self.assertIn("line 4:", report.problems[2])

    def test_a_link_in_jobs_db_links_the_job_and_keeps_the_csv_names(self):
        path = self.csv("company,title,url,applied_at\nAcme Inc,Backend Engineer II,"
                        "https://jobs.example.com/acme/1?utm_source=sheet,2026-09-01\n")
        import_csv(path, self.apps)
        app = self.apps.all()[0]
        self.assertEqual((app["job_id"], app["board"], app["company"], app["title"]),
                         ("dice_1", "dice", "Acme Inc", "Backend Engineer II"))

    def test_a_second_run_adds_nothing_and_dry_run_writes_nothing(self):
        path = self.csv("company,title,applied_at\nGlobex,Platform Engineer,2026-09-01\n"
                        "Globex,Platform Engineer,2026-09-01\n")
        self.assertEqual(import_csv(path, self.apps, dry_run=True).added, 1)
        self.assertEqual(self.apps.all(), [])
        first = import_csv(path, self.apps)
        self.assertEqual((first.added, first.duplicates), (1, 1))
        second = import_csv(path, self.apps)
        self.assertEqual((second.added, second.duplicates), (0, 2))

    def test_no_feedback_rows_are_written(self):
        path = self.csv("company,title,url,applied_at\nAcme,Backend Engineer,https://jobs.example.com/acme/1,"
                        "2026-09-01\n")
        import_csv(path, self.apps)
        self.assertEqual(self.store.latest_feedback(), [])

    def test_a_missing_required_column_stops_the_import(self):
        report = import_csv(self.csv("company,applied_at\nGlobex,2026-09-01\n"), self.apps)
        self.assertEqual(report.added, 0)
        self.assertIn("title", report.problems[0])
