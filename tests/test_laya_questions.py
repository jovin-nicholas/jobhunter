"""Tests for the questions method in training/laya_train.py: three questions (stack role, dealbreaker, fit) combined by
gates and fitted cut-offs. Invented jobs only."""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import warnings
from collections import Counter
from importlib.util import find_spec
from pathlib import Path

if find_spec("numpy") is None:          # laya_train needs numpy: the base install (requirements.txt) skips this module
    raise unittest.SkipTest("needs numpy: pip install -r requirements-train.txt")

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "training"))
import laya_train as lt   # noqa: E402


def needs(*packages):
    """Skips a test when an optional package (requirements-laya.txt, requirements-train.txt) is not installed."""
    missing = [p for p in packages if find_spec(p) is None]
    return unittest.skipIf(missing, f"needs {', '.join(missing)}: pip install -r requirements-train.txt")


def qrow(i, label="skip", pool="jobhunter", role="backend", deal=None, score=None, title=None, company=None,
         description="Requirements:\n3+ years of Python.", known=True):
    default = {"notify": 8, "log": 6, "skip": 3}[label]
    return {"job_id": f"q{i}", "title": title or f"Engineer {i}", "company": company or f"Co {i}", "location": "",
            "description": description, "label": label, "pool": pool, "match_score": score or default,
            "stack_role": role, "dealbreaker": deal, "dealbreaker_known": known}


class TestDealbreakerMapping(unittest.TestCase):
    def test_categories_and_targets(self):
        cases = {
            None: ("none", False),
            "": ("none", False),
            "Security clearance": ("clearance", True),
            "us-citizenship-only": ("citizenship", True),
            "staff-level": ("seniority", True),
            "pure data/etl": ("role_family", True),
            "pure frontend": ("role_family", True),
            "5+ required years": ("years", False),
            "6+ years required": ("years", False),
            "location outside US": ("location", False),
            "based in Canada": ("location", False),
            "people-management": ("other", True),
            "Fluent French required": ("other", True),
            "5+ required years; staff title": ("seniority", True),
            "5+ required years; public trust clearance": ("clearance", True),
            "outside the us; 5+ required years": ("location", False),
            "pure mobile; 5+ required years": ("other", True),
            "fpga role; outside the us": ("other", True),
            "embedded/outside us": ("role_family", True),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(lt.dealbreaker_category(text), expected)
                self.assertEqual(lt.dealbreaker_target(text), expected[1])

    def test_short_words_match_only_as_words(self):
        # "bi" and "lead" must not fire inside other words
        self.assertEqual(lt.dealbreaker_category("ambiguous leadership wording"), ("other", True))
        self.assertEqual(lt.dealbreaker_category("requires a BI developer"), ("role_family", True))

    def test_role_target_maps_unknown_roles_to_other(self):
        self.assertEqual([lt.role_target(v) for v in ("AI", " data ", "mobile", None)], ["ai", "data", "other", "other"])

    def test_label_gates(self):
        self.assertTrue(lt.label_gates_pass(qrow(1, role="fullstack", deal="5+ required years")))
        self.assertFalse(lt.label_gates_pass(qrow(2, role="infra")))
        self.assertFalse(lt.label_gates_pass(qrow(3, role="backend", deal="security clearance")))

    def test_counts_per_pool_and_category(self):
        rows = [qrow(1, deal="security clearance"), qrow(2, deal="5+ required years"), qrow(3),
                qrow(4, pool="old", deal="outside the us")]
        self.assertEqual(lt.dealbreaker_counts(rows),
                         {"jobhunter": {"clearance": 1, "none": 1, "years": 1}, "old": {"location": 1}})


def posting(n_words=60, changed=()):
    """An invented posting whose requirements are `n_words` distinct words; `changed` positions get another word."""
    words = [f"skill{i}" for i in range(n_words)]
    for i in changed:
        words[i] = f"other{i}"
    return "Requirements:\n" + " ".join(words)


class TestNearDuplicates(unittest.TestCase):
    def test_shingles_need_thirty_words(self):
        self.assertIsNone(lt.shingles(qrow(1, description=posting(29))))
        self.assertEqual(len(lt.shingles(qrow(1, description=posting(60)))), 56)

    def test_one_changed_word_is_similar_two_are_not(self):
        base = lt.shingles(qrow(1, description=posting()))
        one = lt.shingles(qrow(2, description=posting(changed=(30,))))         # 51 shared of 61: 0.836
        two = lt.shingles(qrow(3, description=posting(changed=(10, 40))))      # 46 shared of 66: 0.697
        self.assertEqual(lt.similar_pairs([one, two], [base]), [(0, 0)])
        self.assertEqual(lt.similar_pairs([base, one, two, None]), [(0, 1)])

    def test_boilerplate_ngrams_propose_no_candidates(self):
        base = lt.shingles(qrow(1, description=posting()))
        self.assertEqual(lt.similar_pairs([base], [base], max_df=0), [])

    def test_exclusion_counts_each_rule(self):
        reference = [qrow(1, title="Data Engineer", company="Acme", description=posting())]
        rows = [qrow(1, title="Renamed", company="Elsewhere"),                                   # same id
                qrow(2, title="data-engineer", company="ACME"),                                  # same title + company
                qrow(3, title="Platform Engineer", company="Beta", description=posting(changed=(30,))),  # similar
                qrow(4, title="Platform Engineer", company="Beta", description=posting(changed=(10, 40))),
                qrow(5)]
        kept, counts = lt.exclude_near_duplicates(rows, reference)
        self.assertEqual([r["job_id"] for r in kept], ["q4", "q5"])
        self.assertEqual(counts, {"same_id": 1, "same_title_company": 1, "similar_text": 1})

    def test_groups_join_title_company_and_similar_text(self):
        rows = [qrow(1, title="A", company="X", description=posting()),
                qrow(2, title="B", company="Y", description=posting(changed=(30,))),
                qrow(3, title="A", company="X"),
                qrow(4, title="D", company="Z", description=posting(changed=(10, 40)))]
        self.assertEqual(lt.job_groups(rows), [0, 0, 0, 1])

    def test_a_jobhunter_row_similar_to_an_old_row_trains(self):
        rows = [qrow(i, label) for i, label in enumerate(["notify"] * 40 + ["log"] * 60 + ["skip"] * 300)]
        rows.append(qrow(900, "notify", pool="old", title="Old Title", company="Old Co", description=posting()))
        rows.append(qrow(901, "notify", title="New Title", company="New Co", description=posting(changed=(30,))))
        logged = []
        s = lt.split_rows(rows, lt.Settings(), log=logged.append)
        self.assertIn("q901", [r["job_id"] for r in s["train"]])
        self.assertEqual(len(logged), 1)


class TestReadPoolForQuestions(unittest.TestCase):
    def _write(self, rows):
        path = Path(tempfile.mkdtemp()) / "p.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    def test_question_fields_are_required_and_checked(self):
        good = {k: v for k, v in qrow(1).items() if k not in ("pool", "dealbreaker_known")}
        self.assertEqual(lt.read_pool(self._write([good]), "jobhunter", questions=True)[0]["dealbreaker_known"], True)
        for bad in ({**good, "match_score": 11}, {**good, "match_score": "7"},
                    {k: v for k, v in good.items() if k != "stack_role"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                lt.read_pool(self._write([bad]), "jobhunter", questions=True)

    def test_unknown_dealbreaker_is_kept(self):
        row = {k: v for k, v in qrow(1).items() if k != "pool"} | {"dealbreaker_known": False}
        self.assertFalse(lt.read_pool(self._write([row]), "old", questions=True)[0]["dealbreaker_known"])

    def test_alert_pools_still_read_without_question_fields(self):
        row = {"job_id": "a", "title": "t", "company": "c", "description": "d", "label": "log"}
        self.assertEqual(lt.read_pool(self._write([row]), "old")[0]["pool"], "old")


class FakeTok:
    pad_token_id = 0


@needs("laya")
class TestQuestionItems(unittest.TestCase):
    def setUp(self):
        from laya.common import render_options
        self.calls = []

        def fake_build_sequence(tok, state, q, max_len, head_max_len):
            self.calls.append((state, q, max_len, head_max_len))
            n = len(render_options(q))
            return list(range(30)), list(range(10, 10 + n))
        patcher = unittest.mock.patch.object(lt, "build_sequence", fake_build_sequence)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_laya_renders_the_questions(self):
        from laya.common import render_options
        fit = render_options(lt.to_internal(lt.QUESTIONS["fit"]))
        self.assertEqual((fit[0], fit[9]), ("level 0: 1 - disqualifying mismatch (wrong role, stack or seniority)",
                                            "level 9: 10 - exceptional fit"))
        role = render_options(lt.to_internal(lt.QUESTIONS["stack_role"]))
        self.assertEqual(role[0], "backend: server-side services, APIs, databases or distributed systems")
        self.assertEqual(len(role), 7)
        deal = render_options(lt.to_internal(lt.QUESTIONS["dealbreaker"]))
        self.assertEqual(deal, ["false: no such requirement is stated in the posting",
                                "true: the posting states one of these requirements"])

    def test_states_lengths_and_targets_per_question(self):
        rows = [qrow(1, "notify", role="ai", deal="Security clearance", score=7), qrow(2, role="mobile")]
        settings = lt.Settings()
        role = lt.build_question_items(rows, "the candidate", FakeTok(), settings, "stack_role")
        deal = lt.build_question_items(rows, "the candidate", FakeTok(), settings, "dealbreaker")
        fit = lt.build_question_items(rows, "the candidate", FakeTok(), settings, "fit")
        self.assertEqual([(it["qtype"], it["label"], it["qid"]) for it in role], [(0, 3, "stack_role"), (0, 6, "stack_role")])
        self.assertEqual(role[0]["target"], [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0])
        self.assertEqual([(it["qtype"], it["target"]) for it in deal], [(2, [0.0, 1.0]), (2, [1.0, 0.0])])
        self.assertEqual([(it["qtype"], it["label"], it["gold"]) for it in fit], [(1, 6, "notify"), (1, 2, "skip")])
        np.testing.assert_allclose(fit[0]["target"], [0, 0, 0, 0, 0.000264, 0.106451, 0.786571, 0.106451, 0.000264, 0],
                                   atol=1e-6)
        self.assertAlmostEqual(sum(fit[0]["target"]), 1.0)
        states = [(list(state), q["t"], max_len, head) for state, q, max_len, head in self.calls]
        self.assertEqual(states[0], (["job"], "choice", 1024, 256))
        self.assertEqual(states[2], (["job"], "noul", 1024, 256))
        self.assertEqual(states[4], (["candidate", "job"], "score", 2048, 256))
        self.assertEqual(self.calls[4][0]["candidate"], "the candidate")
        self.assertEqual(self.calls[4][1], lt.to_internal(lt.QUESTIONS["fit"]))

    def test_options_cut_by_head_max_len_are_an_error(self):
        with unittest.mock.patch.object(lt, "build_sequence", lambda *a: (list(range(30)), [10, 11])):
            with self.assertRaises(ValueError):
                lt.build_question_items([qrow(1)], "the candidate", FakeTok(), lt.Settings(), "fit")

    def test_smoothed_target_at_the_edges(self):
        np.testing.assert_allclose(lt.smoothed_target(0, 10, 0.5)[:3], [0.880537, 0.119168, 0.000295], atol=1e-6)


class TestQuestionSampling(unittest.TestCase):
    def rows(self):
        rows = ([qrow(i, "notify", role="backend") for i in range(40)]
                + [qrow(100 + i, "log", role="ai") for i in range(60)]
                + [qrow(200 + i, "skip", role="data") for i in range(100)]
                + [qrow(400 + i, "skip", role="infra", known=i >= 10) for i in range(200)])
        return rows

    def test_each_question_gets_its_rows(self):
        out = lt.sample_questions(self.rows(), lt.Settings())
        self.assertEqual(len(out["stack_role"]), 400)
        self.assertEqual(len(out["dealbreaker"]), 390)
        # fit: 200 gated + 50 of 200 gate-failing; labels 40 / 60 / 150 capped at 40 + 50
        self.assertEqual(Counter(r["label"] for r in out["fit"]), {"notify": 40, "log": 60, "skip": 90})

    def test_same_seed_same_sample(self):
        a = lt.sample_questions(self.rows(), lt.Settings())["fit"]
        b = lt.sample_questions(self.rows(), lt.Settings())["fit"]
        self.assertEqual([r["job_id"] for r in a], [r["job_id"] for r in b])

    def test_class_counts_name_the_options(self):
        items = {"stack_role": [{"label": 3}, {"label": 3}, {"label": 0}], "dealbreaker": [{"label": 1}],
                 "fit": [{"label": 6}]}
        self.assertEqual(lt.class_counts(items), {"stack_role": {"backend": 1, "ai": 2}, "dealbreaker": {"true": 1},
                                                  "fit": {"7": 1}})


def one_hot_logits(index, k, scale=10.0):
    z = np.zeros(k)
    z[index] = scale
    return z


class TestSummaries(unittest.TestCase):
    def test_expected_score_and_p_good_use_zero_based_levels(self):
        uniform = np.full(10, 0.1)
        level6 = np.eye(10)[6]                      # all mass on laya level 6 = score 7
        np.testing.assert_allclose(lt.summarise([uniform, level6], "expected"), [5.5, 7.0])
        np.testing.assert_allclose(lt.summarise([uniform, level6], "p_good"), [0.4, 1.0])
        with self.assertRaises(ValueError):
            lt.summarise([uniform], "median")

    def test_answers_apply_bucket_temperatures(self):
        logits = {"stack_role": [one_hot_logits(3, 7, 2.0)], "dealbreaker": [np.array([0.0, 2.0])],
                  "fit": [np.zeros(10)]}
        a = lt.answers_from_logits(logits, {"noul:2": 2.0})
        self.assertEqual(a["role"], ["ai"])
        self.assertAlmostEqual(float(a["deal_p"][0]), 1 / (1 + np.exp(-1)), places=6)
        np.testing.assert_allclose(a["fit_p"][0], np.full(10, 0.1))

    def test_gates(self):
        self.assertEqual(list(lt.gate_mask(["backend", "infra", "ai", "data"], [0.1, 0.1, 0.5, 0.49])),
                         [True, False, False, True])


class TestGatedCutoff(unittest.TestCase):
    s = np.array([9, 8.5, 8, 7.5, 7, 6, 5, 4, 9.5, 3])
    gated = np.array([1, 1, 1, 1, 1, 1, 1, 1, 0, 1], dtype=bool)
    y = np.array([1, 0, 1, 1, 0, 0, 1, 0, 1, 0])          # 5 good jobs; job 8 is removed by a gate

    def test_gate_removed_good_jobs_still_count_as_missed(self):
        c = lt.pick_gated_cutoff(self.s, self.gated, self.y, 0.5)
        self.assertEqual((c.threshold, c.precision, c.recall, c.alerts, c.met_rule), (7.5, 0.75, 0.6, 4, True))
        no_gates = lt.pick_gated_cutoff(self.s, np.ones(10, dtype=bool), self.y, 0.5)
        self.assertEqual((no_gates.threshold, no_gates.precision, no_gates.alerts), (7.5, 0.8, 5))

    def test_rule_not_met_is_flagged(self):
        c = lt.pick_gated_cutoff(self.s, self.gated, self.y, 0.9)      # gated jobs can catch at most 4 of 5
        self.assertFalse(c.met_rule)
        self.assertEqual((c.threshold, c.alerts), (5.0, 7))

    def test_no_gated_job(self):
        c = lt.pick_gated_cutoff(self.s, np.zeros(10, dtype=bool), self.y, 0.5)
        self.assertEqual((c.threshold, c.alerts, c.met_rule), (9.5, 0, False))

    def test_nearby_table(self):
        table = lt.gated_cutoff_table(self.s, self.gated, self.y, 7.5, 0.25)
        self.assertEqual([t for t, *_ in table], [6.75, 7.0, 7.25, 7.5, 7.75, 8.0, 8.25])
        self.assertEqual(table[3], (7.5, 0.75, 0.6, 4))

    def test_summary_choice_by_average_precision(self):
        # job 0 (good): half its mass on level 9, half on level 0 -> E 5.5, P(good) 0.5
        # job 1 (not good): all mass on level 5 -> E 6.0, P(good) 0.0
        fit_p = np.array([[0.5] + [0.0] * 8 + [0.5], np.eye(10)[5]])
        name, aps = lt.choose_summary(fit_p, np.array([True, True]), np.array([1, 0]))
        self.assertEqual((name, aps), ("p_good", {"expected": 0.5, "p_good": 1.0}))

    def test_decisions(self):
        out = lt.decide([True, True, True, False], [8.0, 6.0, 4.0, 9.0], 7.0, 5.0)
        self.assertEqual(list(out), ["notify", "log", "skip", "skip"])

    def test_decision_from_laya_answers(self):
        training = {"gates": lt.GATES, "cutoffs": {"summary": "expected", "alert_at": 7.0, "save_at": 5.0}}
        fit = {"probabilities": {str(i): (1.0 if i == 7 else 0.0) for i in range(10)}}
        good = {"stack_role": {"choice": "backend"}, "dealbreaker": {"noul": 0.1}, "fit": fit}
        self.assertEqual(lt.decide_from_answers(good, training), ("notify", 8.0))
        gated_out = {"stack_role": {"choice": "infra"}, "dealbreaker": {"noul": 0.1}}
        self.assertEqual(lt.decide_from_answers(gated_out, training), ("skip", None))
        p_good = {**training, "cutoffs": {"summary": "p_good", "alert_at": 0.6, "save_at": 0.3}}
        self.assertEqual(lt.decide_from_answers(good, p_good), ("notify", 1.0))

    def test_exported_cutoffs_round_down(self):
        self.assertEqual([lt.floor4(v) for v in (7.998865, 0.61239, 0.0003)], [7.9988, 0.6123, 0.0003])

    def test_wilson_matches_the_live_baseline_range(self):
        lo, hi = lt.wilson(18, 43)
        self.assertEqual((round(lo, 3), round(hi, 3)), (0.284, 0.567))
        self.assertEqual(lt.wilson(0, 0), (0.0, 0.0))


def item(qid, label, n_ids=12, gold="skip"):
    k = len(lt.OPTION_NAMES[qid])
    target = lt.smoothed_target(label, k, 0.5) if qid == "fit" else [float(i == label) for i in range(k)]
    return {"ids": list(range(1, n_ids + 1)), "markers": list(range(1, k + 1)),
            "qtype": {"stack_role": 0, "fit": 1, "dealbreaker": 2}[qid], "target": target, "label": label,
            "qid": qid, "gold": gold, "job_id": f"{qid}{label}", "cut": False}


def tiny_model():
    import torch

    class Tiny(torch.nn.Module):
        """Stands in for Laya's DecisionModel: one logit per option marker."""
        def __init__(self):
            super().__init__()
            self.encoder = torch.nn.Embedding(64, 8)
            self.scorer = torch.nn.Linear(8, 1)

        def forward(self, ids, att, mpos, mmask, qtype):
            h = self.encoder(ids)
            m = torch.gather(h, 1, mpos[:, :, None].expand(-1, -1, h.size(-1)))
            return self.scorer(m).squeeze(-1).masked_fill(~mmask, -1e4), None
    torch.manual_seed(0)
    return Tiny()


class TestQuestionLoss(unittest.TestCase):
    @needs("torch")
    def test_advantage_floors_a_narrow_row_at_the_batch_std(self):
        import torch
        # column 0: a near-saturated row (tiny spread, e.g. a confident, correct dealbreaker row); column 1: a row
        # with wide spread. Both get the same rows x group shape; the narrow row's own std (1.41) is floored at the
        # whole batch's std (13.24), so its advantage stays well under unit scale instead of blowing up to it, while
        # the wide row's own std (14.14) already exceeds the batch std and its advantage stays ~unit scale.
        r = torch.tensor([[1.0, 10.0], [3.0, 30.0]])
        out = lt.advantage(r).numpy()
        np.testing.assert_allclose(out, [[-0.0755, -0.7071], [0.0755, 0.7071]], atol=1e-4)
        self.assertLess(abs(out[0, 0]), 0.5)
        self.assertGreater(abs(out[0, 1]), 0.5)

    @needs("torch", "laya")
    def test_mixed_batch_and_question_weights(self):
        import torch
        from laya.common import collate_items
        batch = collate_items([[item("dealbreaker", 1), item("stack_role", 3), item("fit", 6)]], 0)
        logits = torch.randn(3, 10, generator=torch.Generator().manual_seed(1)).masked_fill(~batch["marker_mask"], -1e4)
        logits.requires_grad_(True)
        torch.manual_seed(0)
        loss, rows = lt.question_loss(logits, batch, lt.Settings(), 0.4, torch.device("cpu"))
        self.assertEqual(tuple(rows.shape), (3,))
        self.assertTrue(torch.isfinite(loss))
        self.assertAlmostEqual(float(loss.detach()), float(rows.sum() / 3), places=5)
        loss.backward()
        self.assertTrue(torch.isfinite(logits.grad).all())
        torch.manual_seed(0)
        settings = lt.Settings(question_weights={"stack_role": 1.0, "dealbreaker": 1.0, "fit": 0.0})
        loss0, rows0 = lt.question_loss(logits, batch, settings, 0.4, torch.device("cpu"))
        np.testing.assert_allclose(rows0.numpy(), rows.numpy(), rtol=1e-6)
        self.assertAlmostEqual(float(loss0.detach()), float(rows[:2].sum() / 3), places=5)

    @needs("torch", "laya")
    def test_one_step_raises_the_true_option_probability(self):
        import torch
        from laya.common import collate_items
        batch = collate_items([[item("dealbreaker", 1), item("stack_role", 3)]], 0)
        logits = torch.nn.Parameter(torch.zeros(2, batch["marker_mask"].shape[1]))

        def true_probs():
            p = torch.softmax(logits.detach().masked_fill(~batch["marker_mask"], -1e4), -1)
            return p[torch.arange(len(batch["label"])), batch["label"]]
        before = true_probs()
        torch.manual_seed(0)
        loss, _ = lt.question_loss(logits, batch, lt.Settings(), 0.4, torch.device("cpu"))
        loss.backward()
        with torch.no_grad():
            logits -= 0.5 * logits.grad
        self.assertTrue((true_probs() > before).all())


class TestQuestionTraining(unittest.TestCase):
    @needs("torch", "laya")
    def test_predictions_keep_every_option(self):
        import torch

        class Echo:
            """Returns each marker's position as its logit."""
            def eval(self):
                pass

            def __call__(self, ids, att, mpos, mmask, qtype):
                return mpos.float().masked_fill(~mmask, -1e4), None
        items = [item("dealbreaker", 0), item("stack_role", 2), item("fit", 4)]
        out = lt.predict_question_logits(Echo(), items, lt.Settings(micro_batch=1), torch.device("cpu"))
        self.assertEqual([len(o) for o in out], [2, 7, 10])
        np.testing.assert_allclose(out[2], np.arange(1, 11))

    def test_select_cutoff_runs_the_whole_decision(self):
        rows = [qrow(0, "notify"), qrow(1, "notify"), qrow(2, "skip"), qrow(3, "notify")]
        logits = {"stack_role": [one_hot_logits(i, 7) for i in (0, 3, 4, 5)],           # backend, ai, data, infra
                  "dealbreaker": [np.array([5.0, 0.0])] * 4,
                  "fit": [one_hot_logits(i, 10) for i in (8, 7, 6, 9)]}
        c = lt.select_cutoff(logits, rows, lt.Settings())
        # infra removes job 3; at E ~ 8 two of three good jobs alert and both are good
        self.assertEqual((c.alerts, c.precision, c.met_rule), (2, 1.0, True))
        self.assertAlmostEqual(c.recall, 2 / 3)

    @needs("torch", "laya")
    def test_train_loop_runs_on_a_tiny_model(self):
        import torch
        train_items = [item("stack_role", i % 7) for i in range(8)] + [item("dealbreaker", i % 2) for i in range(8)] \
            + [item("fit", i % 10, n_ids=20) for i in range(8)]
        select_rows = [qrow(i, "notify" if i < 2 else "skip") for i in range(4)]
        select_items = {"stack_role": [item("stack_role", 0) for _ in range(4)],
                        "dealbreaker": [item("dealbreaker", 0) for _ in range(4)],
                        "fit": [item("fit", 7) for _ in range(4)]}
        seen = []
        settings = lt.Settings(epochs=2, patience=5, micro_batch=4, grad_accum=1)
        result = lt.train_questions(tiny_model(), train_items, select_items, select_rows, settings,
                                    torch.device("cpu"), log=lambda s: None, on_best=seen.append)
        self.assertEqual(len(result.history), 2)
        self.assertEqual(sorted(result.history[0]["loss_by_question"]), ["dealbreaker", "fit", "stack_role"])
        self.assertTrue(seen and seen[0].epoch == 1)
        self.assertIn("encoder.weight", result.state)


# Eight select jobs with a known answer: (stack role, dealbreaker predicted, fit score, label).
SELECT = [("backend", False, 9, "notify"), ("ai", False, 8, "notify"), ("data", False, 7, "skip"),
          ("fullstack", False, 6, "log"), ("backend", False, 5, "log"), ("infra", False, 9, "notify"),
          ("backend", True, 9, "notify"), ("ai", False, 2, "skip")]


def select_case():
    rows = [qrow(i, label, role=role, deal="security clearance" if deal else None, score=score)
            for i, (role, deal, score, label) in enumerate(SELECT)]
    sel = {"stack_role": [one_hot_logits(lt.STACK_ROLES.index(role), 7) for role, *_ in SELECT],
           "dealbreaker": [np.array([0.0, 10.0]) if deal else np.array([10.0, 0.0]) for _, deal, *_ in SELECT],
           "fit": [one_hot_logits(score - 1, 10) for _, _, score, _ in SELECT]}
    # all-zero calibrate logits leave every temperature at exactly 1.0
    cal = {"stack_role": [np.zeros(7)] * 4, "dealbreaker": [np.zeros(2)] * 4, "fit": [np.zeros(10)] * 4}
    cal_rows = [qrow(100 + i, "skip") for i in range(4)]
    return rows, sel, cal, cal_rows


class TestCalibration(unittest.TestCase):
    def test_one_temperature_per_bucket(self):
        rng = np.random.default_rng(0)
        y = rng.integers(0, 2, 400)
        noul = np.stack([np.zeros(400), (np.where(y == 1, 1.0, -1.0) + rng.normal(0, 1, 400)) * 4], axis=1)
        role_y = rng.integers(0, 7, 400)
        choice = np.eye(7)[role_y]                                   # always right: sharpen as far as allowed
        logits = {"stack_role": list(choice), "dealbreaker": list(noul), "fit": [np.zeros(10)] * 400}
        labels = {"stack_role": role_y, "dealbreaker": y, "fit": rng.integers(0, 10, 400)}
        t = lt.fit_bucket_temperatures(logits, labels)
        self.assertEqual(sorted(t), ["choice:6-10", "noul:2", "score:6-10"])
        self.assertEqual((t["choice:6-10"], t["score:6-10"]), (0.5, 1.0))
        self.assertTrue(1.6 < t["noul:2"] < 2.4, t)

    def test_question_metrics(self):
        rows = [qrow(1, role="backend", deal=None, score=8), qrow(2, role="ai", deal="security clearance", score=3),
                qrow(3, role="backend", deal="5+ required years", score=5)]
        answers = {"role": ["backend", "backend", "backend"], "deal_p": np.array([0.1, 0.9, 0.7]),
                   "fit_p": np.stack([np.eye(10)[7], np.eye(10)[2], np.eye(10)[6]])}
        m = lt.question_metrics(answers, rows)
        self.assertAlmostEqual(m["stack_role"]["accuracy"], 2 / 3)
        self.assertEqual(m["stack_role"]["confusion"], {"backend": {"backend": 2}, "ai": {"backend": 1}})
        self.assertEqual((m["dealbreaker"]["precision"], m["dealbreaker"]["recall"]), (0.5, 1.0))
        self.assertAlmostEqual(m["fit"]["mae"], 2 / 3)
        self.assertAlmostEqual(m["fit"]["spearman"], 1.0)

    def test_question_metrics_leaves_out_unknown_dealbreaker_rows(self):
        # Deviation (controller-requested): row 3 has dealbreaker_known False and a wrong dealbreaker prediction
        # (truth is "no dealbreaker" but deal_p says 0.9). Included, it would add a false positive and drag
        # precision from 1.0 to 0.5; it must be left out of the dealbreaker metrics even though its prediction is
        # still produced (gating still needs it).
        rows = [qrow(1, role="backend", deal=None, score=8),
                qrow(2, role="backend", deal="security clearance", score=3),
                qrow(3, role="backend", deal=None, score=3, known=False)]
        answers = {"role": ["backend", "backend", "backend"], "deal_p": np.array([0.1, 0.9, 0.9]),
                   "fit_p": np.stack([np.eye(10)[7], np.eye(10)[2], np.eye(10)[2]])}
        m = lt.question_metrics(answers, rows)
        self.assertEqual((m["dealbreaker"]["precision"], m["dealbreaker"]["recall"], m["dealbreaker"]["accuracy"]),
                         (1.0, 1.0, 1.0))
        # stack_role and fit still see all three rows
        self.assertEqual(sum(sum(v.values()) for v in m["stack_role"]["confusion"].values()), 3)

    def test_question_metrics_is_none_not_nan_when_no_dealbreaker_is_known(self):
        rows = [qrow(1, role="backend", deal=None, score=8, known=False),
                qrow(2, role="backend", deal="security clearance", score=3, known=False)]
        answers = {"role": ["backend", "backend"], "deal_p": np.array([0.1, 0.9]),
                   "fit_p": np.stack([np.eye(10)[7], np.eye(10)[2]])}
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            m = lt.question_metrics(answers, rows)
        self.assertEqual((m["dealbreaker"]["accuracy"], m["dealbreaker"]["majority"], m["dealbreaker"]["precision"],
                         m["dealbreaker"]["recall"]), (None, None, None, None))

    def test_build_questions_report_fits_the_dealbreaker_temperature_without_unknown_rows(self):
        # Same calibrate data as test_one_temperature_per_bucket, but with extra dealbreaker_known=False rows whose
        # dealbreaker logits/labels are reversed (would badly skew the fit if counted). The other buckets' arrays
        # keep their own, unfiltered length.
        rng = np.random.default_rng(0)
        y = rng.integers(0, 2, 400)
        noul = np.stack([np.zeros(400), (np.where(y == 1, 1.0, -1.0) + rng.normal(0, 1, 400)) * 4], axis=1)
        role_y = rng.integers(0, 7, 400)
        choice = np.eye(7)[role_y]
        fit_y = rng.integers(0, 10, 400)
        noise = np.stack([np.zeros(40), -(np.where(y[:40] == 1, 1.0, -1.0)) * 4], axis=1)   # reversed, would skew
        logits = {"stack_role": list(choice) + list(np.eye(7)[role_y[:40]]), "dealbreaker": list(noul) + list(noise),
                  "fit": [np.zeros(10)] * 440}
        rows = ([qrow(i, "skip", role=lt.STACK_ROLES[role_y[i]], deal="security clearance" if y[i] else None,
                      score=fit_y[i] + 1) for i in range(400)]
                + [qrow(400 + i, "skip", role=lt.STACK_ROLES[role_y[i]], deal="security clearance" if y[i] else None,
                       score=fit_y[i] + 1, known=False) for i in range(40)])
        select_rows, sel, cal, _ = select_case()
        report, temps, _ = lt.build_questions_report(sel, logits, select_rows, rows, lt.Settings(), {"base": "x"})
        self.assertTrue(1.6 < temps["noul:2"] < 2.4, temps)


class TestQuestionsReport(unittest.TestCase):
    def test_end_to_end_selection_has_the_known_answer(self):
        rows, sel, cal, cal_rows = select_case()
        report, temps, cutoffs = lt.build_questions_report(sel, cal, rows, cal_rows, lt.Settings(), {"base": "typed-decisions"})
        self.assertEqual(temps, {"choice:6-10": 1.0, "noul:2": 1.0, "score:6-10": 1.0})
        self.assertEqual(cutoffs["summary"], "expected")
        self.assertAlmostEqual(cutoffs["alert_at"], 8.0, places=2)
        self.assertAlmostEqual(cutoffs["save_at"], 5.0, places=2)
        e = report["summaries"]["expected"]
        self.assertEqual((e["select"]["alerts"], e["select"]["precision"], e["select"]["recall"]), (2, 1.0, 0.5))
        # without gates the best cut-off is E ~ 9: jobs 0, 5 and 6 (all good; 5 and 6 are gated out above)
        self.assertEqual((e["no_gates"]["alerts"], e["no_gates"]["precision"], e["no_gates"]["recall"]), (3, 1.0, 0.75))
        # P(good) is ~1 for scores 7, 8 and 9 alike, so it cannot rank the skip at 7 below the good jobs
        self.assertEqual(e["average_precision"], 1.0)
        self.assertLess(report["summaries"]["p_good"]["average_precision"], 1.0)
        self.assertEqual((report["gated_jobs"], report["good_jobs"], round(report["base_rate"], 4)), (6, 4, 0.3333))
        self.assertEqual(report["confusion"]["notify"], {"notify": 2, "log": 0, "skip": 2})
        self.assertEqual(report["confusion"]["log"], {"notify": 0, "log": 2, "skip": 0})
        self.assertEqual(report["base"], "typed-decisions")
        json.dumps(report)                                         # plain JSON, ready for the checkpoint config

    def test_report_text_warns(self):
        rows, sel, cal, cal_rows = select_case()
        report, _, _ = lt.build_questions_report(sel, cal, rows, cal_rows, lt.Settings(), {"base": "typed-decisions"})
        healthy = lt.questions_report_text(report)
        self.assertNotIn("!!", healthy)
        self.assertIn("expected (chosen)", healthy)
        weak = json.loads(json.dumps(report))
        weak["base"] = "root laya (fallback: typed-decisions failed with OSError: gone)"
        weak["summaries"]["expected"]["select"].update(met_rule=False, precision=0.2)
        weak["summaries"]["expected"]["range"] = [6.0, 7.0]
        weak["questions"]["stack_role"].update(accuracy=0.6, majority=0.58)
        text = lt.questions_report_text(weak)
        for warning in ("!! Trained from the root laya checkpoint", "!! No cut-off caught half the good jobs",
                        "!! Notify alerts are no better than the gated base rate (20% against 33%)",
                        "!! The fit summary sits in a narrow band (6 to 7)",
                        "!! stack_role accuracy 60% is near its majority class (58%)"):
            self.assertIn(warning, text)


    def test_a_model_that_gates_out_every_job_still_reports(self):
        rows, sel, cal, cal_rows = select_case()
        sel["stack_role"] = [one_hot_logits(5, 7)] * len(rows)            # everything "infra"
        report, _, cutoffs = lt.build_questions_report(sel, cal, rows, cal_rows, lt.Settings(), {"base": "typed-decisions"})
        self.assertEqual(report["gated_jobs"], 0)
        self.assertTrue(np.isfinite(cutoffs["alert_at"]) and cutoffs["save_at"] < cutoffs["alert_at"])
        self.assertIn("!! No select job passes the predicted gates", lt.questions_report_text(report))

    def test_prints_best_epoch_when_present(self):
        rows, sel, cal, cal_rows = select_case()
        report, _, _ = lt.build_questions_report(sel, cal, rows, cal_rows, lt.Settings(),
                                                 {"base": "typed-decisions", "best_epoch": 3})
        self.assertIn("best_epoch: 3", lt.questions_report_text(report))


class TestQuestionsExport(unittest.TestCase):
    @needs("torch", "safetensors")
    def test_export_fields(self):
        import torch
        base = Path(tempfile.mkdtemp()); (base / "tokenizer").mkdir(); (base / "encoder").mkdir()
        out = Path(tempfile.mkdtemp()) / "ckpt"
        cfg = {"temperature_by_options": {"choice:3-5": 1.4, "noul:2": 1.98}, "max_len": 1024, "training": {"updates": 1}}
        temps = {"choice:6-10": 1.25, "noul:2": 0.9, "score:6-10": np.float64(1.5)}
        cutoffs = {"summary": "p_good", "alert_at": 0.61234, "save_at": 0.3}
        lt.export_questions_checkpoint({"w": torch.ones(2)}, str(base), cfg, out, lt.Settings(), temps, cutoffs,
                                       {"select": np.float32(0.5)})
        saved = json.loads((out / "rl_agent_config.json").read_text())
        self.assertEqual((saved["max_len"], saved["head_max_len"]), (2048, 256))
        self.assertEqual(saved["temperature_by_options"],
                         {"choice:3-5": 1.4, "noul:2": 0.9, "choice:6-10": 1.25, "score:6-10": 1.5})
        t = saved["training"]
        self.assertEqual(t["decision_method"], {"own": "questions"})
        self.assertEqual(t["questions"], lt.QUESTIONS)
        self.assertEqual(t["inputs"], {"stack_role": {"state": "job", "max_len": 1024},
                                       "dealbreaker": {"state": "job", "max_len": 1024},
                                       "fit": {"state": "candidate_job", "max_len": 2048}})
        self.assertEqual(t["gates"], {"stack_roles": ["backend", "fullstack", "ai", "data"], "dealbreaker_at": 0.5})
        self.assertEqual(t["cutoffs"], {"summary": "p_good", "alert_at": 0.6123, "save_at": 0.3})
        self.assertEqual(t["report"], {"select": 0.5})
        self.assertTrue((out / "model.safetensors").is_file())

    @needs("torch", "safetensors")
    def test_inherited_temperatures_outside_the_fitted_buckets_are_clamped(self):
        # choice:11+ is not a bucket we fit (none of our three questions has 11+ options): it is carried from the
        # base checkpoint as-is today, which is how an invalid/out-of-range value (laya's clamp range is [0.5, 5])
        # reaches Agent(out) and makes it warn on every load. It must be clamped at export instead.
        import torch
        base = Path(tempfile.mkdtemp()); (base / "tokenizer").mkdir(); (base / "encoder").mkdir()
        out = Path(tempfile.mkdtemp()) / "ckpt"
        cfg = {"temperature_by_options": {"choice:11+": 0.1006}, "max_len": 1024, "training": {}}
        temps = {"choice:6-10": 1.25, "noul:2": 0.9, "score:6-10": 1.5}
        cutoffs = {"summary": "p_good", "alert_at": 0.5, "save_at": 0.3}
        lt.export_questions_checkpoint({"w": torch.ones(2)}, str(base), cfg, out, lt.Settings(), temps, cutoffs, {})
        saved = json.loads((out / "rl_agent_config.json").read_text())
        self.assertEqual(saved["temperature_by_options"]["choice:11+"], 0.5)


class TestQuestionBase(unittest.TestCase):
    def test_typed_decisions_first(self):
        got = lt.load_question_base("cpu", log=lambda s: None, download=lambda sub: f"/hub/{sub or 'root'}",
                                    load=lambda path, device: ("model", "tok", {"path": path}))
        self.assertEqual(got, ("model", "tok", {"path": "/hub/typed-decisions"}, "/hub/typed-decisions", "typed-decisions"))

    def test_falls_back_to_the_root_checkpoint_and_says_so(self):
        logged = []

        def load(path, device):
            if path.endswith("typed-decisions"):
                raise RuntimeError("size mismatch")
            return "model", "tok", {"path": path}
        got = lt.load_question_base("cpu", log=logged.append, download=lambda sub: f"/hub/{sub or 'root'}", load=load)
        self.assertEqual(got[3], "/hub/root")
        self.assertEqual(got[4], "root laya (fallback: typed-decisions not found (RuntimeError))")
        self.assertTrue(logged[0].startswith("!! typed-decisions could not be loaded"))

    def test_fallback_note_and_log_never_carry_a_local_path(self):
        def load(path, device):
            if path.endswith("typed-decisions"):
                raise FileNotFoundError("/Users/someone/.cache/huggingface/hub/models--convaiinnovations--laya/"
                                        "snapshots/abc123/typed-decisions/model.safetensors not found")
            return "model", "tok", {"path": path}
        logged = []
        got = lt.load_question_base("cpu", log=logged.append, download=lambda sub: f"/hub/{sub or 'root'}", load=load)
        self.assertNotIn("/Users", got[4])
        self.assertIn("FileNotFoundError", got[4])
        self.assertNotIn("/Users", logged[0])


class TestBenchmarkArithmetic(unittest.TestCase):
    def test_alert_summary_alone_and_inside_the_hybrid(self):
        labels = {f"j{i}": ("notify" if i < 4 else "skip") for i in range(10)}
        decisions = {"j0": "notify", "j1": "notify", "j2": "notify", "j5": "notify", "j6": "notify", "j3": "log"}
        alone = lt.alert_summary(decisions, labels)
        self.assertEqual((alone["alerts"], alone["caught"], alone["worth"], alone["good"], alone["per100"]),
                         (5, 3, 0.6, 4, 50.0))
        hybrid = lt.alert_summary(decisions, labels, keep={"j0", "j1", "j5", "j6", "j3"})
        self.assertEqual((hybrid["alerts"], hybrid["caught"], hybrid["worth"], hybrid["per100"]), (4, 2, 0.5, 40.0))
        self.assertEqual(hybrid["good"], 4)      # j2 is good but outside keep: still counted among every good job
        lo, hi = lt.wilson(2, 4)
        self.assertEqual(hybrid["worth_range"], [round(lo, 3), round(hi, 3)])

    def test_go_needs_better_alerts_and_as_many_caught(self):
        baseline = {"alerts": 20, "caught": 8, "good": 16}                  # an invented current setup
        self.assertTrue(lt.go_no_go({"worth": 8 / 18, "caught": 8}, baseline))
        self.assertFalse(lt.go_no_go({"worth": 8 / 20, "caught": 8}, baseline))      # equal is not better
        self.assertFalse(lt.go_no_go({"worth": 4 / 8, "caught": 4}, baseline))       # better alerts, fewer good jobs
        self.assertFalse(lt.go_no_go({"worth": 0.0, "caught": 0}, baseline))

    def test_there_is_no_built_in_baseline(self):
        import inspect
        self.assertFalse(hasattr(lt, "BASELINE"))
        self.assertIs(inspect.signature(lt.go_no_go).parameters["baseline"].default, inspect.Parameter.empty)


@unittest.skipUnless(os.environ.get("LAYA_SLOW_TESTS"), "set LAYA_SLOW_TESTS=1 to load a real checkpoint")
class TestOneQuestionsTrainingStep(unittest.TestCase):
    def test_one_epoch_with_all_three_questions(self):
        import torch
        base = os.environ.get("LAYA_SLOW_BASE")
        if not base:
            self.skipTest("set LAYA_SLOW_BASE to a fine-tuned Laya checkpoint folder")
        device = torch.device("cpu")
        model, tok, _ = lt.load_base(base, device)
        settings = lt.Settings(job_max_len=320, max_len=384, epochs=1, micro_batch=2, grad_accum=1)
        rows = [qrow(1, "notify", role="ai"), qrow(2, "skip", role="infra", deal="security clearance")]
        # the real tokenizer: every question's options fit head_max_len 256, or this raises
        items = {qid: lt.build_question_items(rows, "the candidate", tok, settings, qid) for qid in lt.QUESTIONS}
        train_items = [it for its in items.values() for it in its]
        result = lt.train_questions(model, train_items, items, rows, settings, device, log=lambda s: None,
                                    pad_id=tok.pad_token_id)
        self.assertEqual(result.epoch, 1)
        self.assertEqual(sorted(result.history[0]["loss_by_question"]), ["dealbreaker", "fit", "stack_role"])
        out = lt.predict_question_logits(model, train_items, settings, device, tok.pad_token_id)
        self.assertEqual([len(o) for o in out], [7, 7, 2, 2, 10, 10])


@unittest.skipUnless(os.environ.get("LAYA_SLOW_TESTS"), "set LAYA_SLOW_TESTS=1 to load a real checkpoint")
class TestQuestionsExportRoundTrip(unittest.TestCase):
    def test_exported_checkpoint_answers_match_the_training_side_probabilities(self):
        """export_questions_checkpoint's weights and temperatures, reloaded through laya.agent.Agent, must answer
        identically to the training-side logits they were computed from (no training here: the base checkpoint is
        only read, then its own state_dict is round-tripped through the export)."""
        import torch
        from laya.agent import Agent
        base = os.environ.get("LAYA_SLOW_BASE")
        if not base:
            self.skipTest("set LAYA_SLOW_BASE to a fine-tuned Laya checkpoint folder")
        device = torch.device("cpu")
        model, tok, cfg = lt.load_base(base, device)
        settings = lt.Settings(job_max_len=320, max_len=384, epochs=1, micro_batch=2, grad_accum=1)
        resume = "the candidate"
        rows = [qrow(1, "notify", role="ai"), qrow(2, "skip", role="infra", deal="security clearance")]
        items = {qid: lt.build_question_items(rows, resume, tok, settings, qid) for qid in lt.QUESTIONS}
        logits = {qid: lt.predict_question_logits(model, its, settings, device, tok.pad_token_id)
                  for qid, its in items.items()}
        temps = {bucket: 1.0 for bucket in lt.BUCKETS.values()}       # compare at temperature 1.0, no fit involved
        cutoffs = {"summary": "expected", "alert_at": 7.0, "save_at": 5.0}
        before = sorted(str(p) for p in Path(base).rglob("*"))        # read-only: base must come out unchanged
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ckpt"
            lt.export_questions_checkpoint(model.state_dict(), base, cfg, out, settings, temps, cutoffs, {})
            agent = Agent(str(out), device="cpu")
            for i, row in enumerate(rows):
                for qid in lt.QUESTIONS:
                    spec = lt.question_inputs(settings)[qid]
                    state = lt.job_state(row) if spec["state"] == "job" else lt.alert_state(resume, row)
                    answer = agent.system_one(state, {qid: lt.QUESTIONS[qid]}, max_len=spec["max_len"],
                                              head_max_len=settings.head_max_len)["answers"][qid]
                    p_train = lt.softmax_rows(np.stack([logits[qid][i]]), temps[lt.BUCKETS[qid]])[0]
                    if qid == "dealbreaker":
                        self.assertAlmostEqual(answer["noul"], float(p_train[1]), delta=1e-3)
                    else:
                        keys = lt.OPTION_NAMES["stack_role"] if qid == "stack_role" else [str(k) for k in range(10)]
                        p_agent = np.array([answer["probabilities"][k] for k in keys])
                        np.testing.assert_allclose(p_agent, p_train, atol=1e-3)
        self.assertEqual(sorted(str(p) for p in Path(base).rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
