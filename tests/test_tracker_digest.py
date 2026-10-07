import unittest
from datetime import date

from jobhunter.settings import TrackerSettings
from jobhunter.tracker.digest import compose, due_items

TODAY = date(2026, 10, 7)


def app(i, stage="applied", applied="2026-10-01", follow_up=None, events=(), people="", notes="",
        url="https://jobs.example.com/1"):
    evs = [{"kind": "stage", "value": "applied", "at": applied, "created_at": applied + "T09:00:00+00:00"}]
    evs += [{"kind": "stage", "value": v, "at": at, "created_at": made + "T09:00:00+00:00"} for v, at, made in events]
    if follow_up:
        evs.append({"kind": "follow_up_set", "value": follow_up, "at": applied, "created_at": applied})
    return {"id": i, "company": f"Co{i}", "title": "Engineer", "url": url, "stage": stage, "stage_at": evs[-1]["at"],
            "applied_at": applied, "follow_up_at": follow_up, "people": people, "notes": notes, "events": evs}


class TestDueItems(unittest.TestCase):
    def test_groups(self):
        apps = [app(1, follow_up="2026-10-07"),                                       # follow up today
                app(2, "interview", events=[("interview", "2026-10-07", "2026-10-02")]),   # interview today
                app(3, follow_up="2026-10-03"),                                        # 4 days late
                app(4, "screen", events=[("screen", "2026-10-09", "2026-10-05")]),       # call this week
                app(5, "screen", events=[("screen", "2026-10-15", "2026-10-05")]),       # 8 days out: not this week
                app(6, "offer", events=[("offer", "2026-10-06", "2026-10-06")]),         # offers are never listed
                app(7, "rejected", follow_up="2026-10-07", events=[("rejected", "2026-10-06", "2026-10-06")]),
                app(8, applied="2026-08-01")]                                          # quiet for 30+ days
        items = due_items(apps, TrackerSettings(), TODAY)
        self.assertEqual([(e["id"], e["reason"], e["when"]) for e in items["today"]],
                         [(1, "follow up", "today"), (2, "interview", "today")])
        self.assertEqual([(e["id"], e["when"]) for e in items["overdue"]], [(3, "4 days late")])
        self.assertEqual([(e["id"], e["reason"], e["when"]) for e in items["this_week"]], [(4, "screen", "Fri Oct 9")])
        self.assertEqual(items["quiet"], 1)

    def test_a_new_application_is_due_its_default_follow_up(self):
        items = due_items([app(1, applied="2026-09-30")], TrackerSettings(), TODAY)          # 7 days ago
        self.assertEqual([e["id"] for e in items["today"]], [1])

    def test_a_call_logged_for_today_on_today_is_not_scheduled_ahead(self):
        items = due_items([app(1, "screen", events=[("screen", "2026-10-07", "2026-10-07")])], TrackerSettings(), TODAY)
        self.assertEqual(items["today"], [])

    def test_one_day_late_and_tomorrow_read_naturally(self):
        items = due_items([app(1, follow_up="2026-10-06"),
                           app(2, "screen", events=[("screen", "2026-10-08", "2026-10-01")])], TrackerSettings(), TODAY)
        self.assertEqual(items["overdue"][0]["when"], "1 day late")
        self.assertEqual(items["this_week"][0]["when"], "tomorrow")

    def test_quiet_days_reflects_settings(self):
        items = due_items([app(1, applied="2026-09-01")], TrackerSettings(ghost_after_days=21), TODAY)
        self.assertEqual(items["quiet_days"], 21)


class TestCompose(unittest.TestCase):
    def items(self, *apps, settings=None):
        return due_items(list(apps), settings or TrackerSettings(), TODAY)

    def test_nothing_due_today_gives_nothing_to_send(self):
        self.assertIsNone(compose(self.items(app(3, follow_up="2026-10-03"),
                                             app(4, "screen", events=[("screen", "2026-10-09", "2026-10-05")])), TODAY))

    def test_subject_and_entries(self):
        subject, plain, html = compose(self.items(
            app(1, follow_up="2026-10-07", people="Jane at Co1, recruiter", notes="Call back\nafter 2pm"),
            app(2, "interview", events=[("interview", "2026-10-07", "2026-10-02")]),
            app(3, follow_up="2026-10-03"),
            app(4, "screen", events=[("screen", "2026-10-09", "2026-10-05")]),
            app(5, "screen", events=[("screen", "2026-10-10", "2026-10-05")])), TODAY)
        self.assertEqual(subject, "[jobhunter] Due today: Co1, Co2 · 1 overdue · 2 calls this week")
        for text in ("Due today", "Overdue", "Calls this week", "People: Jane at Co1, recruiter", "Call back", "after 2pm",
                     "python -m jobhunter tracker"):
            self.assertIn(text, plain)
        self.assertIn('<a href="https://jobs.example.com/1">Engineer</a>', html)
        self.assertIn("Call back<br>after 2pm", html)
        self.assertNotIn("mailto:", html)                                   # no buttons
        self.assertEqual(html.count("<a "), 5)                              # only the five job links

    def test_markup_is_escaped_and_bad_links_dropped(self):
        bad = app(1, follow_up="2026-10-07", url="javascript:alert(1)", notes="<b>bold</b> & co")
        bad["title"] = "<script>x</script>"
        _, plain, html = compose(self.items(bad), TODAY)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; co", html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("<a ", html)

    def test_one_call_is_singular(self):
        subject, _, _ = compose(self.items(app(1, follow_up="2026-10-07"),
                                           app(4, "screen", events=[("screen", "2026-10-09", "2026-10-05")])), TODAY)
        self.assertEqual(subject, "[jobhunter] Due today: Co1 · 1 call this week")

    def test_footer_uses_configured_ghost_after_days(self):
        quiet_app = app(9, applied="2026-08-01")
        due_app = app(1, follow_up="2026-10-07")
        _, plain, html = compose(self.items(due_app, quiet_app, settings=TrackerSettings(ghost_after_days=21)), TODAY)
        self.assertIn("quiet for 21+ days", plain)
        self.assertIn("quiet for 21+ days", html)
