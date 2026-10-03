import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobhunter.models import Job, Resume, ScoreResult
from jobhunter.ollama_check import memory_notes, ollama_problem
from jobhunter.errors import ScorerUnavailable
from jobhunter.scorers.chain import ScorerChain
from jobhunter.scorers.laya import LayaScorer
from jobhunter.settings import Decisions
from tests.helpers import FakeResponse, write_project
from tests.test_cli import cli
from tests.test_pipeline import BOARDS, SCORERS

TAGS = {"models": [{"name": "gemma4:e4b"}, {"name": "llama3:latest"}]}


class TestOllamaProblem(unittest.TestCase):
    def check(self, model, reply=None, error=None):
        with patch("jobhunter.ollama_check.requests.get", return_value=reply or FakeResponse(json_data=TAGS),
                   side_effect=error) as get:
            problem = ollama_problem("http://localhost:11434/", model)
        self.assertEqual(get.call_args.args[0], "http://localhost:11434/api/tags")
        return problem

    def test_a_pulled_model_is_fine(self):
        self.assertIsNone(self.check("gemma4:e4b"))
        self.assertIsNone(self.check("llama3"))                  # Ollama names an untagged model :latest

    def test_a_missing_model_says_how_to_pull_it(self):
        with patch("jobhunter.ollama_check.free_disk_gb", return_value=12.4):
            self.assertEqual(self.check("qwen3:8b"),
                             "qwen3:8b is not downloaded; run `ollama pull qwen3:8b` (12 GB of disk space is free)")

    def test_ollama_not_running(self):
        import requests
        problem = self.check("gemma4:e4b", error=requests.ConnectionError("refused"))
        self.assertIn("Ollama is not reachable at http://localhost:11434/", problem)
        self.assertIn("ollama serve", problem)


class TestMemoryNotes(unittest.TestCase):
    def test_models_that_fit_give_no_note(self):
        self.assertEqual(memory_notes({"gemma4:e4b": 9.6, "nimble:9b-q4_K_M": 5.6}, True, 25.8), [])

    def test_one_model_too_big(self):
        notes = memory_notes({"qwen3:32b": 20.0}, False, 16.0)
        self.assertEqual(len(notes), 1)
        self.assertIn("qwen3:32b needs about 20 GB of memory and this computer has 16 GB", notes[0])

    def test_models_that_do_not_fit_together(self):
        notes = memory_notes({"gemma4:e4b": 9.6, "nimble:9b-q4_K_M": 5.6}, True, 16.0)
        self.assertEqual(len(notes), 1)
        self.assertIn("gemma4:e4b, nimble:9b-q4_K_M and Laya together need about 17 GB", notes[0])

    def test_unknown_memory(self):
        self.assertEqual(memory_notes({"qwen3:32b": 20.0}, False, None), [])


class TestLayaCheck(unittest.TestCase):
    def test_a_missing_folder(self):
        with patch("jobhunter.scorers.laya.importlib.util.find_spec", return_value=object()):
            problem = LayaScorer({"model": "~/models/nowhere_to_be_found"}).check()
        self.assertIn("nowhere_to_be_found not found", problem)
        self.assertIn("convaiinnovations/laya", problem)

    def test_a_hugging_face_id_is_downloaded_on_first_use(self):
        with patch("jobhunter.scorers.laya.importlib.util.find_spec", return_value=object()):
            self.assertIsNone(LayaScorer({"model": "convaiinnovations/laya"}).check())

    def test_an_existing_folder(self):
        folder = tempfile.mkdtemp()
        with patch("jobhunter.scorers.laya.importlib.util.find_spec", return_value=object()):
            self.assertIsNone(LayaScorer({"model": folder}).check())

    def test_the_package_is_missing(self):
        with patch("jobhunter.scorers.laya.importlib.util.find_spec", return_value=None):
            self.assertIn("requirements-laya.txt", LayaScorer({"model": "convaiinnovations/laya"}).check())


class Missing:
    def score(self, job, resume):
        raise ScorerUnavailable("model folder not found")


class Works:
    def score(self, job, resume):
        return ScoreResult(score=8, model="ollama:gemma4:e4b")


class TestFallThroughNote(unittest.TestCase):
    def test_a_failing_scorer_is_noted_once_per_run(self):
        chain = ScorerChain([("laya", Missing()), ("ollama", Works())], Decisions(notify_at=7, log_at=5))
        job, resume = Job("1", "Engineer", "Acme", "Remote", "u"), Resume("r.txt", "text")
        first, second = chain.score(job, resume), chain.score(job, resume)
        self.assertEqual(first.first_failures, ["laya: unavailable (model folder not found)"])
        self.assertEqual(second.first_failures, [])
        self.assertEqual(second.errors, ["laya: unavailable (model folder not found)"])


CHECKED = SCORERS + '''

@scorer("checked")
class Checked:
    def __init__(self, options):
        self.problem = options.get("problem")

    def score(self, job, resume):
        return ScoreResult(score=8, model="checked")

    def check(self):
        return self.problem
'''

SETTINGS = """
resumes: {folder: resumes, default: backend.txt}
search: {queries: [software engineer], locations: [Remote], fetch_descriptions: false}
boards:
  fake: {}
scorers:
SCORERS_HERE
"""


class TestCheckConfigNotes(unittest.TestCase):
    def check_config(self, scorers):
        path = write_project(Path(tempfile.mkdtemp()), SETTINGS.replace("SCORERS_HERE", scorers),
                             plugins={"boards.py": BOARDS, "scorers.py": CHECKED})
        code, out, _ = cli("--settings", str(path), "check-config")
        self.assertEqual(code, 0)
        return out

    def test_a_scorer_that_cannot_run_yet_is_noted(self):
        out = self.check_config("  - checked: {problem: model not pulled}\n  - fixed: {}")
        self.assertIn("note: checked cannot judge jobs yet (scorers.checked): model not pulled. Until it is fixed, "
                      "fixed judges the jobs instead.", out)
        self.assertNotIn("no model can judge", out)

    def test_no_scorer_can_run(self):
        out = self.check_config("  - checked: {problem: model not pulled}")
        self.assertIn("note: checked cannot judge jobs yet (scorers.checked): model not pulled.\n", out)
        self.assertIn("no model can judge jobs right now", out)
        self.assertIn("no alerts are sent", out)
        self.assertIn("Fix one of the notes above", out)

    def test_ready_scorers_give_no_notes(self):
        self.assertNotIn("cannot judge", self.check_config("  - checked: {}"))
