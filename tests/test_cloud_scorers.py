import unittest
from unittest.mock import patch

import requests

from jobhunter.errors import RateLimited, ScorerError, ScorerUnavailable
from jobhunter.scorers.cloud import GeminiScorer, GroqScorer
from jobhunter.scorers.ollama import OllamaScorer
from tests.helpers import FakeResponse
from tests.test_scorers import GOOD, JOB, RESUME


def gemini_reply(text):
    return FakeResponse(json_data={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def groq_reply(text):
    return FakeResponse(json_data={"choices": [{"message": {"content": text}}]})


FAST = {"min_interval_s": 0}


class TestGemini(unittest.TestCase):
    def test_scores_with_the_key_in_a_header(self):
        with patch("jobhunter.scorers.cloud.requests.post", return_value=gemini_reply(GOOD)) as post:
            result = GeminiScorer(FAST, env={"GEMINI_API_KEYS": "k1"}).score(JOB, RESUME)
        self.assertEqual((result.score, result.model), (8, "gemini:gemini-3.5-flash-lite"))
        url, kw = post.call_args.args[0], post.call_args.kwargs
        self.assertNotIn("k1", url)
        self.assertEqual(kw["headers"], {"x-goog-api-key": "k1"})
        self.assertEqual(kw["json"]["generationConfig"], {"response_mime_type": "application/json"})
        self.assertIn("Backend Engineer", kw["json"]["contents"][0]["parts"][0]["text"])

    def test_rate_limited_key_hands_over_and_is_remembered(self):
        replies = [FakeResponse(status_code=429, text="slow"), gemini_reply(GOOD), gemini_reply(GOOD)]
        scorer = GeminiScorer(FAST, env={"GEMINI_API_KEYS": "k1, k2"})
        with patch("jobhunter.scorers.cloud.requests.post", side_effect=replies) as post:
            scorer.score(JOB, RESUME)
            scorer.score(JOB, RESUME)
        self.assertEqual([c.kwargs["headers"]["x-goog-api-key"] for c in post.call_args_list], ["k1", "k2", "k2"])

    def test_every_key_limited_raises_at_once(self):
        with patch("jobhunter.scorers.cloud.requests.post", return_value=FakeResponse(status_code=429, text="")) as post, \
             patch("jobhunter.scorers.cloud.time.sleep") as sleep:
            with self.assertRaises(RateLimited):
                GeminiScorer(FAST, env={"GEMINI_API_KEYS": "k1,k2,k3"}).score(JOB, RESUME)
        self.assertEqual(post.call_count, 3)
        sleep.assert_not_called()

    def test_network_errors_never_include_the_key(self):
        error = requests.ConnectionError("https://x?key=SECRET refused")
        with patch("jobhunter.scorers.cloud.requests.post", side_effect=error):
            with self.assertRaises(ScorerUnavailable) as caught:
                GeminiScorer(FAST, env={"GEMINI_API_KEYS": "SECRET"}).score(JOB, RESUME)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_missing_key_stops_construction(self):
        with self.assertRaises(ScorerUnavailable):
            GeminiScorer({}, env={})

    def test_errors_map_to_chain_outcomes(self):
        cases = [(FakeResponse(status_code=503, text="down"), ScorerUnavailable),
                 (FakeResponse(status_code=400, text="bad request"), ScorerError),
                 (FakeResponse(json_data={"candidates": []}), ScorerError),
                 (gemini_reply("not json"), ScorerError)]
        for reply, error in cases:
            with patch("jobhunter.scorers.cloud.requests.post", return_value=reply):
                with self.assertRaises(error):
                    GeminiScorer(FAST, env={"GEMINI_API_KEYS": "k"}).score(JOB, RESUME)


class TestGroq(unittest.TestCase):
    def test_scores_and_generates(self):
        with patch("jobhunter.scorers.cloud.requests.post", side_effect=[groq_reply(GOOD), groq_reply(" Dear team ")]) as post:
            scorer = GroqScorer(FAST, env={"GROQ_API_KEYS": "g1"})
            self.assertEqual(scorer.score(JOB, RESUME).score, 8)
            self.assertEqual(scorer.generate("write"), "Dear team")
        first, second = post.call_args_list
        self.assertEqual(first.kwargs["headers"], {"Authorization": "Bearer g1"})
        self.assertEqual(first.kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", second.kwargs["json"])


class TestOllamaGenerate(unittest.TestCase):
    def test_plain_text_without_json_format(self):
        with patch("jobhunter.scorers.ollama.requests.post",
                   return_value=FakeResponse(json_data={"response": " Letter "})) as post:
            self.assertEqual(OllamaScorer({"model": "gemma4:e4b"}).generate("write"), "Letter")
        self.assertNotIn("format", post.call_args.kwargs["json"])


def limited(**headers):
    return FakeResponse(status_code=429, text="slow down", headers=headers)


class TestCooldown(unittest.TestCase):
    def scorer(self, keys="k1,k2"):
        scorer, now = GeminiScorer(FAST, env={"GEMINI_API_KEYS": keys}), [0.0]
        scorer.keys.clock = lambda: now[0]
        return scorer, now

    def keys_used(self, post):
        return [c.kwargs["headers"]["x-goog-api-key"] for c in post.call_args_list]

    def test_a_rate_limited_key_rests_while_the_others_work(self):
        scorer, now = self.scorer()
        with patch("jobhunter.scorers.cloud.requests.post", side_effect=[limited(), gemini_reply(GOOD),
                                                                          gemini_reply(GOOD)]) as post:
            scorer.score(JOB, RESUME)
            now[0] = 30
            scorer.score(JOB, RESUME)
        self.assertEqual(self.keys_used(post), ["k1", "k2", "k2"])

    def test_when_every_key_rests_no_request_is_made(self):
        scorer, now = self.scorer()
        with patch("jobhunter.scorers.cloud.requests.post", side_effect=[limited(), limited()]) as post:
            with self.assertRaises(RateLimited):
                scorer.score(JOB, RESUME)
            now[0] = 30
            with self.assertRaises(RateLimited) as caught:
                scorer.score(JOB, RESUME)
        self.assertEqual(post.call_count, 2)
        self.assertIn("resting", str(caught.exception))

    def test_retry_after_sets_the_rest(self):
        scorer, now = self.scorer("k1")
        with patch("jobhunter.scorers.cloud.requests.post",
                   side_effect=[limited(**{"Retry-After": "5"}), gemini_reply(GOOD)]) as post:
            with self.assertRaises(RateLimited):
                scorer.score(JOB, RESUME)
            now[0] = 6
            self.assertEqual(scorer.score(JOB, RESUME).score, 8)
        self.assertEqual(post.call_count, 2)

    def test_the_rest_doubles_and_resets_after_a_success(self):
        scorer, now = self.scorer("k1")
        answers = [limited(), limited(), gemini_reply(GOOD), limited()]
        with patch("jobhunter.scorers.cloud.requests.post", side_effect=answers) as post:
            with self.assertRaises(RateLimited):
                scorer.score(JOB, RESUME)                   # rests 60 s
            now[0] = 61
            with self.assertRaises(RateLimited):
                scorer.score(JOB, RESUME)                   # limited again in a row: rests 120 s
            now[0] = 61 + 100
            with self.assertRaises(RateLimited):
                scorer.score(JOB, RESUME)                   # still resting, no request
            self.assertEqual(post.call_count, 2)
            now[0] = 61 + 121
            scorer.score(JOB, RESUME)                       # works: the rest goes back to 60 s
            with self.assertRaises(RateLimited):
                scorer.score(JOB, RESUME)
            now[0] = 61 + 121 + 61
            self.assertEqual(scorer.keys.available(), [0])


class TestSetupErrors(unittest.TestCase):
    """A wrong key or model is a setup problem: the job must be retried after the fix, not failed for good."""

    def test_a_rejected_key_hands_over_and_all_rejected_is_unavailable(self):
        scorer = GeminiScorer(FAST, env={"GEMINI_API_KEYS": "bad,good"})
        with patch("jobhunter.scorers.cloud.requests.post",
                   side_effect=[FakeResponse(status_code=403, text="denied"), gemini_reply(GOOD)]):
            self.assertEqual(scorer.score(JOB, RESUME).score, 8)
        scorer = GeminiScorer(FAST, env={"GEMINI_API_KEYS": "bad1,bad2"})
        with patch("jobhunter.scorers.cloud.requests.post", return_value=FakeResponse(status_code=401, text="no")):
            with self.assertRaises(ScorerUnavailable) as caught:
                scorer.score(JOB, RESUME)
        self.assertIn("key", str(caught.exception))

    def test_an_unknown_model_is_unavailable(self):
        with patch("jobhunter.scorers.cloud.requests.post",
                   return_value=FakeResponse(status_code=404, text='{"error": "model not found"}')):
            with self.assertRaises(ScorerUnavailable):
                GroqScorer(FAST, env={"GROQ_API_KEYS": "g"}).score(JOB, RESUME)

    def test_the_doubling_resets_only_after_a_success(self):
        scorer = GeminiScorer(FAST, env={"GEMINI_API_KEYS": "k1"})
        scorer.keys._streak = 3
        with patch("jobhunter.scorers.cloud.requests.post", return_value=FakeResponse(status_code=400, text="bad")):
            with self.assertRaises(ScorerError):
                scorer.score(JOB, RESUME)
        self.assertEqual(scorer.keys._streak, 3)

    def test_current_default_models(self):
        # Groq shut llama-4-scout down on 2026-07-17; Gemini 2.5 is limited to earlier users.
        self.assertEqual(GroqScorer(FAST, env={"GROQ_API_KEYS": "g"}).options.model, "openai/gpt-oss-120b")
        self.assertEqual(GeminiScorer(FAST, env={"GEMINI_API_KEYS": "k"}).options.model, "gemini-3.5-flash-lite")


class TestOllamaMissingModel(unittest.TestCase):
    def test_a_model_that_is_not_pulled_is_unavailable(self):
        reply = FakeResponse(status_code=404, text='{"error":"model \'nonexistent:1b\' not found"}')
        with patch("jobhunter.scorers.ollama.requests.post", return_value=reply):
            with self.assertRaises(ScorerUnavailable) as caught:
                OllamaScorer({"model": "nonexistent:1b"}).score(JOB, RESUME)
        self.assertIn("ollama pull", str(caught.exception))


class TestSetupErrorsIn400(unittest.TestCase):
    def test_an_invalid_key_or_retired_model_reported_as_400_is_unavailable(self):
        replies = {
            "gemini": FakeResponse(status_code=400, text='{"error": {"code": 400, "message": "API key not valid.", '
                                                         '"status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}'),
            "groq": FakeResponse(status_code=400, text='{"error": {"message": "The model has been decommissioned", '
                                                       '"type": "invalid_request_error", "code": "model_decommissioned"}}'),
        }
        for name, cls, env in (("gemini", GeminiScorer, {"GEMINI_API_KEYS": "k"}), ("groq", GroqScorer, {"GROQ_API_KEYS": "g"})):
            with patch("jobhunter.scorers.cloud.requests.post", return_value=replies[name]):
                with self.assertRaises(ScorerUnavailable, msg=name):
                    cls(FAST, env=env).score(JOB, RESUME)

    def test_other_400s_stay_errors(self):
        with patch("jobhunter.scorers.cloud.requests.post", return_value=FakeResponse(status_code=400, text="bad prompt")):
            with self.assertRaises(ScorerError) as caught:
                GroqScorer(FAST, env={"GROQ_API_KEYS": "g"}).score(JOB, RESUME)
        self.assertNotIsInstance(caught.exception, ScorerUnavailable)


class TestCloudBusy(unittest.TestCase):
    def test_5xx_and_a_read_timeout_are_busy_and_a_refused_connection_is_not(self):
        from jobhunter.errors import ScorerBusy
        for kw, busy in (({"return_value": FakeResponse(status_code=503, text="down")}, True),
                         ({"side_effect": requests.ReadTimeout("slow")}, True),
                         ({"side_effect": requests.ConnectionError("refused")}, False)):
            with self.subTest(kw=kw), patch("jobhunter.scorers.cloud.requests.post", **kw):
                with self.assertRaises(ScorerUnavailable) as caught:
                    GeminiScorer(FAST, env={"GEMINI_API_KEYS": "k"}).score(JOB, RESUME)
                self.assertEqual(isinstance(caught.exception, ScorerBusy), busy)


if __name__ == "__main__":
    unittest.main()
