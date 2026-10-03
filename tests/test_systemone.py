import tempfile
import unittest
from pathlib import Path

import requests

from jobhunter.errors import SettingsError
from jobhunter.filters import build_filters
from jobhunter.filters.location import LocationFilter
from jobhunter.filters.systemone import SystemOneFilter
from jobhunter.models import Job
from jobhunter.settings import (FilterSettings, LocationFilterSettings, SeniorityFilterSettings, SystemOneSettings,
                                load_settings)
from jobhunter.systemone import SystemOneClient
from tests.test_settings import VALID
from tests.helpers import write_project

RESUMES = {"backend.txt": "John Doe\nBackend engineer. Python, Go.\n" * 5,
           "fullstack.txt": "John Doe\nFull-stack engineer. React, Node.\n" * 5}


def job(title="Software Engineer", location="Austin, TX", description="Build services in Python."):
    return Job("t_1", title, "Acme", location, "https://example.com/1", description=description)


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


class FakeOllama:
    """Answers /v1/systemone with a fixed noul per question name; records every request."""

    def __init__(self, answers=None, fail=None):
        self.answers, self.fail, self.calls = answers or {}, fail, []

    def __call__(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        if self.fail is not None:
            raise self.fail
        (name,) = json["questions"]
        return FakeResponse(200, {"answers": {name: {"noul": self.answers.get(name, 0.9)}}})


def client(fake, **kw):
    return SystemOneClient(SystemOneSettings(model="nimble", **kw), post=fake)


class TestSettings(unittest.TestCase):
    def load(self, filters_extra):
        tmp = Path(tempfile.mkdtemp()).resolve()
        text = VALID.replace("filters:\n", "filters:\n" + filters_extra)
        return load_settings(write_project(tmp, text, RESUMES), env={"SLACK_WEBHOOK_URL": "x"})

    def test_off_unless_set_and_only_the_model_is_needed(self):
        self.assertIsNone(self.load("").filters.systemone)
        s = self.load("  systemone: {model: \"nimble:9b-q4_K_M\"}\n").filters.systemone
        self.assertEqual((s.model, s.url, s.timeout_s), ("nimble:9b-q4_K_M", "http://localhost:11434", 30))

    def test_problems_are_reported(self):
        with self.assertRaises(SettingsError) as err:
            self.load("  systemone: {url: http://x, timeout_s: 0, color: red}\n")
        text = "\n".join(err.exception.problems)
        for expected in ("filters.systemone.model", "filters.systemone.timeout_s", "color"):
            self.assertIn(expected, text)


class TestClient(unittest.TestCase):
    def test_one_question_per_request_to_the_systemone_endpoint(self):
        fake = FakeOllama({"q": 0.25})
        value = client(fake, timeout_s=12).noul("q", {"job": {"title": "x"}}, {"type": "noul", "instructions": "?"})
        self.assertEqual(value, 0.25)
        url, body, timeout = fake.calls[0]
        self.assertEqual(url, "http://localhost:11434/v1/systemone")
        self.assertEqual((body["model"], list(body["questions"]), body["state"], timeout),
                         ("nimble", ["q"], {"job": {"title": "x"}}, 12))
        self.assertIn("keep_alive", body)

    def test_identical_requests_are_answered_from_the_cache(self):
        fake, question = FakeOllama(), {"type": "noul", "instructions": "?"}
        c = client(fake)
        c.noul("q", {"a": 1}, question)
        c.noul("q", {"a": 1}, question)
        c.noul("q", {"a": 2}, question)
        self.assertEqual(len(fake.calls), 2)

    def test_failures_return_none_and_three_in_a_row_stop_asking(self):
        fake = FakeOllama(fail=requests.ConnectionError("refused"))
        c = client(fake)
        for i in range(5):
            self.assertIsNone(c.noul("q", {"i": i}, {"type": "noul", "instructions": "?"}))
        self.assertEqual(len(fake.calls), 3)
        self.assertIn("refused", c.report())

    def test_an_unexpected_reply_is_a_failure(self):
        for reply in (FakeResponse(404, "model not found"), FakeResponse(200, {"answers": {}}),
                      FakeResponse(200, {"answers": {"q": {"noul": "yes"}}})):
            c = client(lambda url, json, timeout, r=reply: r)
            self.assertIsNone(c.noul("q", {}, {"type": "noul", "instructions": "?"}))
        self.assertIsNone(client(FakeOllama()).report())


class TestSystemOneFilter(unittest.TestCase):
    def test_a_job_that_is_not_a_software_role_is_skipped(self):
        f = SystemOneFilter(client(FakeOllama({"software_role": 0.1})), ask_students=False)
        result = f.check(job(title="Fixed Income Intern"))
        self.assertFalse(result.keep)
        self.assertIn("not a software engineering role", result.reason)
        self.assertTrue(SystemOneFilter(client(FakeOllama({"software_role": 0.9})), False).check(job()).keep)

    def test_the_students_question_is_asked_only_when_internships_are_not_wanted(self):
        fake = FakeOllama({"software_role": 0.9, "students_only": 0.95})
        self.assertTrue(SystemOneFilter(client(fake), ask_students=False).check(job()).keep)
        self.assertEqual([list(b["questions"]) for _, b, _ in fake.calls], [["software_role"]])
        result = SystemOneFilter(client(fake), ask_students=True).check(job(title="Software Engineering Intern"))
        self.assertFalse(result.keep)
        self.assertIn("only for current students", result.reason)

    def test_the_state_holds_only_the_job(self):
        fake = FakeOllama()
        SystemOneFilter(client(fake), ask_students=True).check(job(description="x" * 5000))
        for _, body, _ in fake.calls:
            self.assertEqual(set(body["state"]), {"job"})
            self.assertLessEqual(len(str(body["state"])), 2000)

    def test_an_unavailable_model_keeps_the_job(self):
        f = SystemOneFilter(client(FakeOllama(fail=requests.Timeout("slow"))), ask_students=True)
        self.assertTrue(f.check(job(title="Fixed Income Intern")).keep)


class TestLocationEscalation(unittest.TestCase):
    def location(self, answer):
        fake = FakeOllama({"in_allowed_country": answer})
        return LocationFilter(LocationFilterSettings(["US"]), client(fake)), fake

    def test_only_undecided_locations_are_asked(self):
        f, fake = self.location(0.9)
        self.assertTrue(f.check(job(location="Austin, TX")).keep)
        self.assertFalse(f.check(job(location="Toronto, ON")).keep)
        self.assertEqual(fake.calls, [])
        self.assertTrue(f.check(job(location="In-Office", description="Join the team.")).keep)
        self.assertTrue(f.check(job(location="Cajamarca, Peru")).keep)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("United States", fake.calls[0][1]["questions"]["in_allowed_country"]["instructions"])

    def test_the_answer_decides(self):
        f, _ = self.location(0.1)
        result = f.check(job(location="In-Office", description="Join the team."))
        self.assertFalse(result.keep)
        self.assertIn("no place named", result.reason)
        self.assertIn("model", result.reason)

    def test_without_a_model_or_when_it_fails_the_parser_decides(self):
        self.assertFalse(LocationFilter(LocationFilterSettings(["US"])).check(job(location="In-Office")).keep)
        f = LocationFilter(LocationFilterSettings(["US"]), client(FakeOllama(fail=requests.Timeout("slow"))))
        self.assertFalse(f.check(job(location="Cajamarca, Peru")).keep)


class TestWiring(unittest.TestCase):
    def test_build_filters_adds_the_checker_last_and_gives_location_the_model(self):
        filters = build_filters(FilterSettings(location=LocationFilterSettings(["US"]),
                                               seniority=SeniorityFilterSettings(["entry", "mid"]),
                                               systemone=SystemOneSettings(model="nimble")))
        self.assertEqual([f.name for f in filters], ["location", "seniority", "systemone"])
        self.assertIsNotNone(filters[0].model)
        self.assertTrue(filters[-1].ask_students)
        with_interns = build_filters(FilterSettings(seniority=SeniorityFilterSettings(["intern", "entry"]),
                                                    systemone=SystemOneSettings(model="nimble")))
        self.assertFalse(with_interns[-1].ask_students)
        self.assertEqual([f.name for f in build_filters(FilterSettings())], [])


if __name__ == "__main__":
    unittest.main()


class TestRunLog(unittest.TestCase):
    def test_a_model_that_did_not_answer_is_noted_once_in_the_run_log(self):
        from tests.test_pipeline import PipelineTestCase

        class Case(PipelineTestCase):
            def runTest(self):
                pass

        case = Case()
        case.setUp()
        failing = client(FakeOllama(fail=requests.ConnectionError("refused")))
        failing.noul("q", {}, {"type": "noul", "instructions": "?"})
        case.app.filters += [SystemOneFilter(failing, ask_students=False),
                             LocationFilter(LocationFilterSettings(["US"]), failing)]
        case.run_once()
        notes = [line for line in case.logs if line.startswith("systemone:")]
        self.assertEqual(len(notes), 1)
        self.assertIn("refused", notes[0])


class TestRequirementChecks(unittest.TestCase):
    def test_skip_if_parses_and_rejects_unknown_checks(self):
        s = TestSettings().load('  systemone: {model: nimble, skip_if: [citizenship, clearance]}\n').filters.systemone
        self.assertEqual(s.skip_if, ["citizenship", "clearance"])
        self.assertEqual(TestSettings().load("  systemone: {model: nimble}\n").filters.systemone.skip_if, [])
        with self.assertRaises(SettingsError) as err:
            TestSettings().load("  systemone: {model: nimble, skip_if: [citizenship, pets]}\n")
        self.assertIn("filters.systemone.skip_if", "\n".join(err.exception.problems))

    def test_a_required_citizenship_or_clearance_skips_the_job(self):
        fake = FakeOllama({"software_role": 0.9, "requires_citizenship": 0.95, "requires_clearance": 0.1})
        f = SystemOneFilter(client(fake), ask_students=False, skip_if=["citizenship", "clearance"], country="the United States")
        result = f.check(job(description="Requirements\nMust be a U.S. citizen.\n3 years of Python."))
        self.assertFalse(result.keep)
        self.assertIn("requires citizenship", result.reason)
        asked = [list(b["questions"])[0] for _, b, _ in fake.calls]
        self.assertEqual(asked, ["software_role", "requires_citizenship"])        # stops at the first skip
        question = fake.calls[1][1]["questions"]["requires_citizenship"]
        self.assertIn("the United States", question["instructions"])
        self.assertIn("Must be a U.S. citizen", fake.calls[1][1]["state"]["job"]["requirements"])

    def test_boilerplate_is_left_to_the_model_and_failures_keep_the_job(self):
        fake = FakeOllama({"software_role": 0.9, "requires_citizenship": 0.05, "requires_clearance": 0.02})
        f = SystemOneFilter(client(fake), ask_students=False, skip_if=["citizenship", "clearance"], country="the United States")
        self.assertTrue(f.check(job(description="All qualified applicants, including U.S. citizens, are welcome.")).keep)
        down = SystemOneFilter(client(FakeOllama(fail=requests.Timeout("slow"))), ask_students=False,
                               skip_if=["citizenship"], country="the United States")
        self.assertTrue(down.check(job()).keep)

    def test_build_filters_passes_the_allowed_country(self):
        filters = build_filters(FilterSettings(location=LocationFilterSettings(["US"]),
                                               systemone=SystemOneSettings(model="nimble", skip_if=["citizenship"])))
        self.assertEqual((filters[-1].skip_if, filters[-1].country), (["citizenship"], "United States"))
