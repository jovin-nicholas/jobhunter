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

    def test_after_a_review_action_the_cursor_stays_in_the_queue(self):
        body = re.search(r"async function review\(jobId, action\) \{(.*?)\n\}", self.text, re.S).group(1)
        self.assertNotIn("openDrawer", body)                              # no jump to the new application
        self.assertIn('v.type === "review" && v.job_id === next', body)   # the next row, found by its key

    def test_it_follows_the_colour_scheme_and_reduced_motion(self):
        self.assertIn("prefers-color-scheme: dark", self.text)
        self.assertIn("prefers-reduced-motion", self.text)

    def test_buttons_carry_no_key_letters(self):
        self.assertNotIn('el("kbd"', self.text)                           # shortcuts live in the ? dialog
        self.assertNotIn("numbers <kbd>", self.text)

    def test_the_add_box_is_labelled_and_has_an_add_button(self):
        self.assertIn('<label for="quick" class="sr-only"', self.text)     # one visible word: the Add button
        self.assertIn("Add application", self.text)
        self.assertIn('id="add-button"', self.text)
        self.assertIn('id="search-open"', self.text)

    def test_the_job_title_is_the_link_to_the_posting(self):
        self.assertIn('class: "title-link"', self.text)
        self.assertNotIn('"open")', self.text)                            # no separate "open" link per row

    def test_action_buttons_are_spaced(self):
        self.assertIn(".acts-row { display: flex; gap: 8px;", self.text)

    def test_a_missing_fit_says_why(self):
        self.assertIn("no fit score", self.text)

    def test_the_scaled_fit_keeps_the_raw_value_in_a_tooltip(self):
        self.assertIn("fit_raw", self.text)
        self.assertIn("function fitCell(row)", self.text)

    def test_the_drawer_eyebrow_shows_the_raw_fit_in_a_tooltip_when_it_differs(self):
        # The drawer's eyebrow line (board · fit · resume) must show the raw value the same way the table cells do
        # (fitCell): a tooltip, only when the scaled fit differs from the raw one.
        self.assertIn('title: "raw " + a.fit_raw', self.text)

    def test_search_also_filters_the_review_queue(self):
        self.assertIn("state.review.filter(matchesReview)", self.text)
        self.assertIn("No matches for", self.text)

    def test_applications_and_alerts_are_separate_pages(self):
        for view in ('id="view-apps"', 'id="view-alerts"', 'id="view-numbers"'):
            self.assertIn(view, self.text)
        self.assertIn('view === "alerts" ? renderAlerts()', self.text)
        self.assertNotIn("state.review", self.text.split("function renderApps()")[1].split("function renderAlerts()")[0])

    def test_the_stage_row_is_a_labelled_filter_shown_only_on_the_applications_page(self):
        self.assertIn('pipeline.hidden = strip.hidden = view !== "apps"', self.text)
        self.assertIn('"filter by stage"', self.text)

    def test_bad_matches_and_hidden_alerts_can_be_seen_and_restored(self):
        self.assertIn("state.dismissed", self.text)
        self.assertIn('"restore"', self.text)
        self.assertIn('"/api/review/undo"', self.text.split("async function restore(")[1].split("\n}")[0])

    def test_times_are_shown_instead_of_today(self):
        self.assertNotIn('"today" : `${days}d`', self.text)
        self.assertIn("function when(iso)", self.text)
        self.assertIn("when(r.alerted_at)", self.text)

    def test_a_duplicate_add_says_already_tracked(self):
        self.assertIn("Already tracked", self.text)

    def test_the_drawer_can_delete_an_application(self):
        self.assertIn("/delete`", self.text)
        self.assertIn("Delete application", self.text)
