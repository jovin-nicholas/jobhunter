import unittest
from unittest.mock import patch

import requests

from jobhunter.errors import RateLimited, ScorerError
from jobhunter.models import Job, Resume, ScoreResult
from jobhunter.scorers.chain import ScorerChain
from jobhunter.scorers.ollama import OllamaScorer
from jobhunter.scorers.prompt import build_prompt, parse_json, to_result
from jobhunter.scorers.skills import compare_skills
from jobhunter.settings import Decisions
from tests.helpers import FakeResponse

JOB = Job("t_1", "Backend Engineer", "Acme", "Austin, TX", "https://example.com/1",
          description="Java, Spring Boot and Kubernetes services.")
RESUME = Resume("backend.txt", "the candidate — SDE. Java, Spring Boot, Docker.")
GOOD = '{"match_score": 8, "reasoning": "Strong Java fit.", "matched_skills": ["java"], "keyword_gaps": ["kubernetes"], "role_type": "backend"}'


class Fixed:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, 0

    def score(self, job, resume):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return ScoreResult(score=self.outcome, model="fixed")


class TestChain(unittest.TestCase):
    def test_first_success_wins_and_decision_comes_from_thresholds(self):
        first, second = Fixed(ScorerError("down")), Fixed(6)
        outcome = ScorerChain([("a", first), ("b", second), ("c", Fixed(9))], Decisions(7, 5)).score(JOB, RESUME)
        self.assertEqual((outcome.status, outcome.result.score, outcome.result.decision), ("scored", 6, "log"))
        self.assertEqual(outcome.errors, ["a: down"])

    def test_all_failed_with_a_rate_limit_is_retried_later(self):
        outcome = ScorerChain([("a", Fixed(RateLimited("429"))), ("b", Fixed(ScorerError("x")))],
                              Decisions()).score(JOB, RESUME)
        self.assertEqual((outcome.result, outcome.status), (None, "error_429_retry"))

    def test_all_failed_otherwise_is_terminal(self):
        outcome = ScorerChain([("a", Fixed(ScorerError("x")))], Decisions()).score(JOB, RESUME)
        self.assertEqual(outcome.status, "error_terminal")

    def test_an_unexpected_error_is_retried_not_final(self):
        # A bug or a bad option (timeout_s: "240") is not the job's fault; the job is tried again.
        outcome = ScorerChain([("a", Fixed(ValueError("Timeout value connect was 240")))], Decisions()).score(JOB,
                                                                                                           RESUME)
        self.assertEqual(outcome.status, "error_unavailable")


class Confident:
    def __init__(self, score, confidence):
        self.score_, self.confidence = score, confidence

    def score(self, job, resume):
        return ScoreResult(score=self.score_, model="m", confidence=self.confidence)


class TestMinConfidence(unittest.TestCase):
    def decide(self, score, confidence, min_confidence):
        chain = ScorerChain([("m", Confident(score, confidence))], Decisions(8, 5, min_confidence))
        return chain.score(JOB, RESUME).result.decision

    def test_an_unsure_notify_is_logged(self):
        self.assertEqual(self.decide(8, 0.62, 0.7), "log")
        self.assertEqual(self.decide(8, 0.70, 0.7), "notify")
        self.assertEqual(self.decide(6, 0.95, 0.7), "log")

    def test_off_by_default_and_scorers_without_confidence_are_not_affected(self):
        self.assertEqual(self.decide(8, 0.1, None), "notify")
        self.assertEqual(self.decide(9, None, 0.7), "notify")


class TestPrompt(unittest.TestCase):
    def test_description_is_truncated_and_resume_included(self):
        long_job = Job("t", "T", "C", "L", "u", description="x" * 30000)
        prompt = build_prompt(long_job, RESUME, max_description_chars=2000)
        self.assertIn("the candidate — SDE", prompt)
        self.assertEqual(prompt.count("x" * 2000), 1)
        self.assertNotIn("x" * 2001, prompt)

    def test_json_is_found_inside_surrounding_text(self):
        self.assertEqual(parse_json("Sure! ```json\n" + GOOD + "\n```")["match_score"], 8)
        with self.assertRaises(ScorerError):
            parse_json("no json here")

    def test_result_validation(self):
        result = to_result({"match_score": "7", "matched_skills": ["java", 3]}, "m")
        self.assertEqual((result.score, result.matched_skills), (7, ["java"]))
        for bad in ({"match_score": 11}, {"match_score": "high"}, {}):
            with self.subTest(bad=bad), self.assertRaises(ScorerError):
                to_result(bad, "m")


class TestSkills(unittest.TestCase):
    def test_matched_and_gaps_are_whole_word(self):
        matched, gaps = compare_skills("Java, JavaScript and Kubernetes (k8s)", "Java and JavaScript")
        self.assertEqual(matched, ["javascript", "java"])
        self.assertEqual(gaps, ["kubernetes"])


class TestOllama(unittest.TestCase):
    def scorer(self):
        return OllamaScorer({"model": "gemma4:e4b"})

    def test_good_reply(self):
        with patch("jobhunter.scorers.ollama.requests.post", return_value=FakeResponse(json_data={"response": GOOD})) as post:
            result = self.scorer().score(JOB, RESUME)
        self.assertEqual((result.score, result.model, result.role_type), (8, "ollama:gemma4:e4b", "backend"))
        self.assertEqual(post.call_args.kwargs["json"]["format"], "json")
        self.assertEqual(post.call_args.args[0], "http://localhost:11434/api/generate")

    def test_bad_json_is_retried_once_with_a_stricter_prompt(self):
        replies = [FakeResponse(json_data={"response": "I think it's an 8"}), FakeResponse(json_data={"response": GOOD})]
        with patch("jobhunter.scorers.ollama.requests.post", side_effect=replies) as post:
            self.assertEqual(self.scorer().score(JOB, RESUME).score, 8)
        self.assertTrue(post.call_args.kwargs["json"]["prompt"].startswith("CRITICAL"))

    def test_rate_limit_and_unreachable(self):
        with patch("jobhunter.scorers.ollama.requests.post", return_value=FakeResponse(status_code=429, text="slow down")):
            with self.assertRaises(RateLimited):
                self.scorer().score(JOB, RESUME)
        with patch("jobhunter.scorers.ollama.requests.post", side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(ScorerError) as caught:
                self.scorer().score(JOB, RESUME)
        self.assertIn("not reachable", str(caught.exception))

    def test_thinking_off_and_temperature_zero_by_default(self):
        with patch("jobhunter.scorers.ollama.requests.post", return_value=FakeResponse(json_data={"response": GOOD})) as post:
            self.scorer().score(JOB, RESUME)
        body = post.call_args.kwargs["json"]
        self.assertIs(body["think"], False)
        self.assertEqual(body["options"], {"temperature": 0.0})

    def test_thinking_temperature_and_context_are_settings(self):
        scorer = OllamaScorer({"model": "m", "think": True, "temperature": 0.7, "num_ctx": 8192})
        with patch("jobhunter.scorers.ollama.requests.post", return_value=FakeResponse(json_data={"response": GOOD})) as post:
            scorer.score(JOB, RESUME)
        body = post.call_args.kwargs["json"]
        self.assertEqual((body["think"], body["options"]), (True, {"temperature": 0.7, "num_ctx": 8192}))

    def test_generate_writes_plain_text_with_the_scorers_settings(self):
        # Letter defaults (thinking on) come from cover_letters, which builds its own writer; see test_cover_letter.
        with patch("jobhunter.scorers.ollama.requests.post", return_value=FakeResponse(json_data={"response": "Dear team"})) as post:
            self.scorer().generate("Write a letter")
        self.assertNotIn("format", post.call_args.kwargs["json"])
        self.assertIs(post.call_args.kwargs["json"]["think"], False)

    def test_a_model_that_cannot_think_is_asked_again_without_thinking(self):
        scorer = OllamaScorer({"model": "llama3", "think": True})
        refused = FakeResponse(status_code=400, text='{"error":"\\"llama3\\" does not support thinking"}')
        replies = [refused, FakeResponse(json_data={"response": "Dear team"}), FakeResponse(json_data={"response": "Again"})]
        with patch("jobhunter.scorers.ollama.requests.post", side_effect=replies) as post:
            self.assertEqual(scorer.generate("Write a letter"), "Dear team")
            self.assertEqual(scorer.generate("Write another"), "Again")
        bodies = [c.kwargs["json"] for c in post.call_args_list]
        self.assertEqual(["think" in b for b in bodies], [True, False, False])   # remembered: asked without it next time

    def test_other_bad_requests_are_errors(self):
        with patch("jobhunter.scorers.ollama.requests.post", return_value=FakeResponse(status_code=400, text="bad prompt")):
            with self.assertRaises(ScorerError):
                OllamaScorer({"model": "m", "think": True}).generate("x")

    def test_inline_thinking_is_removed(self):
        reply = FakeResponse(json_data={"response": "<think>\nThe user wants a letter.\n</think>\n\nDear team"})
        with patch("jobhunter.scorers.ollama.requests.post", return_value=reply):
            self.assertEqual(self.scorer().generate("Write a letter"), "Dear team")



class TestReviewFixes(unittest.TestCase):
    def test_an_unavailable_scorer_is_retried_on_the_next_run(self):
        from jobhunter.errors import ScorerUnavailable
        outcome = ScorerChain([("laya", Fixed(ScorerUnavailable("not installed")))], Decisions()).score(JOB, RESUME)
        self.assertEqual(outcome.status, "error_unavailable")

    def test_an_unexpected_exception_falls_through_to_the_next_scorer(self):
        outcome = ScorerChain([("a", Fixed(KeyError("match_score"))), ("b", Fixed(8))], Decisions()).score(JOB, RESUME)
        self.assertEqual((outcome.status, outcome.result.score), ("scored", 8))
        self.assertIn("KeyError", outcome.errors[0])

    def test_unreachable_ollama_is_unavailable_and_not_retried(self):
        from jobhunter.errors import ScorerUnavailable
        with patch("jobhunter.scorers.ollama.requests.post", side_effect=requests.ConnectionError("refused")) as post:
            with self.assertRaises(ScorerUnavailable):
                OllamaScorer({"model": "m"}).score(JOB, RESUME)
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
