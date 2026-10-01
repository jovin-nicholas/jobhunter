import threading
import unittest
from datetime import datetime, timedelta, timezone

import requests

from jobhunter.boards.ats_urls import Posting, ats_of, parse, postings
from jobhunter.boards.common import is_fresh, parse_time
from jobhunter.discovery import Discovery
from jobhunter.settings import DiscoverySettings
from tests.helpers import FakeResponse

README = """| Company | Role | Link |
| Acme | SWE | <a href="https://boards.greenhouse.io/acme/jobs/123?gh_src=x">Apply</a> |
| Beta | SWE | [Apply](https://jobs.lever.co/beta/0f7a8c2e-1111-2222-3333-444455556666/apply) |
| Gamma | SWE | https://gamma.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Software-Engineer_R123 |
| Docs | x | https://example.com/not-an-ats |
"""
ADP = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
       "?cid=c0ffee&ccId=19000101_000001&jobId=515151&lang=en_US")


class FakeHttp:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.handler(url, kw)


class TestParse(unittest.TestCase):
    def test_each_ats(self):
        cases = {
            ("greenhouse", "https://boards.greenhouse.io/acme/jobs/123?gh_src=x"): ("acme", "123"),
            ("greenhouse", "https://job-boards.greenhouse.io/acme/jobs/123"): ("acme", "123"),
            ("lever", "https://jobs.lever.co/beta/0f7a8c2e-1111-2222-3333-444455556666/apply"):
                ("beta", "0f7a8c2e-1111-2222-3333-444455556666"),
            ("ashby", "https://jobs.ashbyhq.com/clerk/4174686d-3925-4584-ac24-49ed637218fa/application"):
                ("clerk", "4174686d-3925-4584-ac24-49ed637218fa"),
            ("workday", "https://gamma.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Software-Engineer_R123"):
                ("gamma", "Software-Engineer_R123"),
            ("workday", "https://gamma.wd5.myworkdayjobs.com/External/job/Austin-TX/Software-Engineer_R123/apply"):
                ("gamma", "Software-Engineer_R123"),
            ("workday", "https://gamma.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Software-Engineer_R123"
                        "/apply/applyManually"): ("gamma", "Software-Engineer_R123"),
            ("workday", "https://gamma.wd5.myworkdayjobs.com/External/job/Austin-TX/Software-Engineer_R123/"
                        "autofillWithResume?x=1"): ("gamma", "Software-Engineer_R123"),
            ("workday", "https://gamma.wd5.myworkdayjobs.com/en-US/Careers/details/Software-Engineer_R123"):
                ("gamma", "Software-Engineer_R123"),
            ("dover", "https://jobs.dover.io/acme/abc-123"): ("acme", "abc-123"),
            ("dover", "https://app.dover.io/apply/acme/1f2e3d4c-aaaa-bbbb-cccc-000011112222?rs=1"):
                ("acme", "1f2e3d4c-aaaa-bbbb-cccc-000011112222"),
            ("gem", "https://jobs.gem.com/acme/xyz"): ("acme", "xyz"),
            ("adp", ADP): ("c0ffee", "515151"),
        }
        for (ats, url), expected in cases.items():
            self.assertEqual(parse(ats, url), Posting(*expected, url), url)

    def test_urls_that_are_not_postings(self):
        for ats, url in (("greenhouse", "https://example.com/jobs/1"),
                         ("workday", "https://gamma.wd5.myworkdayjobs.com/en-US/External"),
                         ("adp", ADP.replace("&jobId=515151", "")), ("lever", "https://jobs.lever.co/beta")):
            self.assertIsNone(parse(ats, url), url)

    def test_the_same_posting_under_two_urls_is_kept_once(self):
        found = postings("greenhouse", ["https://job-boards.greenhouse.io/acme/jobs/123?x=1",
                                        "https://boards.greenhouse.io/Acme/jobs/123", "https://example.com/x"])
        self.assertEqual([(p.slug, p.job_id) for p in found], [("acme", "123")])

    def test_ats_of(self):
        self.assertEqual(ats_of("https://boards.greenhouse.io/acme/jobs/1"), "greenhouse")
        self.assertEqual(ats_of("https://acme.wd1.myworkdayjobs.com/x/job/y"), "workday")
        self.assertEqual(ats_of("https://notgreenhouse.io.example.com/", "hydepark"), "hydepark")


class TestFreshness(unittest.TestCase):
    def test_parse_time_formats(self):
        self.assertEqual(parse_time("2026-09-27T10:00:00Z"), datetime(2026, 9, 27, 10, tzinfo=timezone.utc))
        self.assertEqual(parse_time(1790000000), datetime.fromtimestamp(1790000000, tz=timezone.utc))
        self.assertEqual(parse_time("1790000000000"), datetime.fromtimestamp(1790000000, tz=timezone.utc))
        self.assertEqual(parse_time("September 24, 2026"), datetime(2026, 9, 24, tzinfo=timezone.utc))
        self.assertIsNone(parse_time("soon"))

    def test_unreadable_dates_count_as_fresh(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
        self.assertTrue(is_fresh(None, cutoff))
        self.assertTrue(is_fresh("soon", cutoff))
        self.assertFalse(is_fresh("2020-01-01T00:00:00Z", cutoff))


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.logs = []

    def test_readme_links_are_fetched_once_per_run(self):
        http = FakeHttp(lambda url, kw: FakeResponse(text=README))
        d = Discovery(DiscoverySettings(["https://raw.example/README.md"]), ["swe"], http, self.logs.append, env={})
        threads = [threading.Thread(target=d.urls) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        urls = d.urls()
        self.assertEqual(len(http.calls), 1)
        self.assertIn("https://boards.greenhouse.io/acme/jobs/123?gh_src=x", urls)
        self.assertIn("https://jobs.lever.co/beta/0f7a8c2e-1111-2222-3333-444455556666/apply", urls)
        self.assertEqual(len(urls), 3)

    def test_a_failing_readme_is_logged_and_the_others_used(self):
        def handler(url, kw):
            if "bad" in url:
                raise requests.ConnectionError("refused")
            return FakeResponse(text=README)
        d = Discovery(DiscoverySettings(["https://raw.example/bad.md", "https://raw.example/README.md"]), [],
                      FakeHttp(handler), self.logs.append, env={})
        self.assertEqual(len(d.urls()), 3)
        self.assertTrue(any("bad.md" in line for line in self.logs), self.logs)

    def test_google_is_off_without_settings(self):
        http = FakeHttp(lambda url, kw: FakeResponse(text=""))
        Discovery(DiscoverySettings([]), ["swe"], http, self.logs.append, env={}).urls()
        self.assertEqual(http.calls, [])

    def test_google_results_and_quota(self):
        answers = [FakeResponse(json_data={"items": [{"link": "https://jobs.lever.co/beta/abc"}]}),
                   FakeResponse(status_code=429, json_data={})]
        http = FakeHttp(lambda url, kw: answers.pop(0))
        google = {"api_key_env": "GOOGLE_API_KEY", "cx_env": "GOOGLE_CX"}
        d = Discovery(DiscoverySettings([], google), ["a", "b", "c"], http, self.logs.append,
                      env={"GOOGLE_API_KEY": "SECRET", "GOOGLE_CX": "cx1"})
        self.assertEqual(d.urls(), {"https://jobs.lever.co/beta/abc"})
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[0][2]["params"]["q"], "a")
        self.assertTrue(any("quota" in line for line in self.logs), self.logs)

    def test_google_errors_never_log_the_key(self):
        def handler(url, kw):
            raise requests.ConnectionError(f"GET {url}?key={kw['params']['key']} failed")
        d = Discovery(DiscoverySettings([], {"api_key_env": "K", "cx_env": "C"}), ["a"], FakeHttp(handler),
                      self.logs.append, env={"K": "SECRET", "C": "cx"})
        d.urls()
        self.assertTrue(self.logs)
        self.assertFalse(any("SECRET" in line for line in self.logs), self.logs)


class TestCraftedLinks(unittest.TestCase):
    def test_a_link_whose_real_host_is_not_the_ats_does_not_parse(self):
        for ats, url in (("workday", "http://localhost:8080#@a.myworkdayjobs.com/x/job/1"),
                         ("workday", "http://169.254.169.254?.myworkdayjobs.com/x/job/1"),
                         ("lever", "https://jobs.lever.co.evil.example/acme/abc"),
                         ("greenhouse", "https://boards.greenhouse.io@127.0.0.1/acme/jobs/1")):
            self.assertIsNone(parse(ats, url), url)
        self.assertIsNotNone(parse("workday", "https://gamma.wd5.myworkdayjobs.com/en-US/External/job/X/Y_R1"))

    def test_google_results_are_kept_only_for_known_ats_hosts(self):
        links = [{"link": "https://jobs.lever.co/beta/abc"}, {"link": "http://127.0.0.1/admin"},
                 {"link": "https://example.com/careers"}]
        http = FakeHttp(lambda url, kw: FakeResponse(json_data={"items": links}))
        d = Discovery(DiscoverySettings([], {"api_key_env": "K", "cx_env": "C"}), ["a"], http, lambda line: None,
                      env={"K": "k", "C": "c"})
        self.assertEqual(d.urls(), {"https://jobs.lever.co/beta/abc"})


class TestGoogleReplies(unittest.TestCase):
    def test_a_reply_that_is_not_json_is_skipped(self):
        logs = []

        def handler(url, kw):
            if "googleapis" in url:
                return FakeResponse(text="<html>captcha</html>")
            return FakeResponse(text=README)
        d = Discovery(DiscoverySettings(["https://raw.example/README.md"], {"api_key_env": "K", "cx_env": "C"}),
                      ["a"], FakeHttp(handler), logs.append, env={"K": "k", "C": "c"})
        self.assertEqual(len(d.urls()), 3)
        self.assertTrue(any("not JSON" in line for line in logs), logs)


LIVE_FORMS = """<table><tr><td><a href="https://job-boards.eu.greenhouse.io/imc/jobs/4842595101?utm_source=Simplify">
<img src="apply.png"></a></td></tr>
<tr><td><a href="https://jobs.eu.lever.co/cirrus/b4334931-aee2-40d9-a82e-ae6fb644cab0/apply?utm_source=Simplify">x</a></td></tr>
<tr><td><a href="https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/CHARLOTTE-NC/Software-Engineering-_R-574285?utm_source=Simplify">x</a></td></tr>
<tr><td><a href="https://boards.greenhouse.io/embed/job_app?for=acme&token=7586263002">x</a></td></tr>
<tr><td><a href="https://clever.com/about/careers">not a job board</a></td></tr></table>
[Apply](https://jobs.ashbyhq.com/clerk/4174686d-3925-4584-ac24-49ed637218fa)
"""


class TestLinkForms(unittest.TestCase):
    """Link forms seen in the three default job-list READMEs on 2026-09-29."""

    def test_each_form_is_parsed(self):
        self.assertEqual(parse("greenhouse", "https://job-boards.eu.greenhouse.io/imc/jobs/4842595101?x=1")[:2],
                         ("imc", "4842595101"))
        self.assertEqual(parse("lever", "https://jobs.eu.lever.co/cirrus/b4334931-aee2-40d9-a82e-ae6fb644cab0/apply")[:2],
                         ("cirrus", "b4334931-aee2-40d9-a82e-ae6fb644cab0"))
        self.assertEqual(parse("workday", "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/CHARLOTTE-NC/"
                                          "Software-Engineering-_R-574285?utm_source=x")[:2],
                         ("wf", "Software-Engineering-_R-574285"))
        self.assertEqual(parse("greenhouse", "https://boards.greenhouse.io/embed/job_app?for=acme&token=7586263002")[:2],
                         ("acme", "7586263002"))
        # An embed link with only a token names no company, and the API cannot look it up.
        self.assertIsNone(parse("greenhouse", "https://boards.greenhouse.io/embed/job_app?token=7586263002"))

    def test_discovery_reads_html_markdown_and_bare_links_by_host(self):
        d = Discovery(DiscoverySettings(["https://raw.example/README.md"]), [],
                      FakeHttp(lambda url, kw: FakeResponse(text=LIVE_FORMS)), lambda line: None, env={})
        urls = d.urls()
        self.assertEqual({ats_of(u) for u in urls}, {"greenhouse", "lever", "workday", "ashby"})
        self.assertFalse(any("clever.com" in u for u in urls))
        self.assertEqual(len(urls), 5)


class TestDateOnlyFreshness(unittest.TestCase):
    def test_a_date_without_a_time_is_fresh_while_its_day_is_inside_the_window(self):
        late = datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)
        self.assertTrue(is_fresh("2026-09-28", late - timedelta(hours=24)))       # yesterday, 24 h window
        self.assertTrue(is_fresh("2026-09-29", late - timedelta(hours=6)))        # today, 6 h window
        self.assertTrue(is_fresh("September 28, 2026", late - timedelta(hours=24)))
        self.assertFalse(is_fresh("2026-09-27", late - timedelta(hours=24)))
        self.assertFalse(is_fresh("2026-09-28T01:00:00Z", late - timedelta(hours=6)))   # times are still exact


class TestLinkCleanup(unittest.TestCase):
    def test_links_keep_their_query_and_lose_trailing_punctuation(self):
        from jobhunter.discovery import ats_links
        text = ('<a href="https://jobs.lever.co/a/b?x=1&amp;region=us&amp;copy=2">x</a> '
                'See https://jobs.ashbyhq.com/clerk/4174686d-3925-4584-ac24-49ed637218fa. and '
                '(https://jobs.lever.co/acme/b-c), done')
        self.assertEqual(ats_links(text), {"https://jobs.lever.co/a/b?x=1&region=us&copy=2",
                                           "https://jobs.ashbyhq.com/clerk/4174686d-3925-4584-ac24-49ed637218fa",
                                           "https://jobs.lever.co/acme/b-c"})


if __name__ == "__main__":
    unittest.main()
