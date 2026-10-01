import json
import tempfile
import unittest
from pathlib import Path

from jobhunter.errors import ScorerError
from jobhunter.models import Job, Resume
from jobhunter.scorers.laya import LayaScorer, band_score, build_state

JOB = Job("t_1", "Backend Engineer", "Acme", "Austin, TX", "https://example.com/1",
          description="Java and Kubernetes services. " + "x" * 9000)
RESUME = Resume("backend.txt", "the candidate — SDE. Java, Spring Boot.")


def score_probs(**by_score):
    """Laya's probabilities for the 10-level score question, keyed "0".."9" (option index = score - 1)."""
    return {str(i): by_score.get(f"s{i + 1}", 0.0) for i in range(10)}


class FakeAgent:
    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls = answers or {}, error, []

    def system_one(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return {"answers": {q: self.answers[q] for q in questions}}


def checkpoint(method, notify_at=7, log_at=5):
    folder = Path(tempfile.mkdtemp())
    (folder / "rl_agent_config.json").write_text(json.dumps({"training": {
        "decision_method": {"own": method, "public": "question"},
        "score_thresholds": {"notify_at": notify_at, "log_at": log_at}}}))
    return str(folder)


class TestBandScore(unittest.TestCase):
    def test_band_with_most_probability_wins_even_when_the_mean_is_lower(self):
        probs = [0, 0, 0, 0, 0.25, 0.15, 0.25, 0.35, 0, 0]      # mean 6.7, but 60% on scores 7-8
        self.assertEqual(band_score(probs, 7, 5), 8)

    def test_custom_thresholds(self):
        probs = [0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.15, 0.1, 0.05]
        self.assertEqual(band_score(probs, 9, 3), 8)            # 3-8 holds 0.65, and 8 is its most likely score


class TestLayaScorer(unittest.TestCase):
    def test_from_score_checkpoint_uses_bands(self):
        agent = FakeAgent({"match_score": {"probabilities": score_probs(s5=0.25, s6=0.15, s7=0.25, s8=0.35),
                                           "answer_confidence": 0.35}})
        scorer = LayaScorer({"model": checkpoint("from_score")}, agent_factory=lambda path, device: agent)
        result = scorer.score(JOB, RESUME)
        self.assertEqual((result.score, result.confidence), (8, 0.35))
        self.assertEqual(set(agent.calls[0][1]), {"match_score"})
        self.assertIn("java", result.matched_skills)
        self.assertIn("kubernetes", result.keyword_gaps)

    def test_question_checkpoint_maps_decisions_to_scores(self):
        for choice, expected in (("notify", 8), ("log", 6), ("skip", 3)):
            agent = FakeAgent({"decision": {"choice": choice, "answer_confidence": 0.7}})
            scorer = LayaScorer({"model": checkpoint("question")}, agent_factory=lambda p, d: agent)
            with self.subTest(choice=choice):
                self.assertEqual(scorer.score(JOB, RESUME).score, expected)

    def test_model_without_training_info_uses_the_decision_question(self):
        agent = FakeAgent({"decision": {"choice": "notify", "answer_confidence": 0.9}})
        scorer = LayaScorer({"model": "convaiinnovations/laya"}, agent_factory=lambda p, d: agent)
        self.assertEqual(scorer.score(JOB, RESUME).score, 8)

    def test_state_matches_the_training_layout_and_caps_the_description(self):
        state = build_state(RESUME.text, JOB)
        self.assertTrue(state.startswith("Candidate profile:\nthe candidate — SDE. Java, Spring Boot.\n\n"
                                         "Job title: Backend Engineer\nCompany: Acme\n\nJob description:\nJava"))
        self.assertEqual(len(state.split("Job description:\n", 1)[1]), 8000)

    def test_prediction_error_becomes_a_scorer_error(self):
        scorer = LayaScorer({"model": checkpoint("question")},
                            agent_factory=lambda p, d: FakeAgent(error=RuntimeError("CUDA out of memory")))
        with self.assertRaises(ScorerError):
            scorer.score(JOB, RESUME)

    def test_load_failure_is_raised_once_then_fails_fast(self):
        attempts = []

        def broken(path, device):
            attempts.append(path)
            raise ScorerError("the laya package is not installed")

        scorer = LayaScorer({"model": "/no/such/model"}, agent_factory=broken)
        for _ in range(3):
            with self.assertRaises(ScorerError):
                scorer.score(JOB, RESUME)
        self.assertEqual(len(attempts), 1)



class TestReviewFixes(unittest.TestCase):
    def test_load_failure_is_unavailable(self):
        from jobhunter.errors import ScorerUnavailable

        def missing(path, device):
            raise ScorerUnavailable("the laya package is not installed")

        with self.assertRaises(ScorerUnavailable):
            LayaScorer({"model": "/no/such/model"}, agent_factory=missing).score(JOB, RESUME)

    def test_malformed_answer_is_a_scorer_error(self):
        agent = FakeAgent({"match_score": {"choice": "odd reply without probabilities"}})
        scorer = LayaScorer({"model": checkpoint("from_score")}, agent_factory=lambda p, d: agent)
        with self.assertRaises(ScorerError):
            scorer.score(JOB, RESUME)


if __name__ == "__main__":
    unittest.main()
