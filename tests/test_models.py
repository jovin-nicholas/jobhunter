import unittest

from jobhunter.models import KEEP, Job, ScoreResult, decide, score_label, skip


class TestDecide(unittest.TestCase):
    def test_default_thresholds(self):
        self.assertEqual([decide(s, 7, 5) for s in (10, 7, 6, 5, 4, 1)],
                         ["notify", "notify", "log", "log", "skip", "skip"])

    def test_custom_thresholds(self):
        self.assertEqual([decide(s, 9, 3) for s in (9, 8, 3, 2)], ["notify", "log", "log", "skip"])


class TestJob(unittest.TestCase):
    def test_extra_is_not_shared_between_jobs(self):
        a = Job("a", "t", "c", "l", "u")
        b = Job("b", "t", "c", "l", "u")
        a.extra["guid"] = "x"
        self.assertEqual(b.extra, {})


class TestFilterResults(unittest.TestCase):
    def test_keep_and_skip(self):
        self.assertTrue(KEEP.keep)
        result = skip("too senior")
        self.assertEqual((result.keep, result.reason), (False, "too senior"))


class TestScoreLabel(unittest.TestCase):
    def test_score_and_probability_labels(self):
        self.assertEqual(score_label(ScoreResult(score=8, model="m")), "8/10")
        self.assertEqual(score_label(ScoreResult(score=None, model="m", probability=0.724)), "fit 72%")

    def test_a_label_set_by_the_scorer_is_shown_as_given(self):
        self.assertEqual(score_label(ScoreResult(score=6, model="m", probability=0.4, label="fit 6.1/10")),
                         "fit 6.1/10")

    def test_neither_score_nor_probability_is_not_available(self):
        self.assertEqual(score_label(ScoreResult(score=None, model="m")), "n/a")


class TestScoreLabelScalesUnlabelledFits(unittest.TestCase):
    """The tracker and tracker-calibrate treat an unlabelled whole Laya score as a real fit unless it is a decision
    placeholder (fit_of); score_label must scale it the same way, not only a labelled "fit X/10" result."""
    MODEL = "laya:~/models/questions"

    def test_an_unlabelled_real_fit_is_scaled_like_a_labelled_one(self):
        from jobhunter.fit_scale import FitScale
        scales = {self.MODEL: FitScale(3.0, 7.0)}
        result = ScoreResult(score=6.5, model=self.MODEL)              # e.g. the "from_score" method: no label
        self.assertEqual(score_label(result, scales), "fit 8.8/10 (raw 6.50)")

    def test_a_decision_placeholder_is_never_scaled(self):
        from jobhunter.fit_scale import FitScale
        scales = {"laya:~/models/old": FitScale(3.0, 7.0)}
        result = ScoreResult(score=8, model="laya:~/models/old")       # whole number, no label: alert/save/skip
        self.assertEqual(score_label(result, scales), "8/10")

    def test_a_probability_result_is_never_scaled(self):
        from jobhunter.fit_scale import FitScale
        scales = {self.MODEL: FitScale(0.0, 1.0)}
        result = ScoreResult(score=None, probability=0.72, model=self.MODEL)
        self.assertEqual(score_label(result, scales), "fit 72%")

    def test_no_scale_for_the_model_leaves_it_as_before(self):
        result = ScoreResult(score=6.5, model=self.MODEL)
        self.assertEqual(score_label(result), "6.5/10")


if __name__ == "__main__":
    unittest.main()
