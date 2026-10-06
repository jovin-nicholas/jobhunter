import json
import unittest
from datetime import datetime, timedelta, timezone

from jobhunter.vc_sources import consider_listings, getro_listings

NOW = datetime.now(timezone.utc)
CUT = NOW - timedelta(hours=72)
GETRO = "https://api.getro.com/api/v2/collections/{}/search/jobs"


class Resp:
    def __init__(self, data=None, text="", status=200):
        self.data, self.text, self.status_code = data, text, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Http:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.handler(method, url, kw)


def gjob(i, hours_ago, url=None, featured=False):
    return {"id": i, "title": f"Engineer {i}", "organization": {"name": f"Co{i}"}, "locations": ["New York, NY", "Remote"],
            "created_at": int((NOW - timedelta(hours=hours_ago)).timestamp()), "featured": featured,
            "url": url if url is not None else f"https://jobs.ashbyhq.com/co{i}/{i}"}


def settings(**kw):
    return {"collections": {189: "Redpoint"}, "job_functions": ["Software Engineering"], "locations": [],
            "seniority": [], "max_pages": 10, **kw}


def pages(*page_jobs, status=200):
    def handler(method, url, kw):
        page = kw["json"]["page"]
        if status != 200:
            return Resp(status=status)
        return Resp({"results": {"count": 99, "jobs": page_jobs[page] if page < len(page_jobs) else []}})
    return handler


class TestGetro(unittest.TestCase):
    def test_reads_pages_until_one_is_older_than_the_cutoff(self):
        http = Http(pages([gjob(1, 1), gjob(2, 2)], [gjob(3, 50), gjob(4, 100)], [gjob(5, 1)]))
        found = getro_listings(http, settings(), CUT, lambda key: False, print)
        self.assertEqual([l.key for l in found], ["getro_1", "getro_2", "getro_3"])
        self.assertEqual(len(http.calls), 2)
        l = found[0]
        self.assertEqual((l.source, l.title, l.company, l.location, l.url),
                         ("Redpoint", "Engineer 1", "Co1", "New York, NY; Remote", "https://jobs.ashbyhq.com/co1/1"))
        posted = datetime.fromisoformat(l.posted_at)          # the date it was posted, read back
        self.assertLess(abs(posted - (NOW - timedelta(hours=1))), timedelta(minutes=5))

    def test_a_page_of_cached_jobs_stops_paging_and_featured_jobs_do_not_count(self):
        http = Http(pages([gjob(9, 500, featured=True), gjob(1, 1), gjob(2, 2)], [gjob(3, 3)]))
        found = getro_listings(http, settings(), CUT, lambda key: key in {"getro_1", "getro_2"}, print, complete={189})
        self.assertEqual([l.key for l in found], [])     # featured 9 is stale; 1 and 2 already cached
        self.assertEqual(len(http.calls), 1)

    def test_cached_jobs_do_not_stop_a_collection_no_run_has_read_to_a_real_stop(self):
        http = Http(pages([gjob(1, 1), gjob(2, 2)], [gjob(3, 3)]))
        finished = set()
        found = getro_listings(http, settings(), CUT, lambda key: key in {"getro_1", "getro_2"}, print,
                               complete=set(), finished=finished)
        self.assertEqual([l.key for l in found], ["getro_3"])     # paged past the cached page 0
        self.assertEqual(len(http.calls), 3)                      # pages 0, 1 and the empty page 2
        self.assertEqual(finished, {189})

    def test_a_cached_job_repeated_on_the_next_page_is_not_new(self):
        http = Http(pages([gjob(1, 1), gjob(2, 2)], [gjob(2, 2)], [gjob(3, 3)]))
        found = getro_listings(http, settings(), CUT, lambda key: key in {"getro_1", "getro_2"}, print)
        self.assertEqual((found, len(http.calls)), ([], 2))

    def test_only_a_real_stop_finishes_a_collection(self):
        stops = {"empty page": pages([gjob(1, 1)]), "older than the cut-off": pages([gjob(1, 1), gjob(2, 100)]),
                 "nothing new": pages([gjob(1, 1)], [gjob(1, 1)])}
        for why, handler in stops.items():
            finished = set()
            getro_listings(Http(handler), settings(), CUT, lambda k: False, print, finished=finished)
            self.assertEqual(finished, {189}, why)
        many = Http(lambda m, u, kw: Resp({"results": {"jobs": [gjob(kw["json"]["page"] + 1, 1)]}}))
        failing = Http(lambda m, u, kw: Resp({"results": {"jobs": [gjob(1, 1)]}}) if kw["json"]["page"] == 0
                       else Resp(status=500))
        for why, http in {"max_pages": many, "a failing page": failing}.items():
            finished = set()
            getro_listings(http, settings(max_pages=3), CUT, lambda k: False, lambda line: None, finished=finished)
            self.assertEqual(finished, set(), why)

    def test_request_shape_and_filters(self):
        http = Http(pages([]))
        getro_listings(http, settings(locations=["United States"], seniority=["entry_level"]), CUT, lambda k: False, print)
        method, url, kw = http.calls[0]
        self.assertEqual((method, url), ("POST", GETRO.format(189)))
        self.assertEqual(kw["headers"]["Accept"], "application/json")
        self.assertEqual(kw["json"], {"hitsPerPage": 20, "page": 0, "query": "",
                                      "filters": {"job_functions": ["Software Engineering"],
                                                  "searchable_locations": ["United States"], "seniority": ["entry_level"]}})
        getro_listings(http, settings(), CUT, lambda k: False, print)
        self.assertEqual(http.calls[-1][2]["json"]["filters"], {"job_functions": ["Software Engineering"]})

    def test_max_pages_and_bad_urls(self):
        http = Http(lambda m, u, kw: Resp({"results": {"jobs": [gjob(kw["json"]["page"] * 10 + 1, 1), gjob(77, 1, url=""),
                                                                 gjob(78, 1, url="javascript:alert(1)")]}}))
        found = getro_listings(http, settings(max_pages=3), CUT, lambda k: False, print)
        self.assertEqual(len(http.calls), 3)
        self.assertEqual([l.key for l in found], ["getro_1", "getro_11", "getro_21"])

    def test_a_failing_collection_is_logged_and_the_others_read(self):
        logs = []

        def handler(method, url, kw):
            return Resp(status=500) if "/189/" in url else Resp({"results": {"jobs": [gjob(1, 1)]}})
        found = getro_listings(Http(handler), settings(collections={189: "Redpoint", 1124: "Primary"}), CUT,
                               lambda k: False, logs.append)
        self.assertEqual([(l.key, l.source) for l in found], [("getro_1", "Primary")])
        self.assertTrue(any("Redpoint" in line and "500" in line for line in logs), logs)


def flight_page(jobs, extra_bad=False):
    payload = json.dumps({"initialData": {"jobs": jobs}})
    pushes = [f"self.__next_f.push([1,{json.dumps('0:' + payload[:40])}])",
              f"self.__next_f.push([1,{json.dumps(payload[40:])}])"]
    if extra_bad:
        pushes.insert(1, 'self.__next_f.push([1,"broken \\x"])')
    return "<html><body>" + "".join(f"<script>{p}</script>" for p in pushes) + "</body></html>"


def cjob(i, hours_ago, url="https://job-boards.greenhouse.io/omadahealth/jobs/8257928"):
    return {"id": f"ats:{i}", "title": f"Engineer {i}", "company_name": "Omada Health", "location": "Remote, USA",
            "posted_at": (NOW - timedelta(hours=hours_ago)).isoformat(), "apply_url": url}


class TestConsider(unittest.TestCase):
    CFG = {"boards": {"jobs.a16z.com": "a16z"}, "roles": ["software-engineer"]}

    def test_jobs_are_read_from_the_flight_data(self):
        http = Http(lambda m, u, kw: Resp(text=flight_page([cjob(1, 2), cjob(2, 500)], extra_bad=True)))
        found = consider_listings(http, self.CFG, CUT, print)
        self.assertEqual(http.calls[0][:2], ("GET", "https://jobs.a16z.com/jobs"))
        self.assertEqual(http.calls[0][2]["params"], {"role": "software-engineer"})
        self.assertEqual([(l.key, l.source, l.company, l.url) for l in found],
                         [("consider_ats:1", "a16z", "Omada Health", "https://job-boards.greenhouse.io/omadahealth/jobs/8257928")])

    def test_a_page_without_a_job_list_is_logged(self):
        logs = []
        self.assertEqual(consider_listings(Http(lambda m, u, kw: Resp(text="<html></html>")), self.CFG, CUT, logs.append), [])
        self.assertTrue(any("no job list found" in line for line in logs), logs)

    def test_a_page_without_a_job_list_is_recorded_as_a_failure(self):
        failures = []
        consider_listings(Http(lambda m, u, kw: Resp(text="<html></html>")), self.CFG, CUT, lambda line: None,
                          failures=failures)
        self.assertEqual(len(failures), 1)
        self.assertIn("jobs.a16z.com", failures[0])
        self.assertIn("page format", failures[0])


if __name__ == "__main__":
    unittest.main()
