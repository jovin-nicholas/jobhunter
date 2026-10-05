import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "training"))
import laya_train as lt   # noqa: E402


def row(i, label, pool="jobhunter", title=None, company=None):
    return {"job_id": f"j{i}", "title": title or f"Engineer {i}", "company": company or f"Co {i}", "location": "",
            "description": "Requirements:\n3+ years of Python.", "label": label, "pool": pool}


def pool(n_notify, n_log, n_skip, pool_name="jobhunter", start=0):
    labels = ["notify"] * n_notify + ["log"] * n_log + ["skip"] * n_skip
    return [row(start + i, l, pool_name) for i, l in enumerate(labels)]


class TestGroupKey(unittest.TestCase):
    def test_reposts_share_a_key(self):
        a = row(1, "notify", title="Back-End Engineer", company="ACME")
        b = row(2, "notify", title="back end  engineer", company="acme")
        self.assertEqual(lt.group_key(a), lt.group_key(b))


class TestReadPool(unittest.TestCase):
    def test_reads_and_validates(self):
        path = Path(tempfile.mkdtemp()) / "p.jsonl"
        path.write_text(json.dumps(row(1, "notify")) + "\n" + json.dumps({**row(2, "maybe")}) + "\n")
        with self.assertRaises(ValueError):
            lt.read_pool(path, "jobhunter")
        path.write_text(json.dumps(row(1, "notify")) + "\n")
        self.assertEqual(lt.read_pool(path, "old")[0]["pool"], "old")


class TestSplits(unittest.TestCase):
    def test_old_pool_is_train_only_and_shares_follow_settings(self):
        rows = pool(40, 60, 300) + pool(30, 30, 140, "old", start=1000)
        s = lt.split_rows(rows, lt.Settings())
        self.assertTrue(all(r["pool"] == "jobhunter" for r in s["select"] + s["calibrate"]))
        self.assertEqual(sum(r["pool"] == "old" for r in s["train"]), 200)
        n = 400
        self.assertAlmostEqual(len(s["select"]) / n, 0.25, delta=0.05)
        self.assertAlmostEqual(len(s["calibrate"]) / n, 0.15, delta=0.05)
        for name in ("select", "calibrate"):
            share = sum(r["label"] == "notify" for r in s[name]) / len(s[name])
            self.assertAlmostEqual(share, 0.10, delta=0.05)

    def test_a_group_never_crosses_splits(self):
        rows = pool(40, 60, 300)
        for r in rows[:60]:
            r["title"], r["company"] = "Same Role", "Same Co"
        s = lt.split_rows(rows, lt.Settings())
        holders = {name for name, rs in s.items() for r in rs if lt.group_key(r) == ("same role", "same co")}
        self.assertEqual(len(holders), 1)

    def test_no_jobhunter_rows_is_an_error(self):
        with self.assertRaises(ValueError):
            lt.split_rows(pool(10, 10, 10, "old"), lt.Settings())

    def test_a_group_shared_by_both_pools_trains(self):
        rows = pool(40, 60, 300) + pool(30, 30, 140, "old", start=1000)
        rows[0]["title"], rows[0]["company"] = "Shared Role", "Shared Co"
        rows[400]["title"], rows[400]["company"] = "Shared Role", "Shared Co"
        logged = []
        s = lt.split_rows(rows, lt.Settings(), log=logged.append)
        self.assertIn(rows[0], s["train"])
        self.assertEqual(logged, ["1 jobhunter row(s) share a job group with the old pool: assigned to train, never "
                                  "select or calibrate"])


class TestSampling(unittest.TestCase):
    def test_smallest_label_plus_slack(self):
        out = lt.sample_train(pool(106, 138, 680), lt.Settings())
        self.assertEqual(Counter(r["label"] for r in out), {"notify": 106, "log": 138, "skip": 156})

    def test_same_seed_same_sample(self):
        rows = pool(20, 30, 200)
        self.assertEqual([r["job_id"] for r in lt.sample_train(rows, lt.Settings())],
                         [r["job_id"] for r in lt.sample_train(rows, lt.Settings())])


class FakeTok:
    pad_token_id = 0


class TestItems(unittest.TestCase):
    def test_items_carry_noul_targets_and_the_cut_flag(self):
        calls = []

        def fake_build_sequence(tok, state, q, max_len, head_max_len):
            calls.append((state, q, max_len, head_max_len))
            return list(range(10 if state["job"]["title"] != "Long" else max_len)), [8, 9]
        patcher = unittest.mock.patch.object(lt, "build_sequence", fake_build_sequence)
        patcher.start()
        self.addCleanup(patcher.stop)
        rows = [row(1, "notify"), {**row(2, "skip"), "title": "Long"}]
        items = lt.build_items(rows, "the candidate", FakeTok(), lt.Settings())
        self.assertEqual([(it["label"], it["target"], it["gold"], it["cut"]) for it in items],
                         [(1, [0.0, 1.0], "notify", False), (0, [1.0, 0.0], "skip", True)])
        state, q, max_len, head = calls[0]
        self.assertEqual(list(state), ["candidate", "job"])
        self.assertEqual((q["t"], max_len, head), ("noul", 2048, 256))


class TestCutoffs(unittest.TestCase):
    def test_best_precision_that_catches_half(self):
        p = np.array([0.95, 0.9, 0.85, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2])
        y = np.array([1, 1, 0, 1, 0, 1, 0, 0, 1, 0])          # 5 good jobs
        c = lt.pick_alert_cutoff(p, y, 0.5)
        self.assertEqual((c.threshold, c.alerts, round(c.precision, 2), c.recall, c.met_rule), (0.8, 4, 0.75, 0.6, True))

    def test_a_high_recall_target_is_met_by_alerting_more(self):
        p = np.array([0.9, 0.2, 0.1])
        y = np.array([0, 1, 1])
        c = lt.pick_alert_cutoff(p, y, 0.99)
        # At t=0.1 (min of positive probs), recall = 2/2 = 1.0 >= 0.99 with precision 2/3
        self.assertTrue(c.met_rule)
        self.assertEqual(c.recall, 1.0)

    def test_no_good_jobs_falls_back_and_flags_it(self):
        p = np.array([0.9, 0.2, 0.1])
        y = np.array([0, 0, 0])
        c = lt.pick_alert_cutoff(p, y, 0.5)
        self.assertFalse(c.met_rule)
        self.assertEqual(c.recall, 0.0)
        self.assertEqual(c.alerts, 1)

    def test_save_cutoff_separates_log_from_skip_below_alert(self):
        p = np.array([0.9, 0.45, 0.4, 0.35, 0.2, 0.1, 0.05])
        gold = ["notify", "log", "log", "skip", "log", "skip", "skip"]
        self.assertEqual(lt.pick_save_cutoff(p, gold, 0.8), 0.2)

    def test_a_cutoff_is_never_rounded_above_its_own_job(self):
        # 0.9000006 would round to 0.900001, which no longer alerts the job it came from
        c = lt.pick_alert_cutoff(np.array([0.9000006, 0.1]), np.array([1, 0]), 0.5)
        self.assertEqual((c.threshold, c.precision, c.alerts), (0.9000006, 1.0, 1))
        self.assertEqual(lt.pick_save_cutoff(np.array([0.9, 0.4000006, 0.1]), ["notify", "log", "skip"], 0.8), 0.4000006)

    def test_cutoff_table_lists_nearby_cutoffs(self):
        p = np.array([0.9, 0.6, 0.4, 0.1])
        y = np.array([1, 1, 0, 0])
        rows = lt.cutoff_table(p, y, around=0.5)
        thresholds = [row[0] for row in rows]
        self.assertEqual(thresholds, [0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65])
        row_for_05 = [row for row in rows if row[0] == 0.5][0]
        self.assertEqual(row_for_05, (0.5, 1.0, 1.0, 2))


class TestTemperature(unittest.TestCase):
    def test_overconfident_logits_get_softened(self):
        rng = np.random.default_rng(0)
        y = rng.integers(0, 2, 400)
        true_logit = np.where(y == 1, 1.0, -1.0) + rng.normal(0, 1, 400)
        logits = np.stack([np.zeros(400), true_logit * 4], axis=1)        # 4x too sharp
        t = lt.fit_temperature(logits, y)
        # the Bayes log-odds are 2x, so 4x logits need temperature 2
        self.assertTrue(1.6 < t < 2.4, t)

    def test_probs_from_logits(self):
        p = lt.probs_from_logits(np.array([[0.0, 0.0], [0.0, 2.0]]), temperature=2.0)
        np.testing.assert_allclose(p, [0.5, 1 / (1 + np.exp(-1))], rtol=1e-6)


class TestHealthAndExport(unittest.TestCase):
    def test_input_health_counts(self):
        rows = [{**row(1, "notify"), "description": "One block of text with Python."},
                {**row(2, "skip"), "description": "Job Details:\nRemote.\nRequirements:\nGo."}]
        items = [{"cut": True}, {"cut": False}]
        self.assertEqual(lt.input_health(rows, items),
                         {"postings": 2, "no_headings": 1, "empty_requirements": 1, "unknown_headings": 1, "cut": 1})

    def test_export_writes_what_the_scorer_reads(self):
        import torch
        base = Path(tempfile.mkdtemp()); (base / "tokenizer").mkdir(); (base / "encoder").mkdir()
        out = Path(tempfile.mkdtemp()) / "ckpt"
        cfg = {"temperature_by_options": {"choice:3-5": 1.4}, "max_len": 512}
        lt.export_checkpoint({"w": torch.ones(2)}, str(base), cfg, out, lt.Settings(), lt.ALERT_QUESTION,
                             0.62, 0.3, 1.2, {"select": {"precision": 0.5}})
        saved = json.loads((out / "rl_agent_config.json").read_text())
        self.assertEqual(saved["training"]["decision_method"], {"own": "alert"})
        self.assertEqual(saved["training"]["alert"], {"question": lt.ALERT_QUESTION, "alert_at": 0.62, "save_at": 0.3})
        self.assertEqual(saved["temperature_by_options"], {"choice:3-5": 1.4, "noul:2": 1.2})
        self.assertEqual((saved["max_len"], saved["head_max_len"]), (2048, 256))
        # what jobhunter reads back
        from jobhunter.scorers.laya import LayaScorer
        scorer = LayaScorer({"model": str(out)}, agent_factory=lambda p, d: None)
        self.assertEqual((scorer.method, scorer.cutoffs()), ("alert", (0.62, 0.3)))

    def test_export_floors_cutoffs_instead_of_rounding(self):
        import torch
        base = Path(tempfile.mkdtemp()); (base / "tokenizer").mkdir(); (base / "encoder").mkdir()
        out = Path(tempfile.mkdtemp()) / "ckpt"
        lt.export_checkpoint({"w": torch.ones(2)}, str(base), {"max_len": 512}, out, lt.Settings(),
                             lt.ALERT_QUESTION, 0.90006, 0.3, 1.0, {"select": {"precision": 0.5}})
        saved = json.loads((out / "rl_agent_config.json").read_text())
        # round(0.90006, 4) would give 0.9001, which lifts the cut-off above the job it was fitted on.
        self.assertEqual(saved["training"]["alert"]["alert_at"], 0.9)

    def test_export_casts_floating_tensors_to_the_base_checkpoints_dtype(self):
        import torch
        from safetensors.torch import save_file
        base = Path(tempfile.mkdtemp()); (base / "tokenizer").mkdir(); (base / "encoder").mkdir()
        save_file({"w": torch.zeros(2, dtype=torch.float16)}, str(base / "model.safetensors"))
        out = Path(tempfile.mkdtemp()) / "ckpt"
        state = {"w": torch.ones(2, dtype=torch.float32), "count": torch.tensor([3])}
        lt.export_checkpoint(state, str(base), {}, out, lt.Settings(), lt.ALERT_QUESTION, 0.6, 0.3, 1.0,
                             {"select": {"precision": 0.5}})
        from safetensors.torch import load_file
        saved = load_file(str(out / "model.safetensors"))
        self.assertEqual(saved["w"].dtype, torch.float16)
        self.assertEqual(saved["count"].dtype, torch.int64)      # non-floating tensors are left alone


class TestReportText(unittest.TestCase):
    def _report(self, **over):
        base = {
            "select": {"alerts": 5, "precision": 0.2, "recall": 0.4, "met_rule": False},
            "alert_at": 0.6, "save_at": 0.3, "temperature": 1.5,
            "ece_before": 0.2, "ece_after": 0.05,
            "table": [(0.5, 0.3, 0.4, 5)],
            "p_min": 0.1, "p_max": 0.25,
            "confusion": {"notify": {"alert": 1, "save": 0, "skip": 0}},
            "health": {"train": {"postings": 10}},
            "base_rate": 0.15,
        }
        base.update(over)
        return base

    def test_rule_not_met_warning(self):
        text = lt.report_text(self._report())
        self.assertIn("!! No cut-off caught half the good jobs: the model is weak; do not switch to it.", text)

    def test_narrow_band_warning(self):
        text = lt.report_text(self._report())
        self.assertIn("!! Probabilities sit in a narrow band or never reach the alert cut-off: check before switching.",
                      text)

    def test_barely_better_than_random_warning(self):
        text = lt.report_text(self._report())
        self.assertIn("!! Alerts are barely better than alerting at random (worth opening 20% against a 15% base "
                      "rate): do not switch to this model.", text)

    def test_no_warnings_when_the_model_is_healthy(self):
        report = self._report(select={"alerts": 5, "precision": 0.9, "recall": 0.8, "met_rule": True},
                              p_min=0.1, p_max=0.95, base_rate=0.15)
        text = lt.report_text(report)
        self.assertNotIn("!!", text)


@unittest.skipUnless(os.environ.get("LAYA_SLOW_TESTS"), "set LAYA_SLOW_TESTS=1 to load a real checkpoint")
class TestOneTrainingStep(unittest.TestCase):
    def test_one_epoch_on_a_few_items_runs_and_scores(self):
        import torch
        base = str(Path.home() / "models" / "laya_finetuned")
        device = torch.device("cpu")
        model, tok, cfg = lt.load_base(base, device)
        settings = lt.Settings(max_len=256, epochs=1, micro_batch=2, grad_accum=1)
        rows = pool(2, 1, 1)
        items = lt.build_items(rows, "the candidate", tok, settings)
        result = lt.train(model, items, items, settings, device, log=lambda s: None)
        self.assertEqual(result.epoch, 1)
        self.assertEqual(lt.predict_logits(model, items, settings, device).shape, (4, 2))
