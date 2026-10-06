import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from jobhunter.boards.hydepark import HydeParkBoard
from jobhunter.boards.industry_jobs import IndustryJobsBoard, is_likely_opening
from jobhunter.boards.top_companies import TopCompaniesBoard
from jobhunter.models import SearchContext
from tests.helpers import FakeResponse

NOW = datetime.now(timezone.utc)


class FakeHttp:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.handler(method, url, kw)

    def get_json(self, url, **kw):
        resp = self.request("GET", url, **kw)
        resp.raise_for_status()
        return resp.json()


def ctx(http, queries=("swe",), logs=None):
    return SearchContext(list(queries), ["Remote"], 72, http, (logs if logs is not None else []).append)


def getro(i, hours_ago, url="https://boards.greenhouse.io/acme/jobs/1", **kw):
    return {"id": i, "title": " Software Engineer ", "organization": {"name": "Acme "}, "locations": ["Detroit, MI", "Remote"],
            "url": url, "created_at": int((NOW - timedelta(hours=hours_ago)).timestamp()), **kw}


class TestHydePark(unittest.TestCase):
    def test_fresh_jobs_with_a_link(self):
        jobs_json = {"results": {"jobs": [getro(1, 2), getro(2, 500), getro(3, 2, url=""), getro(4, 2, created_at=None)]}}
        http = FakeHttp(lambda m, u, kw: FakeResponse(json_data=jobs_json))
        jobs = list(HydeParkBoard({}).search(ctx(http)))
        self.assertEqual([(j.id, j.title, j.company, j.location, j.ats, j.description) for j in jobs],
                         [("hydepark_1", "Software Engineer", "Acme", "Detroit, MI, Remote", "greenhouse", "")])
        method, url, kw = http.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.getro.com/api/v2/collections/112/search/jobs"))
        self.assertEqual(kw["json"]["filters"], {"job_functions": ["Software Engineering"]})
        self.assertIs(kw["retry"], True)          # a search, so safe to repeat

    def test_an_unknown_location_is_left_empty_not_called_remote(self):
        http = FakeHttp(lambda m, u, kw: FakeResponse(json_data={"results": {"jobs": [getro(1, 2, locations=[])]}}))
        self.assertEqual([j.location for j in HydeParkBoard({}).search(ctx(http))], [""])

    def test_a_failed_search_fails_the_board(self):
        http = FakeHttp(lambda m, u, kw: FakeResponse(status_code=500, json_data={}))
        with self.assertRaises(RuntimeError):
            list(HydeParkBoard({}).search(ctx(http)))


def item(title, link, description, hours_ago, *categories):
    cats = "".join(f"<category>{c}</category>" for c in categories)
    return (f"<item><title>{title}</title><link>{link}</link><description>{description}</description>"
            f"<pubDate>{format_datetime(NOW - timedelta(hours=hours_ago))}</pubDate>{cats}</item>")


FEED = "<rss><channel>" + "".join([
    item("[Hiring] Backend Engineer at Acme", "https://dev.to/acme/hiring-backend-engineer-1abc",
         "&lt;p&gt;We are hiring a backend engineer. Apply now.&lt;/p&gt;", 2, "hiring"),
    item("How to run background jobs in Node", "https://dev.to/x/background-jobs-2", "Tutorial", 2, "jobs"),
    item("Frontend developer position", "https://dev.to/y/frontend-3", "Apply for this position", 2, "jobs"),
    item("We are hiring engineers", "https://dev.to/z/old-4", "apply now", 500, "hiring"),
]) + "</channel></rss>"


class TestIndustryJobs(unittest.TestCase):
    def test_only_fresh_openings_each_once(self):
        http = FakeHttp(lambda m, u, kw: FakeResponse(text=FEED))
        jobs = list(IndustryJobsBoard({}).search(ctx(http)))
        self.assertEqual([j.id for j in jobs], ["devto_hiring-backend-engineer-1abc", "devto_frontend-3"])
        self.assertEqual(jobs[0].description, "We are hiring a backend engineer. Apply now.")
        self.assertEqual([c[1] for c in http.calls], ["https://dev.to/feed/tag/jobs", "https://dev.to/feed/tag/hiring"])

    def test_a_broken_feed_is_logged(self):
        logs = []
        http = FakeHttp(lambda m, u, kw: FakeResponse(text="<rss><channel>"))
        self.assertEqual(list(IndustryJobsBoard({"tags": ["jobs"]}).search(ctx(http, logs=logs))), [])
        self.assertTrue(any("jobs" in line for line in logs), logs)

    def test_opening_heuristic(self):
        self.assertTrue(is_likely_opening("We're hiring a software engineer", "", []))
        self.assertFalse(is_likely_opening("Top 10 engineer interview tips", "we are hiring", []))
        self.assertFalse(is_likely_opening("Engineering blog", "", ["jobs"]))
        # "api" is an article word only as a whole word, so "rapid" does not count
        self.assertTrue(is_likely_opening("Frontend developer position", "rapid prototyping, apply here", ["jobs"]))

    def test_article_words_in_a_hiring_post_s_text_do_not_reject_it(self):
        for title, text in (("[Hiring] Backend Engineer", "You will build our API. Apply now."),
                            ("We're hiring a full stack developer", "Work with top engineers. Apply now."),
                            ("Senior software engineer - now hiring", "Why join us? Background jobs at scale. Apply now")):
            self.assertTrue(is_likely_opening(title, text, ["hiring"]), title)
        for title in ("How to land a software engineer job", "Building an API with Supabase: we're hiring",
                      "Top 5 developer job boards"):
            self.assertFalse(is_likely_opening(title, "We are hiring engineers. Apply now.", ["jobs"]), title)


def amazon(path, days_ago, **kw):
    return {"job_path": path, "title": "Software Dev Engineer I", "city": "Seattle", "state": "WA", "country_code": "USA",
            "normalized_location": "Seattle, Washington, USA",
            "description": "Build &lt;b&gt;things&lt;/b&gt;", "basic_qualifications": "Java",
            "preferred_qualifications": "AWS",
            "posted_date": (NOW - timedelta(days=days_ago)).strftime("%B %d, %Y"), **kw}


class TestTopCompanies(unittest.TestCase):
    def test_amazon_and_greenhouse_companies(self):
        amazon_jobs = {"jobs": [amazon("/en/jobs/2790001/sde-i", 1), amazon("/en/jobs/2790001/sde-i", 1),
                                amazon("/en/jobs/1000/old", 30)]}
        stripe = {"jobs": [{"id": 77, "title": "Backend Engineer", "updated_at": NOW.isoformat(),
                            "location": {"name": "Remote - US"}, "absolute_url": "https://stripe.com/jobs/77",
                            "content": "&amp;lt;p&amp;gt;Payments&amp;lt;/p&amp;gt;"}]}

        def handler(method, url, kw):
            if "amazon.jobs" in url:
                return FakeResponse(json_data=amazon_jobs)
            return FakeResponse(json_data=stripe)

        http = FakeHttp(handler)
        jobs = list(TopCompaniesBoard({"greenhouse": {"stripe": "Stripe"}})
                    .search(ctx(http, queries=["a", "b", "c", "d", "e", "f"])))
        self.assertEqual([(j.id, j.company, j.source) for j in jobs],
                         [("top_amazon_2790001", "Amazon", "top_companies"), ("top_stripe_77", "Stripe", "top_companies")])
        self.assertEqual(jobs[0].url, "https://www.amazon.jobs/en/jobs/2790001/sde-i")
        self.assertEqual(jobs[0].location, "Seattle, Washington, USA")
        self.assertEqual(jobs[0].description, "Build things\nJava\nAWS")
        self.assertEqual(jobs[1].description, "Payments")
        amazon_calls = [c for c in http.calls if "amazon.jobs" in c[1]]
        self.assertEqual([c[2]["params"]["base_query"] for c in amazon_calls], ["a", "b", "c", "d"])

    def test_a_greenhouse_company_posting_is_dated_by_its_first_publication(self):
        edited = {"jobs": [{"id": 78, "title": "Backend Engineer", "updated_at": NOW.isoformat(),
                            "first_published": (NOW - timedelta(days=30)).isoformat(),
                            "location": {"name": "Remote - US"}, "absolute_url": "https://stripe.com/jobs/78"}]}
        http = FakeHttp(lambda m, u, kw: FakeResponse(json_data=edited))
        jobs = list(TopCompaniesBoard({"amazon": False, "greenhouse": {"stripe": "Stripe"}}).search(ctx(http)))
        self.assertEqual(jobs, [])


class TestOneBadListingSearchBoards(unittest.TestCase):
    def test_hydepark_keeps_going_past_a_malformed_listing(self):
        items = [getro(1, 2), getro(2, 2, organization="not a dict"), getro(3, 2, created_at="soon"), getro(4, 2)]
        http = FakeHttp(lambda m, u, kw: FakeResponse(json_data={"results": {"jobs": items}}))
        logs = []
        jobs = list(HydeParkBoard({}).search(ctx(http, logs=logs)))
        self.assertEqual([j.id for j in jobs], ["hydepark_1", "hydepark_3", "hydepark_4"])
        self.assertEqual(jobs[1].posted_at, "")                     # an unreadable date is left empty
        self.assertTrue(any("could not read" in line for line in logs), logs)

    def test_top_companies_keeps_going_past_a_malformed_listing(self):
        data = {"jobs": [amazon("/en/jobs/1/a", 1), amazon("/en/jobs/2/b", 1, title=["not", "text"]),
                         amazon("/en/jobs/3/c", 1)]}
        data["jobs"][1]["description"] = 5
        http = FakeHttp(lambda m, u, kw: FakeResponse(json_data=data if "amazon" in u else {"jobs": []}))
        jobs = list(TopCompaniesBoard({"amazon_queries": 1}).search(ctx(http)))
        self.assertEqual([j.id for j in jobs], ["top_amazon_1", "top_amazon_3"])


class TestAmazonRequests(unittest.TestCase):
    def test_us_jobs_newest_first_by_country_code(self):
        # loc_query is ignored by amazon.jobs (a "United States" search returned Sydney, Haifa and Berlin).
        http = FakeHttp(lambda m, u, kw: FakeResponse(json_data={"jobs": []}))
        list(TopCompaniesBoard({"amazon_queries": 1, "greenhouse": {}}).search(ctx(http)))
        params = http.calls[0][2]["params"]
        self.assertEqual((params.get("normalized_country_code[]"), params.get("sort")), ("USA", "recent"))
        self.assertNotIn("loc_query", params)

    def test_the_location_keeps_its_country(self):
        item = amazon("/en/jobs/7/x", 1)
        del item["normalized_location"]
        item.update(city="Berlin", state="BE", country_code="DEU")
        http = FakeHttp(lambda m, u, kw: FakeResponse(json_data={"jobs": [item]} if "amazon" in u else {"jobs": []}))
        jobs = list(TopCompaniesBoard({"amazon_queries": 1}).search(ctx(http)))
        self.assertEqual(jobs[0].location, "Berlin, BE, DEU")


if __name__ == "__main__":
    unittest.main()
