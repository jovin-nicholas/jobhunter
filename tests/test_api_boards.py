import unittest
from datetime import datetime, timedelta, timezone

import requests

from jobhunter.models import SearchContext

NOW = datetime.now(timezone.utc)


def iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def http_error(url, code):
    response = requests.Response()
    response.status_code, response.url = code, url
    return requests.HTTPError(f"{code} Error for url: {url}", response=response)


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise http_error("post", self.status_code)


class Api:
    """GET routes by (url, params) or by url alone; anything else is a 404. POSTs go to `post(operation, variables)`."""

    def __init__(self, get=None, post=None):
        self.get, self.post, self.calls = get or {}, post, []

    def get_json(self, url, params=None, **kw):
        key = (url, tuple(sorted((params or {}).items())))
        self.calls.append(key)
        value = self.get.get(key, self.get.get(url, http_error(url, 404)))
        if isinstance(value, Exception):
            raise value
        return value

    def request(self, method, url, json=None, **kw):
        self.calls.append((method, url, json["operationName"], tuple(sorted(json["variables"].items()))))
        return FakeResponse(self.post(json["operationName"], json["variables"]))


class Discovery:
    def __init__(self, urls):
        self._urls = set(urls)

    def urls(self):
        return set(self._urls)


def cx(http, urls=(), known=(), logs=None, stale=None, gone=None):
    return SearchContext(["swe"], ["Remote"], 72, http, (logs if logs is not None else []).append,
                         discovery=Discovery(urls), is_known=lambda job_id: job_id in known,
                         mark_stale=(stale if stale is not None else []).append,
                         mark_gone=(gone if gone is not None else []).append)


DOVER = "https://app.dover.com/api/v1"
FEED = DOVER + "/job-board/jobs/"
PAGE = lambda offset: (FEED, (("limit", 100), ("offset", offset)))   # noqa: E731


def dover_job(i, hours_ago=2, **kw):
    return {"id": i, "title": f"Engineer {i}", "client_name": "Moda", "created": iso(hours_ago), "active": True,
            "is_private": False, "locations": [{"name": "New York City, NY"}, {"name": "Remote"}],
            "user_provided_description": "<p>Build <b>things</b>.</p>", **kw}


def feed_item(i, hours_ago=2):
    return {"id": i, "title": f"Engineer {i}", "client": {"id": "c1", "name": "Moda"}, "date_posted": iso(hours_ago)}


class TestDover(unittest.TestCase):
    def board(self, **options):
        from jobhunter.boards.dover import DoverBoard
        return DoverBoard(options)

    def test_feed_pages_then_details_with_fields_mapped(self):
        http = Api({PAGE(0): {"next": "p2", "results": [feed_item("a", 1), feed_item("b", 2)]},       # newest first
                    PAGE(2): {"next": None, "results": [feed_item("old", 500)]},
                    f"{DOVER}/inbound/application-portal-job/a": dover_job("a"),
                    f"{DOVER}/inbound/application-portal-job/b": dover_job("b")})
        stale = []
        jobs = list(self.board(discover=False).search(cx(http, stale=stale)))
        self.assertEqual([j.id for j in jobs], ["dover_a", "dover_b"])
        self.assertEqual(stale, ["dover_old"])
        self.assertNotIn((f"{DOVER}/inbound/application-portal-job/old", ()), http.calls)    # stale: no detail request
        j = jobs[0]
        self.assertEqual((j.title, j.company, j.location, j.url, j.source, j.ats),
                         ("Engineer a", "Moda", "New York City, NY; Remote", "https://app.dover.com/apply/Moda/a",
                          "dover", "dover"))
        self.assertEqual(j.description, "Build things.")
        self.assertTrue(j.posted_at)

    def test_the_feed_stops_at_the_first_page_past_the_cutoff(self):
        http = Api({PAGE(0): {"next": "p2", "results": [feed_item("a"), feed_item("old", 100)]},
                    PAGE(2): {"next": "p3", "results": [feed_item("older", 200)]},
                    f"{DOVER}/inbound/application-portal-job/a": dover_job("a")})
        jobs = list(self.board(discover=False).search(cx(http)))
        self.assertEqual([j.id for j in jobs], ["dover_a"])
        self.assertNotIn(PAGE(2), http.calls)                  # newest first: nothing fresh can follow

    def test_an_empty_page_with_next_stops_paging(self):
        http = Api({PAGE(0): {"next": "p2", "results": []}})
        self.assertEqual(list(self.board(discover=False).search(cx(http))), [])
        self.assertEqual(len(http.calls), 1)

    def test_known_jobs_are_not_fetched(self):
        http = Api({PAGE(0): {"next": None, "results": [feed_item("a")]}})
        self.assertEqual(list(self.board(discover=False).search(cx(http, known={"dover_a"}))), [])
        self.assertEqual(len(http.calls), 1)

    def test_a_company_resolves_to_its_jobs(self):
        http = Api({f"{DOVER}/careers-page-slug/moda": {"id": "c1", "name": "Moda"},
                    (f"{DOVER}/careers-page/c1/jobs", (("limit", 100), ("offset", 0))):
                        {"next": None, "results": [{"id": "a", "title": "x", "is_published": True},
                                                   {"id": "hidden", "title": "y", "is_published": False}]},
                    f"{DOVER}/inbound/application-portal-job/a": dover_job("a")})
        jobs = list(self.board(job_board=False, companies=["moda"], discover=False).search(cx(http)))
        self.assertEqual([j.id for j in jobs], ["dover_a"])

    def test_one_job_through_feed_and_discovery_is_fetched_once(self):
        url = "https://app.dover.com/apply/moda/a"
        http = Api({PAGE(0): {"next": None, "results": [feed_item("a")]},
                    f"{DOVER}/inbound/application-portal-job/a": dover_job("a")})
        jobs = list(self.board().search(cx(http, urls=[url])))
        self.assertEqual([j.id for j in jobs], ["dover_a"])
        self.assertEqual(http.calls.count((f"{DOVER}/inbound/application-portal-job/a", ())), 1)

    def test_closed_inactive_and_private_postings_are_gone_and_a_5xx_is_not(self):
        gone, logs = [], []
        http = Api({f"{DOVER}/inbound/application-portal-job/off": dover_job("off", active=False),
                    f"{DOVER}/inbound/application-portal-job/priv": dover_job("priv", is_private=True),
                    f"{DOVER}/inbound/application-portal-job/err":
                        http_error(f"{DOVER}/inbound/application-portal-job/err", 503)})
        urls = [f"https://app.dover.io/apply/moda/{i}" for i in ("closed", "off", "priv", "err")]
        self.assertEqual(list(self.board(job_board=False).search(cx(http, urls, gone=gone, logs=logs))), [])
        self.assertEqual(sorted(gone), ["dover_closed", "dover_off", "dover_priv"])
        self.assertTrue(any("err" in line and "503" in line for line in logs), logs)

    def test_an_untitled_or_null_posting_is_logged_and_never_marked_gone(self):
        gone, logs = [], []
        http = Api({f"{DOVER}/inbound/application-portal-job/untitled": {"id": "untitled", "active": True},
                    f"{DOVER}/inbound/application-portal-job/null": None})
        urls = [f"https://app.dover.com/apply/moda/{i}" for i in ("untitled", "null")]
        self.assertEqual(list(self.board(job_board=False).search(cx(http, urls, gone=gone, logs=logs))), [])
        self.assertEqual(gone, [])
        for job_id in ("untitled", "null"):
            self.assertTrue(any(job_id in line for line in logs), logs)

    def test_an_unreadable_feed_is_logged_and_the_rest_runs(self):
        logs = []
        http = Api({PAGE(0): http_error(FEED, 500), f"{DOVER}/inbound/application-portal-job/a": dover_job("a")})
        jobs = list(self.board().search(cx(http, urls=["https://app.dover.com/apply/moda/a"], logs=logs)))
        self.assertEqual([j.id for j in jobs], ["dover_a"])
        self.assertTrue(any("job board" in line for line in logs), logs)


GEM = "https://jobs.gem.com/api/public/graphql"


class GemSite:
    """`boards`: slug -> (team name, {extId: (hours ago, title)}); `broken`: extIds whose detail call answers 503."""

    def __init__(self, boards, broken=(), locations=None):
        self.boards, self.broken = boards, set(broken)
        self.locations = [{"name": "Remote"}, {"name": "New York, NY"}] if locations is None else locations

    def __call__(self, operation, v):
        if operation == "JobBoardList":
            team, posts = self.boards.get(v["boardId"], (None, {}))
            return {"data": {"oatsExternalJobPostings": {"jobPostings": [
                {"id": f"g{e}", "extId": e, "title": t} for e, (_, t) in posts.items()]},
                "jobBoardExternal": {"teamDisplayName": team} if team else None}}
        assert operation == "ExternalJobPosting", operation
        if v["extId"] in self.broken:
            return FakeResponse(None, 503)
        team, posts = self.boards.get(v["boardId"], (None, {}))
        if v["extId"] not in posts:
            return {"data": {"oatsExternalJobPosting": None}}
        hours, title = posts[v["extId"]]
        return {"data": {"oatsExternalJobPosting": {
            "id": f"g{v['extId']}", "extId": v["extId"], "title": title, "descriptionHtml": "<p>Make <b>it</b>.</p>",
            "firstPublishedTsSec": int((NOW - timedelta(hours=hours)).timestamp()),
            "locations": self.locations, "job": {"teamDisplayName": team}}}}


class GemApi(Api):
    def request(self, method, url, json=None, **kw):
        assert (method, url) == ("POST", GEM), (method, url)
        self.calls.append((json["operationName"], tuple(sorted(json["variables"].items()))))
        answer = self.post(json["operationName"], json["variables"])
        return answer if isinstance(answer, FakeResponse) else FakeResponse(answer)


class TestGem(unittest.TestCase):
    def board(self, **options):
        from jobhunter.boards.gem import GemBoard
        return GemBoard(options)

    def test_a_company_board_lists_then_reads_each_posting(self):
        ids = ("am9icG9zdDpGZP3aImkqw-wYih0yAWFS", "37c9854b-f1ea-4da6-a3f5-51e64923ae08", "4965519002")
        site = GemSite({"nominal": ("Nominal", {ids[0]: (1, "Backend"), ids[1]: (2, "Data"), ids[2]: (500, "Old")})})
        stale = []
        jobs = list(self.board(companies=["nominal"]).search(cx(GemApi(post=site), stale=stale)))
        self.assertEqual([j.id for j in jobs], [f"gem_{ids[0]}", f"gem_{ids[1]}"])
        self.assertEqual(stale, [f"gem_{ids[2]}"])
        j = jobs[0]
        self.assertEqual((j.title, j.company, j.location, j.url, j.source),
                         ("Backend", "Nominal", "Remote; New York, NY", f"https://jobs.gem.com/nominal/{ids[0]}", "gem"))
        self.assertEqual(j.description, "Make it.")
        posted = datetime.fromisoformat(j.posted_at)          # the date it was posted, read back
        self.assertLess(abs(posted - (NOW - timedelta(hours=1))), timedelta(minutes=5))

    def test_known_postings_are_not_read_again(self):
        http = GemApi(post=GemSite({"nominal": ("Nominal", {"a": (1, "x")})}))
        self.assertEqual(list(self.board(companies=["nominal"]).search(cx(http, known={"gem_a"}))), [])
        self.assertEqual([c[0] for c in http.calls], ["JobBoardList"])

    def test_a_null_or_untitled_posting_is_logged_and_never_marked_gone(self):
        gone, logs = [], []

        class Odd(GemSite):
            def __call__(self, operation, v):
                if operation == "ExternalJobPosting" and v["extId"] == "untitled":
                    return {"data": {"oatsExternalJobPosting": {"id": "g", "extId": "untitled"}}}
                return super().__call__(operation, v)
        http = GemApi(post=Odd({"goodbill": ("Goodbill", {"live": (1, "Full Stack")})}))
        urls = [f"https://jobs.gem.com/goodbill/{i}" for i in ("live", "closed", "untitled")]
        jobs = list(self.board().search(cx(http, urls, gone=gone, logs=logs)))
        self.assertEqual([j.id for j in jobs], ["gem_live"])
        self.assertEqual(gone, [])
        for ext_id in ("closed", "untitled"):
            self.assertTrue(any(ext_id in line for line in logs), logs)

    def test_an_empty_board_yields_nothing_quietly(self):
        logs = []
        site = GemSite({"quiet": ("Quiet", {})})
        self.assertEqual(list(self.board(companies=["quiet"]).search(cx(GemApi(post=site), logs=logs))), [])
        self.assertEqual(logs, [])

    def test_an_unknown_board_name_is_logged(self):
        logs = []
        self.assertEqual(list(self.board(companies=["Nominal"]).search(cx(GemApi(post=GemSite({})), logs=logs))), [])
        self.assertTrue(any("no Gem board named" in line and "Nominal" in line for line in logs), logs)

    def test_a_graphql_error_is_logged_and_never_marks_the_posting_gone(self):
        logs, gone = [], []

        class Erroring(GemSite):
            def __call__(self, operation, v):
                if operation == "ExternalJobPosting":
                    return {"errors": [{"message": "rate limited"}], "data": None}
                return super().__call__(operation, v)
        site = Erroring({"nominal": ("Nominal", {"a": (1, "A")})})
        urls = ["https://jobs.gem.com/nominal/b"]
        self.assertEqual(list(self.board(companies=["nominal"]).search(cx(GemApi(post=site), urls, logs=logs, gone=gone))), [])
        self.assertEqual(gone, [])
        self.assertTrue(any("rate limited" in line for line in logs), logs)

    def test_a_failing_posting_is_logged_and_the_rest_come_through(self):
        logs, gone = [], []
        site = GemSite({"nominal": ("Nominal", {"ok": (1, "A"), "bad": (1, "B")})}, broken={"bad"})
        jobs = list(self.board(companies=["nominal"]).search(cx(GemApi(post=site), logs=logs, gone=gone)))
        self.assertEqual([j.id for j in jobs], ["gem_ok"])
        self.assertEqual(gone, [])
        self.assertTrue(any("bad" in line for line in logs), logs)


ADP = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
CID = "a5625759-3bfa-45e5-889d-6b071698195a"


def adp_item(item_id, external, hours_ago=2, title="Junior Software Developer"):
    fields = [{"stringValue": external, "nameCode": {"codeValue": "ExternalJobID"}}] if external else []
    return {"itemID": item_id, "requisitionTitle": title, "postDate": iso(hours_ago),
            "requisitionLocations": [{"nameCode": {"shortName": "Remote, US"}}], "customFieldGroup": {"stringFields": fields}}


def adp_list(skip):
    return (ADP, (("$skip", skip), ("$top", 20), ("cid", CID), ("lang", "en_US")))


def adp_detail(job_id):
    return (f"{ADP}/{job_id}", (("cid", CID), ("lang", "en_US")))


class TestAdp(unittest.TestCase):
    def board(self, **options):
        from jobhunter.boards.adp import AdpBoard
        return AdpBoard(options)

    def detail(self, job_id, **kw):
        return {**adp_item("x", job_id, **kw), "requisitionDescription": "<div>Write <b>code</b>.</div>"}

    def test_pages_through_a_company_and_reads_new_postings(self):
        first = [adp_item(f"i{n}", str(700000 + n)) for n in range(20)]
        http = Api({adp_list(0): {"jobRequisitions": first, "meta": {"totalNumber": 22}},
                    adp_list(20): {"jobRequisitions": [adp_item("i20", "708187"), adp_item("i21", None, 500)],
                                   "meta": {"totalNumber": 22}},
                    **{adp_detail(str(700000 + n)): self.detail(str(700000 + n)) for n in range(20)},
                    adp_detail("708187"): self.detail("708187")})
        stale = []
        jobs = list(self.board(companies={CID: "JRAD"}).search(cx(http, stale=stale)))
        self.assertEqual(len(jobs), 21)
        self.assertEqual(stale, [f"adp_{CID}_i21"])                 # no ExternalJobID: itemID; stale by its list date
        j = next(j for j in jobs if j.id == f"adp_{CID}_708187")
        self.assertEqual((j.title, j.company, j.location, j.source),
                         ("Junior Software Developer", "JRAD", "Remote, US", "adp"))
        self.assertEqual(j.url, "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
                                f"?cid={CID}&jobId=708187")
        self.assertEqual(j.description, "Write code.")

    def test_a_plain_list_of_companies_uses_the_cid_as_the_name(self):
        http = Api({adp_list(0): {"jobRequisitions": [adp_item("i1", "1")], "meta": {"totalNumber": 1}},
                    adp_detail("1"): self.detail("1")})
        self.assertEqual([j.company for j in self.board(companies=[CID]).search(cx(http))], [CID])

    def test_a_discovered_link_is_read_by_its_job_id_and_only_a_404_is_gone(self):
        gone, logs = [], []
        link = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
                f"?cid={CID}&ccId=19000101_000001&jobId={{}}")
        http = Api({adp_detail("708187"): self.detail("708187"), adp_detail("999"): {"customFieldGroup": {}},
                    adp_detail("998"): None})
        urls = [link.format(i) for i in ("708187", "999", "998", "404")]
        jobs = list(self.board().search(cx(http, urls, gone=gone, logs=logs)))
        self.assertEqual([j.id for j in jobs], [f"adp_{CID}_708187"])
        self.assertEqual(gone, [f"adp_{CID}_404"])                  # a skeleton or null may be a hiccup: logged
        for job_id in ("999", "998"):
            self.assertTrue(any(job_id in line for line in logs), logs)

    def test_known_postings_get_no_detail_request(self):
        http = Api({adp_list(0): {"jobRequisitions": [adp_item("i1", "1")], "meta": {"totalNumber": 1}}})
        self.assertEqual(list(self.board(companies=[CID]).search(cx(http, known={f"adp_{CID}_1"}))), [])
        self.assertEqual(len(http.calls), 1)

class TestApiBoardEdges(unittest.TestCase):
    def test_dover_ids_are_lower_cased_so_one_job_is_one_id(self):
        from jobhunter.boards.dover import DoverBoard
        upper = "CA4DF64B-0051-435F-A8E8-F728642DF3F1"
        http = Api({PAGE(0): {"next": None, "results": [feed_item(upper.lower())]},
                    f"{DOVER}/inbound/application-portal-job/{upper.lower()}": dover_job(upper.lower())})
        gone = []
        jobs = list(DoverBoard({}).search(cx(http, urls=[f"https://app.dover.com/apply/moda/{upper}"], gone=gone)))
        self.assertEqual([j.id for j in jobs], [f"dover_{upper.lower()}"])
        self.assertEqual(gone, [])                                  # the upper-case link is the same job, not a 404
        self.assertEqual(sum("application-portal-job" in str(c) for c in http.calls), 1)

    def test_dover_sample_jobs_are_skipped(self):
        from jobhunter.boards.dover import DoverBoard
        http = Api({f"{DOVER}/careers-page-slug/moda": {"id": "c1", "name": "Moda"},
                    (f"{DOVER}/careers-page/c1/jobs", (("limit", 100), ("offset", 0))):
                        {"next": None, "results": [{"id": "s", "title": "x", "is_published": True, "is_sample": True}]}})
        self.assertEqual(list(DoverBoard({"job_board": False, "companies": ["moda"], "discover": False}).search(cx(http))), [])
        self.assertFalse(any("application-portal-job" in str(c) for c in http.calls))

    def test_a_dover_feed_failure_part_way_keeps_the_pages_read(self):
        from jobhunter.boards.dover import DoverBoard
        logs = []
        http = Api({PAGE(0): {"next": "p2", "results": [feed_item("a", 1)]}, PAGE(1): http_error(FEED, 500),
                    f"{DOVER}/inbound/application-portal-job/a": dover_job("a")})
        jobs = list(DoverBoard({"discover": False}).search(cx(http, logs=logs)))
        self.assertEqual([j.id for j in jobs], ["dover_a"])
        self.assertTrue(any("job board" in line for line in logs), logs)

    def test_adp_cids_are_lower_cased_and_a_missing_name_falls_back_to_the_cid(self):
        from jobhunter.boards.adp import AdpBoard
        http = Api({adp_list(0): {"jobRequisitions": [adp_item("i1", "1")], "meta": {"totalNumber": 1}},
                    adp_detail("1"): {**adp_item("x", "1"), "requisitionDescription": "<p>x</p>"}})
        jobs = list(AdpBoard({"companies": {CID.upper(): None}}).search(cx(http)))
        self.assertEqual([(j.id, j.company) for j in jobs], [(f"adp_{CID}_1", CID)])

    def test_adp_keeps_paging_full_pages_when_the_total_is_missing(self):
        from jobhunter.boards.adp import AdpBoard
        full = [adp_item(f"i{n}", str(n), 500) for n in range(20)]
        http = Api({adp_list(0): {"jobRequisitions": full}, adp_list(20): {"jobRequisitions": [adp_item("j", "99", 500)]}})
        stale = []
        list(AdpBoard({"companies": [CID]}).search(cx(http, stale=stale)))
        self.assertEqual(len(stale), 21)


    def test_an_unknown_location_is_left_empty_not_called_remote(self):
        from jobhunter.boards.adp import AdpBoard
        from jobhunter.boards.dover import DoverBoard
        from jobhunter.boards.gem import GemBoard
        dover = Api({PAGE(0): {"next": None, "results": [feed_item("a")]},
                     f"{DOVER}/inbound/application-portal-job/a": dover_job("a", locations=[])})
        gem = GemApi(post=GemSite({"nominal": ("Nominal", {"a": (1, "A")})}, locations=[]))
        adp = Api({adp_list(0): {"jobRequisitions": [adp_item("i1", "1")], "meta": {"totalNumber": 1}},
                   adp_detail("1"): {**adp_item("x", "1"), "requisitionLocations": [], "requisitionDescription": "x"}})
        for name, jobs in (("dover", DoverBoard({"discover": False}).search(cx(dover))),
                           ("gem", GemBoard({"companies": ["nominal"]}).search(cx(gem))),
                           ("adp", AdpBoard({"companies": [CID]}).search(cx(adp)))):
            with self.subTest(board=name):
                self.assertEqual([j.location for j in jobs], [""])


if __name__ == "__main__":
    unittest.main()
