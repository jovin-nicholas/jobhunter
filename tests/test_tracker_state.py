import unittest
from datetime import date

from jobhunter.settings import TrackerSettings
from jobhunter.tracker.state import attention, build_state, follow_up_for

TODAY = date(2026, 10, 6)


def app(i, stage="applied", applied="2026-10-01", follow_up=None, events=()):
    evs = [{"kind": "stage", "value": "applied", "at": applied}] + [
        {"kind": "stage", "value": v, "at": at} for v, at in events]
    return {"id": i, "company": f"Co{i}", "title": "Engineer", "stage": stage, "stage_at": evs[-1]["at"],
            "applied_at": applied, "follow_up_at": follow_up, "board": "dice", "resume_id": "", "fit_score": None,
            "events": evs}


class TestAttention(unittest.TestCase):
    def test_reasons_in_priority_order(self):
        self.assertEqual(attention(app(1, "offer", events=[("offer", "2026-10-05")]), TODAY, 30)[:2], (0, "offer"))
        self.assertEqual(attention(app(2, "interview", events=[("interview", "2026-10-08")]), TODAY, 30),
                         (1, "interview", "2026-10-08"))
        self.assertEqual(attention(app(3, follow_up="2026-10-06"), TODAY, 30), (2, "follow up", "2026-10-06"))
        self.assertEqual(attention(app(4, applied="2026-09-01"), TODAY, 30)[:2], (3, "ghost?"))

    def test_nothing_due_and_closed_need_nothing(self):
        self.assertIsNone(attention(app(5, follow_up="2026-10-07"), TODAY, 30))
        self.assertIsNone(attention(app(6, "interview", events=[("interview", "2026-10-10")]), TODAY, 30))
        self.assertIsNone(attention(app(7, "rejected", applied="2026-08-01", follow_up="2026-08-08",
                                        events=[("rejected", "2026-08-20")]), TODAY, 30))

    def test_ghost_counts_from_the_last_event_not_the_application(self):
        self.assertIsNone(attention(app(8, applied="2026-08-01", follow_up=None,
                                        events=[("applied", "2026-09-20")]), TODAY, 30))


class TestBuildState(unittest.TestCase):
    def test_groups_order_and_counts(self):
        apps = [app(1), app(2, "offer", events=[("offer", "2026-10-05")]), app(3, applied="2026-09-20"),
                app(4, "rejected", events=[("rejected", "2026-10-03")]), app(5, follow_up="2026-10-01")]
        s = build_state(apps, [{"job_id": "dice_9"}], TrackerSettings(), TODAY)
        self.assertEqual([a["id"] for a in s["groups"]["needs"]], [2, 5])
        self.assertEqual([a["id"] for a in s["groups"]["waiting"]], [3, 1])          # quiet longest first
        self.assertEqual([a["id"] for a in s["groups"]["closed"]], [4])
        self.assertEqual(s["counts"]["applied"], 3)
        self.assertEqual(s["counts"]["offer"], 1)
        self.assertEqual(s["review"], [{"job_id": "dice_9"}])
        self.assertEqual(s["today"], "2026-10-06")
        self.assertEqual(s["groups"]["waiting"][0]["age_days"], 16)

    def test_follow_up_defaults(self):
        s = TrackerSettings()
        self.assertEqual(follow_up_for("applied", TODAY, s), "2026-10-13")
        self.assertEqual(follow_up_for("screen", TODAY, s), "2026-10-09")
        self.assertIsNone(follow_up_for("offer", TODAY, s))
        self.assertIsNone(follow_up_for("rejected", TODAY, s))
