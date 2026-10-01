import json
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from jobhunter.boards.scraped_ats import AdpBoard, DoverBoard, GemBoard, WorkdayBoard, read_posting
from jobhunter.models import SearchContext
from tests.helpers import FakeResponse
from tests.test_ats_boards import FakeDiscovery

RECENT = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
LD = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Software Engineer I",
      "description": "&lt;p&gt;Build &lt;b&gt;APIs&lt;/b&gt;.&lt;/p&gt;", "datePosted": RECENT,
      "jobLocation": {"@type": "Place", "address": {"addressLocality": "Austin", "addressRegion": "TX",
                                                    "addressCountry": "US"}}}
WORKDAY_PAGE = (f"<html><head><title>Software Engineer I | Gamma</title>"
                f"<script type='application/ld+json'>{json.dumps(LD)}</script></head><body>menu</body></html>")
DOVER_PAGE = ("<html><body><h1>Backend Engineer</h1><nav>Menu</nav><div class='job-description'>"
              "<p>You will build Python services. Requirements: 2 years.</p></div></body></html>")
WD_URL = "https://gamma.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Software-Engineer_R123"
ADP_URL = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
           "?cid=c0ffee&jobId=515151&lang=en_US")


class Pages:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def request(self, method, url, **kw):
        self.calls.append((url, kw))
        page = self.pages.get(url)
        return FakeResponse(text=page) if page is not None else FakeResponse(status_code=404, text="")


def ctx(http, urls, known=(), logs=None, stale=None, gone=None):
    return SearchContext(["swe"], ["Remote"], 72, http, (logs if logs is not None else []).append,
                         discovery=FakeDiscovery(urls), is_known=lambda job_id: job_id in known,
                         mark_stale=(stale if stale is not None else []).append,
                         mark_gone=(gone if gone is not None else []).append)


class TestReadPosting(unittest.TestCase):
    def test_json_ld(self):
        info = read_posting(WORKDAY_PAGE)
        self.assertEqual(info, {"title": "Software Engineer I", "description": "Build APIs.",
                                "location": "Austin, TX, US", "posted_at": RECENT, "has_job": True})

    def test_a_page_title_alone_is_not_a_job(self):
        self.assertFalse(read_posting("<html><head><title>Careers | Acme</title></head><body></body></html>")["has_job"])

    def test_without_json_ld(self):
        info = read_posting(DOVER_PAGE)
        self.assertEqual(info["title"], "Backend Engineer")
        self.assertEqual(info["description"], "You will build Python services. Requirements: 2 years.")
        self.assertEqual(info["location"], "")


class TestScrapedBoards(unittest.TestCase):
    def test_workday(self):
        http = Pages({WD_URL: WORKDAY_PAGE})
        jobs = list(WorkdayBoard({}).search(ctx(http, [WD_URL])))
        self.assertEqual([(j.id, j.company, j.location, j.ats) for j in jobs],
                         [("workday_Software-Engineer_R123", "gamma", "Austin, TX, US", "workday")])
        self.assertIn("Mozilla", http.calls[0][1]["headers"]["User-Agent"])

    def test_adp_id_includes_the_company(self):
        jobs = list(AdpBoard({}).search(ctx(Pages({ADP_URL: DOVER_PAGE}), [ADP_URL])))
        self.assertEqual([j.id for j in jobs], ["adp_c0ffee_515151"])

    def test_dover_and_gem(self):
        dover, gem = "https://jobs.dover.io/acme/abc-123", "https://jobs.gem.com/acme/xyz"
        self.assertEqual([j.id for j in DoverBoard({}).search(ctx(Pages({dover: DOVER_PAGE}), [dover]))],
                         ["dover_abc-123"])
        self.assertEqual([j.title for j in GemBoard({}).search(ctx(Pages({gem: DOVER_PAGE}), [gem]))],
                         ["Backend Engineer"])

    def test_known_stale_and_failing_postings(self):
        stale = WORKDAY_PAGE.replace(RECENT, "2020-01-01")
        other = WD_URL.replace("R123", "R999")
        known = WD_URL.replace("R123", "R555")
        gone = WD_URL.replace("R123", "R404")
        http, logs, marked = Pages({other: stale}), [], []
        jobs = list(WorkdayBoard({}).search(ctx(http, [other, known, gone], known={"workday_Software-Engineer_R555"},
                                                logs=logs, stale=marked)))
        self.assertEqual(jobs, [])
        self.assertEqual(marked, ["workday_Software-Engineer_R999"])
        self.assertNotIn(known, [c[0] for c in http.calls])
        self.assertTrue(any("R404" in line for line in logs), logs)

    def test_discover_off(self):
        http = Pages({WD_URL: WORKDAY_PAGE})
        self.assertEqual(list(WorkdayBoard({"discover": False}).search(ctx(http, [WD_URL]))), [])
        self.assertEqual(http.calls, [])


class TestGonePages(unittest.TestCase):
    def test_missing_and_empty_pages_are_gone(self):
        # Workday's page for a closed job: a bare app shell, sometimes with the site's <title>.
        empty = "<html><head><title>Careers</title></head><body><div id='root'></div></body></html>"
        closed, missing, broken = (WD_URL.replace("R123", r) for r in ("R1", "R2", "R3"))

        class Answers(Pages):
            def request(self, method, url, **kw):
                self.calls.append((url, kw))
                code = {missing: 404, broken: 500}.get(url, 200)
                return FakeResponse(status_code=code, text=self.pages.get(url, ""))

        gone, logs = [], []
        jobs = list(WorkdayBoard({}).search(ctx(Answers({WD_URL: WORKDAY_PAGE, closed: empty}),
                                                [WD_URL, closed, missing, broken], gone=gone, logs=logs)))
        self.assertEqual([j.id for j in jobs], ["workday_Software-Engineer_R123"])
        self.assertEqual(sorted(gone), ["workday_Software-Engineer_R1", "workday_Software-Engineer_R2"])
        self.assertTrue(any("R3" in line for line in logs), logs)

    def test_a_page_that_cannot_be_read_does_not_stop_the_board(self):
        other = WD_URL.replace("R123", "R7")
        with unittest.mock.patch("jobhunter.boards.scraped_ats.read_posting",
                                 side_effect=[ValueError("odd page"), read_posting(WORKDAY_PAGE)]):
            jobs = list(WorkdayBoard({}).search(ctx(Pages({WD_URL: WORKDAY_PAGE, other: WORKDAY_PAGE}), [WD_URL, other])))
        self.assertEqual(len(jobs), 1)


class TestRedirects(unittest.TestCase):
    def test_a_redirect_off_the_ats_is_not_followed(self):
        moved = WD_URL.replace("R123", "R9")

        class Redirecting(Pages):
            def request(self, method, url, **kw):
                self.calls.append((url, kw))
                if url == WD_URL:
                    return FakeResponse(status_code=302, headers={"Location": "http://127.0.0.1/admin"})
                if url == moved:
                    return FakeResponse(status_code=301, headers={"Location": WD_URL.replace("R123", "R10")})
                return FakeResponse(text=WORKDAY_PAGE)

        http, logs = Redirecting({}), []
        jobs = list(WorkdayBoard({}).search(ctx(http, [WD_URL, moved], logs=logs)))
        fetched = [url for url, _ in http.calls]
        self.assertNotIn("http://127.0.0.1/admin", fetched)
        self.assertIn(WD_URL.replace("R123", "R10"), fetched)       # a redirect within Workday is followed
        self.assertEqual(len(jobs), 1)
        self.assertTrue(all(kw.get("allow_redirects") is False for _, kw in http.calls))
        self.assertTrue(any("redirect" in line for line in logs), logs)


class TestEmptyPagesElsewhere(unittest.TestCase):
    def test_an_empty_page_on_dover_adp_or_gem_is_tried_again_later(self):
        empty = "<html><head><title>Dover</title></head><body><div id='root'></div></body></html>"   # JavaScript only
        for board, url in ((DoverBoard, "https://jobs.dover.io/acme/abc-123"), (GemBoard, "https://jobs.gem.com/acme/x"),
                           (AdpBoard, ADP_URL)):
            gone, logs = [], []
            jobs = list(board({}).search(ctx(Pages({url: empty}), [url], gone=gone, logs=logs)))
            self.assertEqual((jobs, gone), ([], []), board)
            self.assertTrue(any("no job data" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
