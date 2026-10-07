import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path

from jobhunter.__main__ import main
from tests.helpers import RESUME_TEXT, make_pdf, write_project
from tests.test_pipeline import BOARDS, SCORERS

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = """
resumes: {folder: resumes, default: backend.txt}
search: {queries: [software engineer], locations: [Remote], fetch_descriptions: false}
boards:
  fake: {}
scorers:
  - fixed: {}
"""


def cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = write_project(self.tmp, SETTINGS, plugins={"boards.py": BOARDS, "scorers.py": SCORERS})

    def test_check_config_ok(self):
        code, out, _ = cli("--settings", str(self.path), "check-config")
        self.assertEqual(code, 0)
        self.assertIn("OK", out)
        self.assertIn("backend.txt", out)
        self.assertNotIn("no name found", out)

    def test_check_config_notes_a_resume_without_a_name(self):
        tmp = Path(tempfile.mkdtemp())
        path = write_project(tmp, SETTINGS, {"backend.txt": "Skills: Python, Go and SQL.\n" * 10},
                             plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        code, out, _ = cli("--settings", str(path), "check-config")
        self.assertEqual(code, 0)
        self.assertIn("no name found", out)
        self.assertIn("CANDIDATE_NAMES", out)

    def test_check_config_notes_hyde_park_read_twice(self):
        self.path.write_text(SETTINGS.replace("  fake: {}\n", "  fake: {}\n  hydepark: {}\n")
                             + "discovery: {github_readmes: [], getro: {collections: {112: Hyde Park}}}\n")
        code, out, _ = cli("--settings", str(self.path), "check-config")
        self.assertEqual(code, 0)
        self.assertIn("note: boards.hydepark", out)

    def test_check_config_lists_every_problem(self):
        self.path.write_text(SETTINGS.replace("fake: {}", "nope: {}").replace("fixed: {}", "ghost: {}"))
        code, _, err = cli("--settings", str(self.path), "check-config")
        self.assertEqual(code, 2)
        self.assertIn("boards.nope: unknown board", err)
        self.assertIn("scorers.ghost: unknown scorer", err)

    def test_list_boards_and_scorers(self):
        code, out, _ = cli("--settings", str(self.path), "list-boards")
        self.assertEqual(code, 0)
        for name in ("dice", "greenhouse", "fake"):
            self.assertIn(name, out)
        _, out, _ = cli("--settings", str(self.path), "list-scorers")
        for name in ("laya", "ollama", "fixed"):
            self.assertIn(name, out)

    def test_dry_run_stores_nothing(self):
        code, out, _ = cli("--settings", str(self.path), "run", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("[fake] found", out)
        with closing(sqlite3.connect(self.tmp / "data" / "jobs.db")) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_unknown_only_board_is_rejected(self):
        code, _, err = cli("--settings", str(self.path), "run", "--only", "fake,nope")
        self.assertEqual(code, 2)
        self.assertIn("nope", err)


class TestExampleSettings(unittest.TestCase):
    ALL_BOARDS = ("dice", "linkedin", "industry_jobs", "top_companies", "hydepark", "greenhouse", "lever",
                  "ashby", "workday", "dover", "adp", "gem", "vc_boards")     # list-boards shows opt-in hydepark too

    def check(self, example, resumes):
        tmp = Path(tempfile.mkdtemp())
        shutil.copy(example, tmp / "jobhunter.yaml")
        (tmp / "resumes").mkdir()
        for name in resumes:
            if name.endswith(".pdf"):
                make_pdf(tmp / "resumes" / name, RESUME_TEXT.replace("\u2014", "-").splitlines())   # Helvetica is Latin-1
            else:
                (tmp / "resumes" / name).write_text(RESUME_TEXT, encoding="utf-8")
        code, out, err = cli("--settings", str(tmp / "jobhunter.yaml"), "check-config")
        self.assertEqual(code, 0, err)
        return tmp, out

    def test_the_example_is_short_and_runs_the_twelve_default_boards(self):
        tmp, out = self.check(ROOT / "jobhunter.example.yaml", ["resume.pdf"])
        self.assertIn("12 board(s)", out)
        self.assertLessEqual(len((ROOT / "jobhunter.example.yaml").read_text().splitlines()), 40)
        _, out, _ = cli("--settings", str(tmp / "jobhunter.yaml"), "list-boards")
        for name in self.ALL_BOARDS:
            self.assertIn(name, out)

    def test_the_full_example_validates(self):
        _, out = self.check(ROOT / "examples" / "full.yaml", ["backend.txt", "fullstack.txt"])
        self.assertIn("13 board(s)", out)
        self.assertIn("vc_boards", out)


class TestEnvExample(unittest.TestCase):
    def test_copying_env_example_does_not_set_a_placeholder_name(self):
        from dotenv import dotenv_values
        self.assertIsNone(dotenv_values(ROOT / ".env.example").get("CANDIDATE_NAMES"))

class TestFeedbackCommands(unittest.TestCase):
    def setUp(self):
        from jobhunter.models import Job, ScoreResult
        from jobhunter.store import Store
        self.tmp = Path(tempfile.mkdtemp())
        self.path = write_project(self.tmp, SETTINGS, plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        self.store = Store(self.tmp / "data" / "jobs.db")
        self.store.save(Job("dice_1", "Backend Engineer", "Acme", "Austin", "https://e/1", source="dice"), "notified",
                        ScoreResult(score=8, model="fixed"), resume_id="backend.txt")

    def test_export_feedback_writes_label_columns(self):
        self.store.add_feedback([{"job_id": "dice_1", "verdict": "maybe", "received_at": "2026-10-05T10:00:00+00:00",
                                  "message_id": "<a>"}])
        out = self.tmp / "fb.csv"
        code, stdout, _ = cli("--settings", str(self.path), "export-feedback", "--out", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(out.read_text().splitlines(), [
            "job_id,job_title,company,decision,verdict,received_at",
            "dice_1,Backend Engineer,Acme,log,maybe,2026-10-05T10:00:00+00:00"])
        self.assertIn("wrote 1 verdict", stdout)

    def test_export_feedback_writes_to_the_data_folder_by_default(self):
        code, stdout, _ = cli("--settings", str(self.path), "export-feedback")
        self.assertEqual(code, 0)
        out = self.tmp / "data" / "feedback_labels.csv"
        self.assertTrue(out.exists())
        self.assertIn(str(out), stdout)

    def test_the_gitignore_covers_personal_training_files(self):
        lines = (ROOT / ".gitignore").read_text().splitlines()
        for pattern in ("feedback_labels.csv", "/scoring_resume.txt", "pool_*.jsonl", "heldout_ids.txt", "upload_*/"):
            self.assertIn(pattern, lines)

    def test_export_feedback_rejects_a_bad_date(self):
        code, _, err = cli("--settings", str(self.path), "export-feedback", "--since", "yesterday")
        self.assertEqual(code, 2)
        self.assertIn("YYYY-MM-DD", err)

    def test_send_test_alert_needs_email(self):
        code, _, err = cli("--settings", str(self.path), "send-test-alert")
        self.assertEqual(code, 2)
        self.assertIn("notify.email", err)

    def test_send_test_alert_sends_the_last_notified_job(self):
        from unittest.mock import patch
        self.path.write_text(SETTINGS + "notify:\n  email: {from_env: GMAIL_ADDRESS, password_env: GMAIL_APP_PASSWORD}\n")
        with patch.dict("os.environ", {"GMAIL_ADDRESS": "sender@example.com", "GMAIL_APP_PASSWORD": "x"}), \
             patch("jobhunter.__main__.Notifier") as notifier:
            notifier.return_value.send.return_value = True
            code, out, err = cli("--settings", str(self.path), "send-test-alert")
        self.assertEqual(code, 0, err)
        job, result, resume_id = notifier.return_value.send.call_args.args
        self.assertEqual((job.id, result.score, resume_id), ("dice_1", 8, "backend.txt"))
        self.assertIsNone(notifier.call_args.args[0].slack)          # email only
        self.assertIn("sent a test alert", out)

    def test_run_never_opens_imap_on_a_dry_run(self):
        from unittest.mock import patch
        self.path.write_text(SETTINGS + "notify:\n  email: {from_env: GMAIL_ADDRESS, password_env: GMAIL_APP_PASSWORD}\n")
        with patch.dict("os.environ", {"GMAIL_ADDRESS": "sender@example.com", "GMAIL_APP_PASSWORD": "x"}), \
             patch("jobhunter.inbox.imaplib.IMAP4_SSL") as imap:
            code, _, _ = cli("--settings", str(self.path), "run", "--dry-run")
        self.assertEqual(code, 0)
        imap.assert_not_called()



class TestFollowUpEmail(unittest.TestCase):
    EMAIL_SETTINGS = SETTINGS + ("notify:\n  email: {from_env: GMAIL_ADDRESS, password_env: GMAIL_APP_PASSWORD, "
                                 "to_env: NOTIFY_EMAIL}\n"
                                 "tracker: {follow_up_email: true, digest_at: \"00:00\"}\n")

    def setUp(self):
        from datetime import date, timedelta
        from jobhunter.store import Store
        from jobhunter.tracker.records import Applications
        self.tmp = Path(tempfile.mkdtemp())
        self.path = write_project(self.tmp, self.EMAIL_SETTINGS, plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        (self.tmp / ".env").write_text("GMAIL_ADDRESS=sender@example.com\nGMAIL_APP_PASSWORD=app-pass\n"
                                       "NOTIFY_EMAIL=john.doe@example.com\n")
        applied = (date.today() - timedelta(days=7)).isoformat()            # its default follow-up is due today
        Applications(Store(self.tmp / "data" / "jobs.db")).add(title="Engineer", company="Acme", applied_at=applied)

    def recipients(self, smtp):
        return [c.args[1] for c in smtp.return_value.__enter__.return_value.sendmail.call_args_list]

    def test_send_test_digest_sends_to_the_followup_address(self):
        from unittest.mock import patch
        with patch("jobhunter.notify.smtplib.SMTP") as smtp:
            code, out, _ = cli("--settings", str(self.path), "send-test-digest")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.recipients(smtp), ["john.doe+followup@example.com"])
        self.assertIn("john.doe+followup@example.com", out)

    def test_send_test_digest_with_nothing_due_sends_nothing(self):
        from unittest.mock import patch
        from jobhunter.store import Store
        from jobhunter.tracker.records import Applications
        apps = Applications(Store(self.tmp / "data" / "jobs.db"))
        apps.delete(apps.all()[0]["id"])
        with patch("jobhunter.notify.smtplib.SMTP") as smtp:
            code, out, _ = cli("--settings", str(self.path), "send-test-digest")
        self.assertEqual((code, self.recipients(smtp)), (0, []))
        self.assertIn("nothing is due today", out)

    def test_a_run_sends_the_follow_up_email_once(self):
        from unittest.mock import patch
        with patch("jobhunter.notify.smtplib.SMTP") as smtp, patch("jobhunter.notify.requests.post"):
            cli("--settings", str(self.path), "run")
            cli("--settings", str(self.path), "run")
        self.assertEqual(self.recipients(smtp).count("john.doe+followup@example.com"), 1)

    def test_a_dry_run_never_sends_it(self):
        from unittest.mock import patch
        with patch("jobhunter.notify.smtplib.SMTP") as smtp, patch("jobhunter.notify.requests.post"):
            cli("--settings", str(self.path), "run", "--dry-run")
        self.assertNotIn("john.doe+followup@example.com", self.recipients(smtp))


class TestTrackerCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = write_project(self.tmp, SETTINGS, plugins={"boards.py": BOARDS, "scorers.py": SCORERS})

    def test_tracker_import_reports_and_dry_run_writes_nothing(self):
        sheet = self.tmp / "old.csv"
        sheet.write_text("company,title,applied_at\nGlobex,Platform Engineer,2026-09-01\nInitech,SWE,9/2/2026\n")
        code, out, _ = cli("--settings", str(self.path), "tracker-import", str(sheet), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would add 1 application(s)", out)
        self.assertIn("line 3:", out)
        code, out, _ = cli("--settings", str(self.path), "tracker-import", str(sheet))
        self.assertIn("added 1 application(s)", out)
        code, out, _ = cli("--settings", str(self.path), "tracker-import", str(sheet))
        self.assertIn("added 0 application(s), 1 already tracked", out)

    def test_tracker_import_of_a_missing_file(self):
        code, _, err = cli("--settings", str(self.path), "tracker-import", str(self.tmp / "nope.csv"))
        self.assertEqual(code, 2)
        self.assertIn("nope.csv", err)

    def test_tracker_serves_until_interrupted(self):
        from unittest.mock import patch
        with patch("jobhunter.tracker.server.TrackerServer.serve_forever", side_effect=KeyboardInterrupt), \
             patch("webbrowser.open") as browser:
            code, out, _ = cli("--settings", str(self.path), "tracker", "--port", "0")
        self.assertEqual(code, 0)
        self.assertIn("http://127.0.0.1:", out)
        browser.assert_called_once()


if __name__ == "__main__":
    unittest.main()
