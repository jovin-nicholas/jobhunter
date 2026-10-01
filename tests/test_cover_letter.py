import email
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jobhunter.cover_letter import build_prompt
from jobhunter.errors import SettingsError
from jobhunter.models import Job, Resume
from jobhunter.notify import Notifier
from jobhunter.pipeline import bootstrap, run
from jobhunter.store import Store
from tests.helpers import FakeResponse, write_project
from tests.test_notify import BOTH, ENV, JOB, RESULT
from tests.test_pipeline import BOARDS, SCORERS

WRITER = """
from jobhunter import ScoreResult, scorer


@scorer("writer")
class Writer:
    def __init__(self, options):
        self.fail = options.get("fail", False)

    def score(self, job, resume):
        return ScoreResult(score=9, model="writer")

    def generate(self, prompt):
        if self.fail:
            raise RuntimeError("model offline")
        return "Dear team, " + prompt.split("JOB: ")[1].split(chr(10))[0]
"""

SETTINGS = """
resumes: {folder: resumes, default: backend.txt}
search: {queries: [swe], locations: [Remote], fetch_descriptions: false}
boards: {fake: {}}
scorers:
  - writer: {WRITER_OPTIONS}
cover_letters: {enabled: true, writer: writer}
"""


class LetterNotifier:
    def __init__(self):
        self.sent = []

    def send(self, job, result, resume_id, cover_letter=None):
        self.sent.append((job.id, cover_letter))


class TestCoverLetters(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def run_with(self, writer_options="{}"):
        text = SETTINGS.replace("{WRITER_OPTIONS}", writer_options)
        path = write_project(self.tmp, text, plugins={"boards.py": BOARDS, "scorers.py": SCORERS, "writer.py": WRITER})
        app = bootstrap(path, env={})
        notifier = LetterNotifier()
        run(app, Store(self.tmp / "data" / "jobs.db"), notifier, log=lambda line: None)
        return dict(notifier.sent)

    def test_notified_jobs_get_a_letter(self):
        sent = self.run_with()
        self.assertEqual(sent["fake_0"], "Dear team, Great Engineer at Acme")

    def test_a_failing_writer_still_notifies_without_a_letter(self):
        sent = self.run_with("{fail: true}")
        self.assertIn("fake_0", sent)
        self.assertIsNone(sent["fake_0"])

    def test_a_scorer_that_cannot_write_is_a_settings_problem(self):
        text = SETTINGS.replace("  - writer: {WRITER_OPTIONS}\n", "  - fixed: {}\n").replace("writer: writer", "writer: fixed")
        path = write_project(self.tmp, text, plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        with self.assertRaises(SettingsError) as err:
            bootstrap(path, env={})
        self.assertIn("cover_letters.writer: fixed cannot write text", "\n".join(err.exception.problems))

    def test_prompt(self):
        job = Job("x", "Backend Engineer", "Acme", "Remote", "u", description="Go " * 2000)
        prompt = build_prompt(job, Resume("r.txt", "the candidate — Java, Spring."))
        self.assertIn("You are the candidate", prompt)
        self.assertIn("the candidate — Java, Spring.", prompt)
        self.assertIn("JOB: Backend Engineer at Acme", prompt)
        self.assertLess(prompt.count("Go"), 1100)


class TestNotifierLetter(unittest.TestCase):
    def test_letter_is_added_to_slack_and_email(self):
        smtp = MagicMock()
        with patch("jobhunter.notify.requests.post", return_value=FakeResponse()) as post, \
             patch("jobhunter.notify.smtplib.SMTP", return_value=smtp):
            Notifier(BOTH, env=ENV).send(JOB, RESULT, "backend.txt", cover_letter="Dear team, hello.")
        self.assertIn("Dear team, hello.", post.call_args.kwargs["json"]["text"])
        raw = smtp.__enter__.return_value.sendmail.call_args.args[2]
        body = email.message_from_string(raw).get_payload(decode=True).decode("utf-8")   # the body is base64
        self.assertIn("Dear team, hello.", body)
