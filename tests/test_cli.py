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
                  "ashby", "workday", "dover", "adp", "gem")

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

    def test_the_example_is_short_and_runs_all_twelve_boards(self):
        tmp, out = self.check(ROOT / "jobhunter.example.yaml", ["resume.pdf"])
        self.assertIn("12 board(s)", out)
        self.assertLessEqual(len((ROOT / "jobhunter.example.yaml").read_text().splitlines()), 40)
        _, out, _ = cli("--settings", str(tmp / "jobhunter.yaml"), "list-boards")
        for name in self.ALL_BOARDS:
            self.assertIn(name, out)

    def test_the_job_notifier_example_validates(self):
        _, out = self.check(ROOT / "examples" / "full.yaml", ["backend.txt", "fullstack.txt"])
        self.assertIn("12 board(s)", out)


class TestEnvExample(unittest.TestCase):
    def test_copying_env_example_does_not_set_a_placeholder_name(self):
        from dotenv import dotenv_values
        self.assertIsNone(dotenv_values(ROOT / ".env.example").get("CANDIDATE_NAMES"))


if __name__ == "__main__":
    unittest.main()
