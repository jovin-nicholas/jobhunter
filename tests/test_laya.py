import json
import os
import sys
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from jobhunter.errors import ScorerError, ScorerUnavailable
from jobhunter.models import Job, Resume, score_label
from jobhunter.scorers.laya import LayaScorer, alert_state, band_score, build_state, job_state

JOB = Job("t_1", "Backend Engineer", "Acme", "Austin, TX", "https://example.com/1",
          description="Java and Kubernetes services. " + "x" * 9000)
RESUME = Resume("backend.txt", "the candidate — SDE. Java, Spring Boot.")
# check() first asks whether laya and torch are installed; these tests are about the checkpoint, so they pretend both
# are, and run on the base install (requirements.txt) too.
INSTALLED = patch("jobhunter.scorers.laya.importlib.util.find_spec", new=lambda name, package=None: object())


def score_probs(**by_score):
    """Laya's probabilities for the 10-level score question, keyed "0".."9" (option index = score - 1)."""
    return {str(i): by_score.get(f"s{i + 1}", 0.0) for i in range(10)}


class FakeAgent:
    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls, self.max_lens = answers or {}, error, [], []

    def system_one(self, state, questions, max_len=None):
        self.calls.append((state, questions))
        self.max_lens.append(max_len)
        if self.error:
            raise self.error
        return {"answers": {q: self.answers[q] for q in questions}}


def checkpoint(method, notify_at=7, log_at=5):
    folder = Path(tempfile.mkdtemp())
    (folder / "rl_agent_config.json").write_text(json.dumps({"training": {
        "decision_method": {"own": method, "public": "question"},
        "score_thresholds": {"notify_at": notify_at, "log_at": log_at}}}))
    return str(folder)


ALERT_Q = {"type": "noul", "instructions": "Is the job in `job` a good fit for the candidate in `candidate`?",
           "criteria": {"false": "not a fit", "true": "a good fit"}}


def alert_checkpoint(alert_at=0.6, save_at=0.3, with_block=True):
    folder = Path(tempfile.mkdtemp())
    training = {"decision_method": {"own": "alert"}}
    if with_block:
        training["alert"] = {"question": ALERT_Q, "alert_at": alert_at, "save_at": save_at}
    (folder / "rl_agent_config.json").write_text(json.dumps({"training": training}))
    return str(folder)


class TestAlertMethod(unittest.TestCase):
    def scorer(self, p, **options):
        agent = FakeAgent({"alert": {"type": "noul", "noul": p, "confidence": 0.9}})
        return LayaScorer({"model": alert_checkpoint(), **options}, agent_factory=lambda path, device: agent), agent

    def test_cutoffs_decide(self):
        for p, decision in ((0.6, "notify"), (0.59, "log"), (0.3, "log"), (0.29, "skip")):
            result = self.scorer(p)[0].score(JOB, RESUME)
            self.assertEqual((result.decision, result.score, result.probability), (decision, None, p), p)

    def test_state_is_resume_then_condensed_job_and_the_stored_question(self):
        scorer, agent = self.scorer(0.7)
        scorer.score(JOB, RESUME)
        state, questions = agent.calls[0]
        self.assertEqual(list(state), ["candidate", "job"])
        self.assertEqual(state, alert_state(RESUME.text, JOB))
        self.assertEqual(questions, {"alert": ALERT_Q})

    def test_overrides_replace_the_checkpoint_cutoffs(self):
        scorer, _ = self.scorer(0.5, alert_at=0.45, save_at=0.2)
        self.assertEqual(scorer.score(JOB, RESUME).decision, "notify")
        self.assertIn("45%", scorer.describe())

    def test_a_zero_override_is_still_an_override(self):
        # The settings check rejects save_at 0 for an alert checkpoint, so it is set after construction: describe()
        # must not take a falsy override for "no override".
        scorer, _ = self.scorer(0.5)
        scorer.options.save_at = 0.0
        self.assertEqual(scorer.describe(),
                         "laya: alert at 60% fit or higher, save for later from 0% (overridden in jobhunter.yaml)")

    def test_inverted_overrides_are_rejected(self):
        with self.assertRaises(ValueError):
            LayaScorer({"model": alert_checkpoint(), "alert_at": 0.3, "save_at": 0.5}, agent_factory=lambda p, d: None)

    def test_alert_at_override_without_save_at_on_a_checkpoint_with_no_cutoffs_is_rejected(self):
        with self.assertRaises(ValueError):
            LayaScorer({"model": alert_checkpoint(with_block=False), "alert_at": 0.5},
                       agent_factory=lambda p, d: None)

    def test_alert_checkpoint_without_cutoffs_is_unavailable(self):
        scorer = LayaScorer({"model": alert_checkpoint(with_block=False)},
                            agent_factory=lambda path, device: FakeAgent({}))
        with self.assertRaises(ScorerUnavailable):
            scorer.score(JOB, RESUME)

    def test_older_methods_have_no_cutoffs_to_describe(self):
        self.assertIsNone(LayaScorer({"model": checkpoint("question")}, agent_factory=lambda p, d: None).describe())

    def test_overrides_on_a_non_alert_checkpoint_are_rejected(self):
        with self.assertRaises(ValueError):
            LayaScorer({"model": checkpoint("question"), "alert_at": 0.6, "save_at": 0.3},
                      agent_factory=lambda p, d: None)

    @INSTALLED
    def test_check_flags_an_incomplete_alert_block(self):
        scorer = LayaScorer({"model": alert_checkpoint(with_block=False)}, agent_factory=lambda p, d: None)
        self.assertIn("alert checkpoint has no question or cut-offs in rl_agent_config.json", scorer.check())

    @INSTALLED
    def test_check_is_clean_for_a_complete_alert_checkpoint(self):
        scorer = LayaScorer({"model": alert_checkpoint()}, agent_factory=lambda p, d: None)
        self.assertIsNone(scorer.check())


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

    @INSTALLED
    def test_unsupported_method_check_message(self):
        scorer = LayaScorer({"model": checkpoint("levels")}, agent_factory=lambda p, d: None)
        self.assertEqual(scorer.check(),
                         f"{scorer.model}: trained with the 'levels' method, which jobhunter cannot score yet; "
                         "point laya at another checkpoint")

    def test_unsupported_method_raises_before_loading_the_agent(self):
        def factory(path, device):
            raise AssertionError("the agent factory must not be called for an unsupported method")

        scorer = LayaScorer({"model": checkpoint("levels")}, agent_factory=factory)
        with self.assertRaises(ScorerError) as cm:
            scorer.score(JOB, RESUME)
        self.assertEqual(str(cm.exception),
                         f"{scorer.model}: trained with the 'levels' method, which jobhunter cannot score yet; "
                         "point laya at another checkpoint")

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


ROLES = ["backend", "frontend", "fullstack", "ai", "data", "infra", "other"]
QUESTIONS = {
    "stack_role": {"type": "choice", "instructions": "Which kind of engineering role is the job in `job`?",
                   "criteria": {r: f"{r} work" for r in ROLES}},
    "dealbreaker": {"type": "noul", "instructions": "Does the job in `job` state a dealbreaker?",
                    "criteria": {"false": "none stated", "true": "one stated"}},
    "fit": {"type": "score", "instructions": "How good a fit is the job in `job` for the candidate in `candidate`?",
            "criteria": [f"{i} - level {i}" for i in range(1, 11)]},
}
INPUTS = {"stack_role": {"state": "job", "max_len": 1024}, "dealbreaker": {"state": "job", "max_len": 1024},
          "fit": {"state": "candidate_job", "max_len": 2048}}
GATES = {"stack_roles": ["backend", "fullstack", "ai", "data"], "dealbreaker_at": 0.5}
CUTOFFS = {"summary": "expected", "alert_at": 6.0746, "save_at": 4.8582}


def questions_training(drop=(), **overrides):
    training = {"decision_method": {"own": "questions"}, "questions": QUESTIONS, "inputs": INPUTS, "gates": GATES,
                "cutoffs": CUTOFFS, **overrides}
    return {k: v for k, v in training.items() if k not in drop}


def questions_checkpoint(drop=(), **overrides):
    folder = Path(tempfile.mkdtemp())
    (folder / "rl_agent_config.json").write_text(json.dumps({"training": questions_training(drop, **overrides)}))
    return str(folder)


def fit_at(expected):
    """Fit probabilities (keyed "0".."9") with expected 1-10 score `expected`, split over two neighbouring levels."""
    low = int(expected - 1)
    high = min(low + 1, 9)
    w = expected - 1 - low
    probs = {str(i): 0.0 for i in range(10)}
    probs[str(low)] += 1 - w
    probs[str(high)] += w
    return probs


def questions_answers(role="backend", deal=0.1, fit=None):
    return {"stack_role": {"type": "choice", "choice": role},
            "dealbreaker": {"type": "noul", "noul": deal},
            "fit": {"type": "score", "probabilities": fit or fit_at(6.4)}}


class TestQuestionsMethod(unittest.TestCase):
    def scorer(self, answers=None, model=None, **options):
        agent = FakeAgent(answers or questions_answers())
        scorer = LayaScorer({"model": model or questions_checkpoint(), **options},
                            agent_factory=lambda path, device: agent)
        return scorer, agent

    def test_role_gate_fails_skips_without_the_fit_call(self):
        scorer, agent = self.scorer(questions_answers(role="frontend"))
        result = scorer.score(JOB, RESUME)
        self.assertEqual((result.decision, result.score, result.probability), ("skip", None, None))
        self.assertEqual(result.reasoning, "Laya: skipped, stack role frontend")
        self.assertEqual([list(q) for _, q in agent.calls], [["stack_role"]])     # no dealbreaker call either

    def test_dealbreaker_gate_fails_skips_without_the_fit_call(self):
        scorer, agent = self.scorer(questions_answers(deal=0.72))
        result = scorer.score(JOB, RESUME)
        self.assertEqual((result.decision, result.score, result.probability), ("skip", None, None))
        self.assertEqual(result.reasoning, "Laya: skipped, likely dealbreaker (72%)")
        self.assertNotIn(["fit"], [list(q) for _, q in agent.calls])

    def test_a_failed_role_gate_names_only_the_role(self):
        result = self.scorer(questions_answers(role="infra", deal=0.5))[0].score(JOB, RESUME)
        self.assertEqual(result.reasoning, "Laya: skipped, stack role infra")

    def test_gate_questions_are_asked_separately_on_the_job_at_their_max_len(self):
        scorer, agent = self.scorer()
        scorer.score(JOB, RESUME)
        self.assertEqual(agent.calls[0], (job_state(JOB), {"stack_role": QUESTIONS["stack_role"]}))
        self.assertEqual(agent.calls[1], (job_state(JOB), {"dealbreaker": QUESTIONS["dealbreaker"]}))
        self.assertEqual(list(job_state(JOB)), ["job"])
        self.assertEqual(agent.max_lens[:2], [1024, 1024])

    def test_fit_is_asked_on_candidate_and_job_at_its_max_len(self):
        scorer, agent = self.scorer()
        scorer.score(JOB, RESUME)
        self.assertEqual(agent.calls[2], (alert_state(RESUME.text, JOB), {"fit": QUESTIONS["fit"]}))
        self.assertEqual(agent.max_lens[2], 2048)
        self.assertEqual(len(agent.calls), 3)

    def test_cutoffs_decide(self):
        for expected, decision in ((6.075, "notify"), (6.07, "log"), (4.86, "log"), (4.85, "skip")):
            result = self.scorer(questions_answers(fit=fit_at(expected)))[0].score(JOB, RESUME)
            self.assertEqual(result.decision, decision, expected)

    def test_score_probability_and_reasoning_after_the_fit(self):
        fit = {str(i): p for i, p in enumerate([0, 0, 0, 0, 0.2, 0.3, 0.3, 0.2, 0, 0])}   # E = 1 + 5.5 = 6.5
        result = self.scorer(questions_answers(fit=fit))[0].score(JOB, RESUME)
        self.assertEqual((result.decision, result.score), ("notify", 6.5))     # the decimal is kept
        self.assertAlmostEqual(result.probability, 0.5)                          # P(score 7-10) = p6 + p7
        self.assertEqual(result.reasoning,
                         "Laya: fit 6.50/10, at or above the alert cut-off 6.07; stack role backend")
        self.assertIn("java", result.matched_skills)

    def test_display_shows_the_fit_summary_not_a_rounded_score(self):
        # A notify at E 6.1 must not read "6/10", which the default decisions (notify at 7) would call a log.
        result = self.scorer(questions_answers(fit=fit_at(6.1)))[0].score(JOB, RESUME)
        self.assertEqual((result.decision, result.score), ("notify", 6.1))
        self.assertEqual(score_label(result), "fit 6.10/10")
        skipped = self.scorer(questions_answers(role="frontend"))[0].score(JOB, RESUME)
        self.assertEqual(score_label(skipped), "n/a")

    def test_a_job_just_below_a_cut_off_never_reads_as_above_it(self):
        # E 6.0745 is below the 6.0746 alert cut-off: shown floored (6.07) and said in words, never "6.1 ... 6.07".
        result = self.scorer(questions_answers(fit=fit_at(6.0745)))[0].score(JOB, RESUME)
        self.assertEqual(result.decision, "log")
        self.assertEqual(result.reasoning, "Laya: fit 6.07/10, below the alert cut-off 6.07, at or above the save "
                                           "cut-off 4.85; stack role backend")
        skipped = self.scorer(questions_answers(fit=fit_at(4.85)))[0].score(JOB, RESUME)
        self.assertEqual((skipped.decision, skipped.reasoning),
                         ("skip", "Laya: fit 4.85/10, below the save cut-off 4.85; stack role backend"))

    def test_a_notify_never_shows_a_value_below_its_cut_off(self):
        for expected in (6.0746, 6.0799, 6.5, 9.99):
            result = self.scorer(questions_answers(fit=fit_at(expected)))[0].score(JOB, RESUME)
            self.assertEqual(result.decision, "notify", expected)
            self.assertGreaterEqual(float(result.label.split()[1].split("/")[0]), 6.07, expected)

    def test_p_good_summary(self):
        model = questions_checkpoint(cutoffs={"summary": "p_good", "alert_at": 0.4, "save_at": 0.2})
        fit = {str(i): p for i, p in enumerate([0, 0, 0, 0, 0.3, 0.3, 0.3, 0.1, 0, 0])}   # P(good) = 0.4
        result = self.scorer(questions_answers(fit=fit), model=model)[0].score(JOB, RESUME)
        self.assertEqual((result.decision, result.score), ("notify", None))
        self.assertAlmostEqual(result.probability, 0.4)
        self.assertEqual(result.reasoning, "Laya: fit 40%, at or above the alert cut-off 40%; stack role backend")
        self.assertEqual(score_label(result), "fit 40%")

    def test_the_chain_keeps_the_decision(self):
        from jobhunter.scorers.chain import ScorerChain
        from jobhunter.settings import Decisions
        scorer = self.scorer(questions_answers(fit=fit_at(6.1)))[0]
        outcome = ScorerChain([("laya", scorer)], Decisions(notify_at=7, log_at=5)).score(JOB, RESUME)
        self.assertEqual(outcome.result.decision, "notify")

    @unittest.skipIf(find_spec("numpy") is None, "needs numpy: pip install -r requirements-train.txt")
    def test_parity_with_training(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "training"))
        try:
            import laya_train
        finally:
            sys.path.pop(0)
        sets = [questions_answers(), questions_answers(role="frontend"), questions_answers(deal=0.5),
                questions_answers(deal=0.4999, fit=fit_at(4.9)), questions_answers(role="data", fit=fit_at(3.2)),
                questions_answers(role="ai", fit={str(i): 0.1 for i in range(10)}),
                questions_answers(role="fullstack", fit=fit_at(6.0746)), questions_answers(fit=fit_at(9.7))]
        for cutoffs in (CUTOFFS, {"summary": "p_good", "alert_at": 0.35, "save_at": 0.15}):
            training = questions_training(cutoffs=cutoffs)
            model = questions_checkpoint(cutoffs=cutoffs)
            for answers in sets:
                with self.subTest(summary=cutoffs["summary"], answers=answers):
                    decision, value = laya_train.decide_from_answers(answers, training)
                    result = self.scorer(answers, model=model)[0].score(JOB, RESUME)
                    self.assertEqual(result.decision, decision)
                    if value is None:
                        self.assertIsNone(result.probability)
                    elif cutoffs["summary"] == "p_good":
                        self.assertAlmostEqual(result.probability, value)
                    else:
                        self.assertAlmostEqual(result.score, value, places=4)     # the training summary, unrounded

    def test_options_override_the_checkpoint(self):
        answers = questions_answers(role="infra", deal=0.3, fit=fit_at(5.5))
        result = self.scorer(answers, stack_roles=["infra"], dealbreaker_at=0.4, alert_at=5.5, save_at=3)[0] \
            .score(JOB, RESUME)
        self.assertEqual(result.decision, "notify")
        stricter = self.scorer(answers, dealbreaker_at=0.25, stack_roles=["infra"])[0].score(JOB, RESUME)
        self.assertEqual(stricter.reasoning, "Laya: skipped, likely dealbreaker (30%)")

    def test_summary_override_with_its_cutoffs(self):
        fit = {str(i): p for i, p in enumerate([0, 0, 0, 0, 0.5, 0.2, 0.3, 0, 0, 0])}   # P(good) = 0.3
        scorer = self.scorer(questions_answers(fit=fit), summary="p_good", alert_at=0.3, save_at=0.1)[0]
        self.assertEqual(scorer.score(JOB, RESUME).decision, "notify")

    def test_invalid_options_are_rejected(self):
        cases = [
            {"stack_roles": ["backend", "robotics"]},
            {"stack_roles": "backend"},
            {"stack_roles": []},
            {"dealbreaker_at": 0},
            {"dealbreaker_at": 1.5},
            {"dealbreaker_at": "high"},
            {"summary": "median"},
            {"summary": ["expected"]},
            {"summary": "p_good"},                              # the checkpoint's cut-offs are on the 1-10 scale
            {"summary": "p_good", "alert_at": 0.5},
            {"summary": "p_good", "alert_at": 6, "save_at": 4},
            {"alert_at": 11},
            {"alert_at": 4, "save_at": 5},
            {"save_at": 0.5},
            {"alert_at": "6"},
        ]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                LayaScorer({"model": questions_checkpoint(), **options}, agent_factory=lambda p, d: None)

    def test_unknown_role_lists_the_valid_roles(self):
        with self.assertRaises(ValueError) as cm:
            LayaScorer({"model": questions_checkpoint(), "stack_roles": ["robotics"]}, agent_factory=lambda p, d: None)
        self.assertIn("robotics", str(cm.exception))
        self.assertIn(", ".join(ROLES), str(cm.exception))

    def test_questions_options_on_other_methods_are_rejected(self):
        for model in (checkpoint("question"), alert_checkpoint()):
            for options in ({"stack_roles": ["backend"]}, {"dealbreaker_at": 0.5}, {"summary": "expected"}):
                with self.subTest(model=model, options=options), self.assertRaises(ValueError):
                    LayaScorer({"model": model, **options}, agent_factory=lambda p, d: None)

    @INSTALLED
    def test_check_is_clean_for_a_complete_checkpoint(self):
        self.assertIsNone(self.scorer()[0].check())

    @INSTALLED
    def test_check_names_what_an_incomplete_checkpoint_lacks(self):
        scorer = LayaScorer({"model": questions_checkpoint(drop=("gates", "cutoffs"))}, agent_factory=lambda p, d: None)
        self.assertEqual(scorer.check(),
                         f"{scorer.model}: questions checkpoint lacks gates, cutoffs in rl_agent_config.json")
        broken_inputs = dict(INPUTS, fit={"state": "resume", "max_len": 2048})
        scorer = LayaScorer({"model": questions_checkpoint(drop=("questions",), inputs=broken_inputs)},
                            agent_factory=lambda p, d: None)
        self.assertEqual(scorer.check(), f"{scorer.model}: questions checkpoint lacks questions.stack_role, "
                                         "questions.dealbreaker, questions.fit, inputs.fit in rl_agent_config.json")

    def test_incomplete_checkpoint_is_unavailable(self):
        scorer = self.scorer(model=questions_checkpoint(drop=("cutoffs",)))[0]
        with self.assertRaises(ScorerUnavailable):
            scorer.score(JOB, RESUME)

    def test_describe_from_the_checkpoint(self):
        self.assertEqual(self.scorer()[0].describe(),
                         "laya: questions gates: stack role backend, fullstack, ai or data, dealbreaker below 50% "
                         "(from the checkpoint); fit summary expected, alert at 6.07 or higher, save for later from "
                         "4.86 (from the checkpoint)")

    def test_describe_overrides(self):
        scorer = self.scorer(stack_roles=["backend"], summary="p_good", alert_at=0.4, save_at=0.2)[0]
        self.assertEqual(scorer.describe(),
                         "laya: questions gates: stack role backend, dealbreaker below 50% (overridden in "
                         "jobhunter.yaml); fit summary p_good, alert at 40% or higher, save for later from 20% "
                         "(overridden in jobhunter.yaml)")


@unittest.skipUnless(os.environ.get("LAYA_SLOW_TESTS"), "set LAYA_SLOW_TESTS=1 to load a real checkpoint")
class TestQuestionsCheckpointSlow(unittest.TestCase):
    def test_scores_an_invented_job_end_to_end(self):
        model = Path(os.environ.get("LAYA_SLOW_MODEL") or "")
        if not os.environ.get("LAYA_SLOW_MODEL") or not model.is_dir():
            self.skipTest("set LAYA_SLOW_MODEL to a questions checkpoint folder")
        scorer = LayaScorer({"model": str(model)})
        self.assertIsNone(scorer.check())
        job = Job("t_slow", "Backend Software Engineer", "Example Co", "Remote", "https://example.com/slow",
                  description="Build Python and Go services and REST APIs on PostgreSQL. 1+ years of experience.")
        result = scorer.score(job, Resume("generic.txt", "the candidate: software engineer. Python, Go, SQL."))
        self.assertIn(result.decision, ("notify", "log", "skip"))
        self.assertTrue(result.reasoning.startswith("Laya: "))
        if result.reasoning.startswith("Laya: fit"):
            self.assertTrue(1 <= result.score <= 10)
            self.assertTrue(0 <= result.probability <= 1)


if __name__ == "__main__":
    unittest.main()
