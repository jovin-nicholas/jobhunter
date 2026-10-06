import unittest

from jobhunter.tracker.numbers import fit_band, first_reply, reply_numbers


def app(applied, board="dice", resume="backend.txt", fit=None, *stages):
    events = [{"kind": "stage", "value": "applied", "at": applied}]
    events += [{"kind": "stage", "value": value, "at": at} for value, at in stages]
    return {"applied_at": applied, "board": board, "resume_id": resume, "fit_score": fit, "events": events}


class TestNumbers(unittest.TestCase):
    def test_a_reply_is_the_first_screen_or_later(self):
        self.assertEqual(first_reply(app("2026-09-01", "dice", "r", 7, ("screen", "2026-09-05"),
                                         ("interview", "2026-09-12"))), "2026-09-05")
        self.assertIsNone(first_reply(app("2026-09-01", "dice", "r", 7, ("rejected", "2026-09-03"))))

    def test_fit_bands(self):
        self.assertEqual([fit_band(s) for s in (None, 3, 4.99, 5, 6.9, 7, 9.5)],
                         [None, "<5", "<5", "5–7", "5–7", "≥7", "≥7"])

    def test_rates_counts_and_too_few(self):
        apps = [app("2026-09-01", "dice", "backend.txt", 7.5, ("screen", "2026-09-04")),
                app("2026-09-02", "dice", "backend.txt", 8.0, ("screen", "2026-09-12")),
                app("2026-09-02", "dice", "fullstack.txt", 4.0),
                app("2026-09-03", "greenhouse", "backend.txt", 6.0),
                app("2026-09-08", "greenhouse", "backend.txt", 6.5, ("rejected", "2026-09-09")),
                app("2026-09-09", "", "backend.txt", None)]
        n = reply_numbers(apps)
        self.assertEqual(n["overall"], {"n": 6, "replied": 2, "rate": 2 / 6, "few": False})
        boards = {r["label"]: (r["n"], r["replied"], r["few"]) for r in n["by_board"]}
        self.assertEqual(boards, {"dice": (3, 2, True), "greenhouse": (2, 0, True), "other": (1, 0, True)})
        self.assertEqual([(r["label"], r["n"], r["replied"]) for r in n["by_fit"]],
                         [("<5", 1, 0), ("5–7", 2, 0), ("≥7", 2, 2)])
        self.assertEqual([r["label"] for r in n["by_week"]], ["2026-W36", "2026-W37"])
        self.assertEqual((n["median_days_to_reply"], n["replies_counted"]), (6.5, 2))     # 3 and 10 days

    def test_no_applications(self):
        n = reply_numbers([])
        self.assertEqual(n["overall"], {"n": 0, "replied": 0, "rate": None, "few": True})
        self.assertIsNone(n["median_days_to_reply"])
