import re
import unittest
from pathlib import Path

import jobhunter.tracker

PAGE = Path(jobhunter.tracker.__file__).with_name("page.html")


class TestPage(unittest.TestCase):
    def setUp(self):
        self.text = PAGE.read_text(encoding="utf-8")

    def test_job_text_is_never_parsed_as_html(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(sink, self.text)

    def test_nothing_is_loaded_from_the_web(self):
        self.assertEqual(re.findall(r"""(?:src|href)\s*=\s*["']?https?://""", self.text), [])
        self.assertNotIn("@import", self.text)
        self.assertNotIn("fonts.googleapis", self.text)

    def test_every_api_call_goes_through_one_helper_that_sends_the_token(self):
        self.assertEqual(self.text.count("fetch("), 1)
        self.assertIn('"X-Tracker-Token"', self.text)

    def test_links_from_job_data_are_checked(self):
        self.assertIn("function safeHref", self.text)
        uses = re.findall(r"href:", self.text)
        self.assertTrue(uses)
        self.assertEqual(len(re.findall(r"href:\s*safeHref\(", self.text)), len(uses))

    def test_a_stage_can_be_dated(self):
        self.assertIn('id: "stage-on"', self.text)                     # the drawer's optional "on" date
        self.assertRegex(self.text, r"function setStage\(id, stage\) \{\s*const on = document.getElementById\(\"stage-on\"\)")

    def test_date_inputs_save_on_blur_and_only_a_full_date_from_2000(self):
        self.assertIn(r"/^\d{4}-\d{2}-\d{2}$/", self.text)
        self.assertIn('Number(v.slice(0, 4)) >= 2000', self.text)
        self.assertNotIn('type: "date", id: "follow-up", value: a.follow_up_at || "",\n    on: {change', self.text)
        self.assertTrue(re.search(r'id: "follow-up"[^\n]*\n\s*on: \{blur:', self.text), "follow-up saves on blur")
        self.assertIn('kind === "date" ? "blur" : "change"', self.text)

    def test_a_review_action_can_be_undone(self):
        self.assertIn('"/api/review/undo"', self.text)

    def test_back_closes_the_drawer_and_closing_does_not_pile_up_history(self):
        self.assertIn('history.pushState({drawer: id}, "")', self.text)
        self.assertRegex(self.text, r'function closeDrawer\(\) \{[^}]*history\.state[^}]*history\.back\(\)')
        self.assertNotIn('addEventListener("popstate", closeDrawer)', self.text)   # back must not call back again
        self.assertNotRegex(self.text, r"openId = (a\.id|v\.id|t|r\.id);")          # every open goes through openDrawer
        self.assertEqual(self.text.count("openId = id;"), 1)

    def test_it_follows_the_colour_scheme_and_reduced_motion(self):
        self.assertIn("prefers-color-scheme: dark", self.text)
        self.assertIn("prefers-reduced-motion", self.text)
