import unittest

import requests
from datetime import datetime, timedelta, timezone

from jobhunter.boards.ashby import AshbyBoard
from jobhunter.boards.greenhouse import GreenhouseBoard, to_job
from jobhunter.boards.lever import LeverBoard
from jobhunter.models import SearchContext

NOW = datetime.now(timezone.utc)


def iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def ms(hours_ago):
    return int((NOW - timedelta(hours=hours_ago)).timestamp() * 1000)


def not_found(url, code=404):
    response = requests.Response()
    response.status_code, response.url = code, url
    return requests.HTTPError(f"{code} Client Error: Not Found for url: {url}", response=response)


class Routes:
    """get_json by exact URL; anything unknown is a 404."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_json(self, url, **kw):
        self.calls.append(url)
        value = self.routes.get(url, not_found(url))
        if isinstance(value, Exception):
            raise value
        return value


class FakeDiscovery:
    def __init__(self, urls):
        self._urls = set(urls)

    def urls(self):
        return set(self._urls)


def ctx(http, urls=(), known=(), logs=None, stale=None, gone=None):
    return SearchContext(["swe"], ["Remote"], 72, http, (logs if logs is not None else []).append,
                         discovery=FakeDiscovery(urls), is_known=lambda job_id: job_id in known,
                         mark_stale=(stale if stale is not None else []).append,
                         mark_gone=(gone if gone is not None else []).append)


GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs/{}"


def gh(i, hours_ago, **kw):
    return {"id": i, "title": "Software Engineer", "updated_at": iso(hours_ago), "location": {"name": "Remote - US"},
            "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}", "content": "&lt;p&gt;Build&lt;/p&gt;", **kw}


class TestGreenhouseDiscover(unittest.TestCase):
    def test_discovered_postings_are_fetched_once_each(self):
        http = Routes({GH.format("acme", 1): gh(1, 1), GH.format("acme", 2): gh(2, 500)})
        urls = ["https://boards.greenhouse.io/acme/jobs/1", "https://job-boards.greenhouse.io/acme/jobs/1?x=1",
                "https://boards.greenhouse.io/acme/jobs/2", "https://boards.greenhouse.io/acme/jobs/3",
                "https://boards.greenhouse.io/acme/jobs/4"]
        logs, stale = [], []
        jobs = list(GreenhouseBoard({"discover": True}).search(ctx(http, urls, known={"greenhouse_4"}, logs=logs,
                                                                   stale=stale)))
        self.assertEqual(stale, ["greenhouse_2"])       # remembered, so later runs skip it without a request
        self.assertEqual([j.id for j in jobs], ["greenhouse_1"])               # 2 is stale, 3 is gone, 4 is known
        self.assertEqual((jobs[0].description, jobs[0].company), ("Build", "acme"))
        self.assertEqual(http.calls.count(GH.format("acme", 1)), 1)
        self.assertNotIn(GH.format("acme", 4), http.calls)
        self.assertTrue(any("acme/3" in line for line in logs), logs)

    def test_company_list_and_discovery_do_not_duplicate(self):
        http = Routes({"https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [gh(1, 1)]}})
        jobs = list(GreenhouseBoard({"companies": ["acme"], "discover": True})
                    .search(ctx(http, ["https://boards.greenhouse.io/acme/jobs/1"])))
        self.assertEqual([j.id for j in jobs], ["greenhouse_1"])
        self.assertEqual(len(http.calls), 1)

    def test_discover_off_ignores_discovery(self):
        http = Routes({})
        board = GreenhouseBoard({"discover": False})
        self.assertEqual(list(board.search(ctx(http, ["https://boards.greenhouse.io/acme/jobs/1"]))), [])
        self.assertEqual(http.calls, [])


LEVER_ID = "0f7a8c2e-1111-2222-3333-444455556666"


def lever(i, hours_ago):
    return {"id": i, "text": "Backend Engineer", "categories": {"location": "Remote - US"},
            "hostedUrl": f"https://jobs.lever.co/beta/{i}", "createdAt": ms(hours_ago),
            "descriptionPlain": "Build services.",
            "lists": [{"text": "Requirements", "content": "<li>Java</li><li>SQL</li>"}], "additionalPlain": "Benefits."}


class TestLever(unittest.TestCase):
    def test_company_list(self):
        http = Routes({"https://api.lever.co/v0/postings/beta": [lever("a", 1), lever("b", 500)]})
        jobs = list(LeverBoard({"companies": ["beta"]}).search(ctx(http)))
        self.assertEqual([j.id for j in jobs], ["lever_a"])
        self.assertEqual(jobs[0].description, "Build services.\n\nRequirements\nJava\nSQL\n\nBenefits.")
        self.assertEqual((jobs[0].company, jobs[0].location, jobs[0].ats), ("beta", "Remote - US", "lever"))

    def test_discovered_detail_including_the_wrapped_form(self):
        http = Routes({f"https://api.lever.co/v0/postings/beta/{LEVER_ID}": {"postings": [lever(LEVER_ID, 1)]}})
        jobs = list(LeverBoard({"discover": True}).search(ctx(http, [f"https://jobs.lever.co/beta/{LEVER_ID}/apply"])))
        self.assertEqual([j.id for j in jobs], [f"lever_{LEVER_ID}"])


def ashby(i, hours_ago):
    return {"id": i, "title": "Platform Engineer", "location": "New York, NY", "jobUrl": f"https://jobs.ashbyhq.com/clerk/{i}",
            "publishedAt": iso(hours_ago), "descriptionHtml": "<p>Run <b>Kubernetes</b>.</p>"}


class TestAshby(unittest.TestCase):
    def test_one_board_request_per_company(self):
        board = {"jobs": [ashby("j1", 1), ashby("j2", 1), ashby("j3", 500)]}
        http = Routes({"https://api.ashbyhq.com/posting-api/job-board/clerk": board})
        urls = [f"https://jobs.ashbyhq.com/clerk/{i}" for i in ("j1", "j2", "j3", "gone")]
        jobs = list(AshbyBoard({"discover": True}).search(ctx(http, urls)))
        self.assertEqual([j.id for j in jobs], ["ashby_j1", "ashby_j2"])
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(jobs[0].description, "Run Kubernetes.")

    def test_company_list(self):
        http = Routes({"https://api.ashbyhq.com/posting-api/job-board/clerk": {"jobs": [ashby("j1", 1)]}})
        self.assertEqual([j.id for j in AshbyBoard({"companies": ["clerk"]}).search(ctx(http))], ["ashby_j1"])

    def test_a_missing_board_is_logged(self):
        logs = []
        self.assertEqual(list(AshbyBoard({"companies": ["nope"]}).search(ctx(Routes({}), logs=logs))), [])
        self.assertTrue(any("nope" in line for line in logs), logs)

HOSTED = "https://jobs.ashbyhq.com/api/non-user-graphql"


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise not_found(HOSTED, self.status_code)


class HostedRoutes(Routes):
    """Routes, plus Ashby's hosted job-board GraphQL: `boards` maps a slug to {posting id: (hours ago, title)}."""

    def __init__(self, routes, boards):
        super().__init__(routes)
        self.boards, self.posts = boards, []

    def request(self, method, url, json=None, **kw):
        assert method == "POST" and url.split("?")[0] == HOSTED, (method, url)
        v = json["variables"]
        slug = v["organizationHostedJobsPageName"]
        if slug not in self.boards:
            return FakeResponse({"data": {"jobBoard" if "jobPostingId" not in v else "jobPosting": None}})
        if "jobPostingId" not in v:
            self.posts.append((slug, None))
            return FakeResponse({"data": {"jobBoard": {"jobPostings": [
                {"id": i, "title": t} for i, (_, t) in self.boards[slug].items()]}}})
        self.posts.append((slug, v["jobPostingId"]))
        hours, title = self.boards[slug][v["jobPostingId"]]
        return FakeResponse({"data": {"jobPosting": {
            "id": v["jobPostingId"], "title": title, "locationName": "San Francisco, CA",
            "publishedDate": (NOW - timedelta(hours=hours)).date().isoformat(), "descriptionHtml": "<p>Ship <b>features</b>.</p>"}}})


class TestAshbyHostedFallback(unittest.TestCase):
    """Some companies turn Ashby's public posting API off (404) while their hosted job board still works."""

    def test_a_listed_company_is_read_from_its_hosted_board(self):
        http = HostedRoutes({}, {"whatnot": {"p1": (2, "Software Engineer"), "p2": (500, "Old Role")}})
        jobs = list(AshbyBoard({"companies": ["whatnot"]}).search(ctx(http)))
        self.assertEqual([(j.id, j.title, j.company) for j in jobs], [("ashby_p1", "Software Engineer", "whatnot")])
        job = jobs[0]
        self.assertEqual((job.location, job.url, job.source), ("San Francisco, CA", "https://jobs.ashbyhq.com/whatnot/p1", "ashby"))
        self.assertEqual(job.description, "Ship features.")
        self.assertTrue(job.posted_at)

    def test_known_postings_are_not_fetched_again_and_old_ones_are_remembered_as_stale(self):
        stale = []
        http = HostedRoutes({}, {"whatnot": {"p1": (2, "A"), "p2": (500, "B"), "p3": (1, "C")}})
        jobs = list(AshbyBoard({"companies": ["whatnot"]}).search(ctx(http, known={"ashby_p3"}, stale=stale)))
        self.assertEqual([j.id for j in jobs], ["ashby_p1"])
        self.assertEqual(stale, ["ashby_p2"])
        self.assertNotIn(("whatnot", "p3"), http.posts)

    def test_discovered_postings_use_the_hosted_board_too(self):
        gone = []
        http = HostedRoutes({}, {"evenup": {"e1": (3, "Backend Engineer")}})
        urls = ["https://jobs.ashbyhq.com/evenup/e1", "https://jobs.ashbyhq.com/evenup/closed"]
        jobs = list(AshbyBoard({}).search(ctx(http, urls, gone=gone)))
        self.assertEqual([j.id for j in jobs], ["ashby_e1"])
        self.assertEqual(gone, ["ashby_closed"])
        self.assertEqual(http.posts.count(("evenup", None)), 1)            # one listing per company per run

    def test_both_failing_is_logged_once_and_yields_nothing(self):
        logs = []
        http = HostedRoutes({}, {})
        self.assertEqual(list(AshbyBoard({"companies": ["nope"]}).search(ctx(http, logs=logs))), [])
        self.assertEqual(len([line for line in logs if "nope" in line]), 1, logs)

    def test_a_working_posting_api_never_touches_the_hosted_board(self):
        http = HostedRoutes({"https://api.ashbyhq.com/posting-api/job-board/clerk": {"jobs": [ashby("j1", 1)]}},
                            {"clerk": {"x": (1, "never read")}})
        self.assertEqual([j.id for j in AshbyBoard({"companies": ["clerk"]}).search(ctx(http))], ["ashby_j1"])
        self.assertEqual(http.posts, [])



class TestDiscoverDefault(unittest.TestCase):
    def test_discovery_is_on_unless_companies_are_listed(self):
        for cls in (GreenhouseBoard, LeverBoard, AshbyBoard):
            self.assertTrue(cls({}).discover, cls)
            self.assertFalse(cls({"companies": ["acme"]}).discover, cls)
            self.assertTrue(cls({"companies": ["acme"], "discover": True}).discover, cls)
            self.assertFalse(cls({"discover": False}).discover, cls)


class TestGonePostings(unittest.TestCase):
    def test_greenhouse_and_lever_postings_that_answer_404_or_410_are_gone(self):
        gone = []
        http = Routes({GH.format("acme", 5): not_found(GH.format("acme", 5), 410),
                       GH.format("acme", 6): RuntimeError("HTTP 500")})
        urls = [f"https://boards.greenhouse.io/acme/jobs/{i}" for i in (4, 5, 6)]
        self.assertEqual(list(GreenhouseBoard({}).search(ctx(http, urls, gone=gone))), [])
        self.assertEqual(gone, ["greenhouse_4", "greenhouse_5"])            # a server error is not "gone"
        gone = []
        list(LeverBoard({}).search(ctx(Routes({}), [f"https://jobs.lever.co/beta/{LEVER_ID}"], gone=gone)))
        self.assertEqual(gone, [f"lever_{LEVER_ID}"])

    def test_an_ashby_posting_missing_from_its_board_is_gone(self):
        gone = []
        http = Routes({"https://api.ashbyhq.com/posting-api/job-board/clerk": {"jobs": [ashby("j1", 1)]}})
        urls = ["https://jobs.ashbyhq.com/clerk/j1", "https://jobs.ashbyhq.com/clerk/closed",
                "https://jobs.ashbyhq.com/unreachable/j9"]
        jobs = list(AshbyBoard({}).search(ctx(http, urls, gone=gone)))
        self.assertEqual([j.id for j in jobs], ["ashby_j1"])
        self.assertEqual(gone, ["ashby_closed"])                            # an unreachable board proves nothing


class TestOneBadListing(unittest.TestCase):
    def test_a_malformed_listing_is_skipped_and_the_rest_kept(self):
        logs = []
        http = Routes({"https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [gh(1, 1), {"title": "no id"},
                                                                                             "not a dict", gh(2, 1)]}})
        jobs = list(GreenhouseBoard({"companies": ["acme"]}).search(ctx(http, logs=logs)))
        self.assertEqual([j.id for j in jobs], ["greenhouse_1", "greenhouse_2"])
        self.assertEqual(sum("could not read" in line for line in logs), 2, logs)
        http = Routes({"https://api.lever.co/v0/postings/beta": [lever("a", 1), {"id": "b", "categories": "x"},
                                                                 lever("c", 1)]})
        self.assertEqual([j.id for j in LeverBoard({"companies": ["beta"]}).search(ctx(http))], ["lever_a", "lever_c"])


class TestGreenhouseContent(unittest.TestCase):
    def test_content_escaped_once_keeps_escaped_text(self):
        # What boards-api.greenhouse.io returns today: HTML escaped once (55 of 55 SoFi jobs).
        raw = gh(1, 1, content="&lt;p&gt;Use List&amp;lt;String&amp;gt; in Java&lt;/p&gt;")
        self.assertEqual(to_job(raw, "acme").description, "Use List<String> in Java")

    def test_content_escaped_twice_is_still_read(self):
        raw = gh(1, 1, content="&amp;lt;p&amp;gt;Build services&amp;lt;/p&amp;gt;")
        self.assertEqual(to_job(raw, "acme").description, "Build services")


class TestLeverEu(unittest.TestCase):
    def test_eu_postings_are_read_from_the_eu_api(self):
        url = f"https://jobs.eu.lever.co/cirrus/{LEVER_ID}/apply"
        http = Routes({f"https://api.eu.lever.co/v0/postings/cirrus/{LEVER_ID}": lever(LEVER_ID, 1)})
        jobs = list(LeverBoard({}).search(ctx(http, [url])))
        self.assertEqual([j.id for j in jobs], [f"lever_{LEVER_ID}"])


class TestLeverEuHost(unittest.TestCase):
    def test_the_eu_api_is_chosen_by_hostname_not_by_text_in_the_url(self):
        url = f"https://jobs.lever.co/acme/{LEVER_ID}?ref=https://jobs.eu.lever.co/"
        http = Routes({f"https://api.lever.co/v0/postings/acme/{LEVER_ID}": lever(LEVER_ID, 1)})
        self.assertEqual(len(list(LeverBoard({}).search(ctx(http, [url])))), 1)


if __name__ == "__main__":
    unittest.main()
