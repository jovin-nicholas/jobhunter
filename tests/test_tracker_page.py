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

    def test_it_follows_the_colour_scheme_and_reduced_motion(self):
        self.assertIn("prefers-color-scheme: dark", self.text)
        self.assertIn("prefers-reduced-motion", self.text)
