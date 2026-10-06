import unittest
from datetime import date

from jobhunter.settings import TrackerSettings
from jobhunter.tracker.state import attention, build_state, follow_up_for

TODAY = date(2026, 10, 6)
S = TrackerSettings()


def app(i, stage="applied", applied="2026-10-01", follow_up=None, events=(), follow_ups=(), created=None):
    """`created` is when the events were recorded; by default the applied date, so later-dated stages are ahead."""
    made = f"{created or applied}T09:00:00+00:00"
    evs = [{"kind": "stage", "value": "applied", "at": applied, "created_at": made}] + [
        {"kind": "stage", "value": v, "at": at, "created_at": made} for v, at in events] + [
        {"kind": kind, "value": value, "at": applied, "created_at": made} for kind, value in follow_ups]
    return {"id": i, "company": f"Co{i}", "title": "Engineer", "stage": stage, "stage_at": evs[-1]["at"],
            "applied_at": applied, "follow_up_at": follow_up, "board": "dice", "resume_id": "", "fit_score": None,
            "events": evs}


class TestAttention(unittest.TestCase):
    def test_reasons_in_priority_order(self):
        self.assertEqual(attention(app(1, "offer", events=[("offer", "2026-10-05")]), TODAY, S)[:2], (0, "offer"))
        self.assertEqual(attention(app(2, "interview", events=[("interview", "2026-10-08")]), TODAY, S),
                         (1, "interview", "2026-10-08"))
        self.assertEqual(attention(app(3, follow_up="2026-10-06"), TODAY, S), (2, "follow up", "2026-10-06"))
        self.assertEqual(attention(app(4, applied="2026-08-01"), TODAY, S)[:2], (3, "ghost?"))

    def test_nothing_due_and_closed_need_nothing(self):
        self.assertIsNone(attention(app(5, follow_up="2026-10-07"), TODAY, S))
        self.assertIsNone(attention(app(6, "interview", events=[("interview", "2026-10-10")]), TODAY, S))
        self.assertIsNone(attention(app(7, "rejected", applied="2026-08-01", follow_up="2026-08-08",
                                        events=[("rejected", "2026-08-20")]), TODAY, S))

    def test_ghost_counts_from_the_last_event_not_the_application(self):
        self.assertIsNone(attention(app(8, applied="2026-08-01", follow_up=None,
                                        events=[("applied", "2026-09-20")]), TODAY, S))

    def test_only_a_screen_or_interview_scheduled_ahead_is_upcoming(self):
        ahead = app(14, "interview", events=[("interview", "2026-10-06")], created="2026-10-03")
        self.assertEqual(attention(ahead, TODAY, S), (1, "interview", "2026-10-06"))
        logged_today = app(15, "screen", applied="2026-10-02", events=[("screen", "2026-10-06")], created="2026-10-06")
        self.assertIsNone(attention(logged_today, TODAY, S))

    def test_a_new_application_is_due_a_follow_up_after_the_applied_days(self):
        self.assertEqual(attention(app(9, applied="2026-09-29"), TODAY, S), (2, "follow up", "2026-10-06"))
        self.assertIsNone(attention(app(10, applied="2026-09-30"), TODAY, S))           # 6 days: still waiting

    def test_an_explicit_follow_up_overrides_the_derived_one_and_done_clears_it(self):
        later = app(11, applied="2026-09-29", follow_up="2026-10-10", follow_ups=[("follow_up_set", "2026-10-10")])
        self.assertIsNone(attention(later, TODAY, S))
        done = app(12, applied="2026-09-29", follow_ups=[("follow_up_done", None)])
        self.assertIsNone(attention(done, TODAY, S))

    def test_an_old_application_with_no_follow_up_is_a_ghost_suggestion_not_a_follow_up(self):
        self.assertEqual(attention(app(13, applied="2026-08-15"), TODAY, S)[:2], (3, "ghost?"))  # due 08-22: 45d ago


class TestBuildState(unittest.TestCase):
    def test_groups_order_and_counts(self):
        apps = [app(1), app(2, "offer", events=[("offer", "2026-10-05")]), app(3, applied="2026-09-30"),
                app(4, "rejected", events=[("rejected", "2026-10-03")]), app(5, follow_up="2026-10-01")]
        s = build_state(apps, [{"job_id": "dice_9"}], TrackerSettings(), TODAY)
        self.assertEqual([a["id"] for a in s["groups"]["needs"]], [2, 5])
        self.assertEqual([a["id"] for a in s["groups"]["waiting"]], [3, 1])          # quiet longest first
        self.assertEqual([a["id"] for a in s["groups"]["closed"]], [4])
        self.assertEqual(s["counts"]["applied"], 3)
        self.assertEqual(s["counts"]["offer"], 1)
        self.assertEqual(s["review"], [{"job_id": "dice_9"}])
        self.assertEqual(s["today"], "2026-10-06")
        self.assertEqual(s["groups"]["waiting"][0]["age_days"], 6)

    def test_needs_you_mixes_all_four_priorities_in_order(self):
        apps = [app(1, applied="2026-08-01"),                                              # ghost?
                app(2, follow_up="2026-10-04", follow_ups=[("follow_up_set", "2026-10-04")]),  # follow up
                app(3, applied="2026-09-29"),                                              # derived follow up
                app(4, "interview", events=[("interview", "2026-10-07")]),
                app(5, "offer", events=[("offer", "2026-10-05")]),
                app(6)]                                                                    # waiting
        s = build_state(apps, [], S, TODAY)
        self.assertEqual([(a["id"], a["reason"]) for a in s["groups"]["needs"]],
                         [(5, "offer"), (4, "interview"), (2, "follow up"), (3, "follow up"), (1, "ghost?")])
        self.assertEqual([a["id"] for a in s["groups"]["waiting"]], [6])

    def test_follow_up_defaults(self):
        s = TrackerSettings()
        self.assertEqual(follow_up_for("applied", TODAY, s), "2026-10-13")
        self.assertEqual(follow_up_for("screen", TODAY, s), "2026-10-09")
        self.assertIsNone(follow_up_for("offer", TODAY, s))
        self.assertIsNone(follow_up_for("rejected", TODAY, s))
