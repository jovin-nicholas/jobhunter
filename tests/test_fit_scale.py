import json
import tempfile
import unittest
from pathlib import Path

from jobhunter.fit_scale import (FitScale, calibrate, fit_of, is_expected_label, load_scales, save_scales)
from jobhunter.models import Job, ScoreResult
from jobhunter.store import Store

MODEL = "laya:~/models/questions"


def store_with(scores, model=MODEL, label=lambda s: f"fit {s:.2f}/10"):
    folder = Path(tempfile.mkdtemp())
    store = Store(folder / "jobs.db")
    for i, s in enumerate(scores):
        store.save(Job(f"dice_{i}", "Engineer", "Acme", "Remote", f"https://e.example/{i}", source="dice"),
                   "logged", ScoreResult(score=s, model=model, label=label(s)))
    return folder, store


class TestFitScale(unittest.TestCase):
    def test_scale_and_clip(self):
        s = FitScale(3.0, 7.0)
        self.assertEqual([round(s.scale(x), 2) for x in (3.0, 5.0, 6.5, 7.0, 2.0, 9.0)], [0.0, 5.0, 8.75, 10.0, 0.0, 10.0])

    def test_expected_labels_only(self):
        self.assertTrue(is_expected_label("fit 6.42/10"))
        for label in ("fit 72%", "8/10", "", None, "fit 6.42/10 (raw 6.42)"):
            self.assertFalse(is_expected_label(label), label)

    def test_fit_of_still_drops_decision_placeholders(self):
        self.assertIsNone(fit_of(8, "", "laya:~/models/old"))
        self.assertEqual(fit_of(6.5, "", "laya:~/models/questions"), 6.5)
        self.assertEqual(fit_of(8, "", "ollama:gemma4:e4b"), 8)


class TestCalibrate(unittest.TestCase):
    def test_percentile_bounds_per_laya_model(self):
        scores = [2.0] + [3.0 + i * 0.04 for i in range(98)] + [9.5]        # 100 fits with an outlier at each end
        folder, store = store_with(scores)
        bounds = calibrate(store.path)
        self.assertEqual(list(bounds), [MODEL])
        self.assertEqual(bounds[MODEL]["n"], 100)
        self.assertLess(bounds[MODEL]["low"], 3.05)                          # the 1st percentile, not 2.0 itself
        self.assertGreater(bounds[MODEL]["low"], 2.0)
        self.assertLess(bounds[MODEL]["high"], 9.5)
        self.assertGreater(bounds[MODEL]["high"], 6.8)

    def test_too_few_placeholders_and_other_scorers_are_not_calibrated(self):
        folder, store = store_with([5.0 + i * 0.05 for i in range(29)])                  # 29 < MIN_FITS
        self.assertEqual(calibrate(store.path), {})
        folder, store = store_with([8] * 40, model="laya:~/models/old", label=lambda s: "")
        self.assertEqual(calibrate(store.path), {})
        folder, store = store_with([float(1 + i % 10) for i in range(40)], model="ollama:gemma4:e4b")
        self.assertEqual(calibrate(store.path), {})

    def test_a_flat_model_is_not_calibrated(self):
        folder, store = store_with([6.0 + (i % 3) * 0.1 for i in range(40)])              # spread under 0.5
        self.assertEqual(calibrate(store.path), {})


class TestFile(unittest.TestCase):
    def test_round_trip(self):
        folder = Path(tempfile.mkdtemp())
        path = save_scales(folder, {MODEL: {"low": 3.0, "high": 6.9, "n": 455, "computed_at": "2026-10-08T00:00:00+00:00"}})
        self.assertEqual(path, folder / "fit_scale.json")
        self.assertEqual(load_scales(folder), {MODEL: FitScale(3.0, 6.9)})

    def test_missing_or_corrupt_file_means_no_scaling(self):
        folder = Path(tempfile.mkdtemp())
        self.assertEqual(load_scales(folder), {})
        (folder / "fit_scale.json").write_text("{not json")
        self.assertEqual(load_scales(folder), {})
        (folder / "fit_scale.json").write_text(json.dumps({MODEL: {"low": 5, "high": 5}, "x": "bad"}))
        self.assertEqual(load_scales(folder), {})
