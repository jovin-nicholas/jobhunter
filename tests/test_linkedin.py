import json
import shutil
import tempfile
import unittest
import unittest.mock
from contextlib import asynccontextmanager
from types import SimpleNamespace

from jobhunter.boards.linkedin import LinkedInBoard, clean_company, find_npx, normalize
from jobhunter.errors import BoardError
import requests

from jobhunter.models import SearchContext
from tests.helpers import FakeResponse


def li(n, ago="30 minutes ago", url=None):
    return {"position": f"Engineer {n}", "company": "Acme\n          \n      Acme", "location": "Austin, TX",
            "date": "2026-09-28", "agoTime": ago,
            "jobUrl": url if url is not None else f"https://www.linkedin.com/jobs/view/engineer-at-acme-{n}?trk=x"}


class FakeSession:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(reply))])


def factory(session):
    @asynccontextmanager
    async def open_session(npx_option, cwd):
        yield session
    return open_session


def ctx(queries=("swe",), locations=("Austin, TX",), logs=None, http=None):
    return SearchContext(list(queries), list(locations), 72, http, (logs if logs is not None else []).append)


class TestNormalize(unittest.TestCase):
    def test_fields_ids_and_age_filter(self):
        jobs = normalize([li(1), li(2, "an hour ago"), li(3, "2 hours ago"), li(4, "3 days ago"),
                          li(5, url=""), li(6, url="https://www.linkedin.com/company/acme"),
                          {**li(7), "jobUrl": ["not", "text"]}], max_hours_old=1)
        self.assertEqual([j.id for j in jobs], ["linkedin_engineer-at-acme-1", "linkedin_engineer-at-acme-2",
                                                "linkedin_https://www.linkedin.com/company/acme"])
        self.assertEqual((jobs[0].company, jobs[0].title, jobs[0].description, jobs[0].ats),
                         ("Acme", "Engineer 1", "", "linkedin"))

    def test_clean_company(self):
        self.assertEqual(clean_company("Apple\n   \n  Apple"), "Apple")
        self.assertEqual(clean_company("  Beta  "), "Beta")


class TestLinkedInBoard(unittest.TestCase):
    def test_every_query_and_location_is_searched_once_per_job(self):
        session = FakeSession([[li(1), li(2)], {"jobs": [li(2), li(3)]}])
        board = LinkedInBoard({"delay_s": 0}, session_factory=factory(session))
        jobs = board.search(ctx(locations=["Austin, TX", "Remote"]))
        self.assertEqual([j.id for j in jobs], [f"linkedin_engineer-at-acme-{n}" for n in (1, 2, 3)])
        self.assertEqual([c[1]["location"] for c in session.calls], ["Austin, TX", "Remote"])
        self.assertEqual(session.calls[0][1]["dateSincePosted"], "past 24 hours")
        self.assertEqual(session.calls[0][1]["limit"], "25")

    def test_rate_limit_is_retried(self):
        session = FakeSession([{"success": False, "error": "429 Too Many Requests"}, [li(1)]])
        jobs = LinkedInBoard({"delay_s": 0}, session_factory=factory(session)).search(ctx())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(session.calls), 2)

    def test_one_failing_search_does_not_stop_the_others(self):
        logs = []
        session = FakeSession([RuntimeError("server crashed"), [li(1)]])
        jobs = LinkedInBoard({"delay_s": 0}, session_factory=factory(session)).search(
            ctx(queries=["a", "b"], logs=logs))
        self.assertEqual(len(jobs), 1)
        self.assertTrue(any("server crashed" in line for line in logs), logs)

    def test_constructing_the_board_does_not_need_npx(self):
        LinkedInBoard({"npx": "/nonexistent/npx"})

    def test_the_server_is_pinned_and_runs_outside_the_repo(self):
        from pathlib import Path

        from jobhunter.boards.linkedin import MCP_PACKAGE, server_params
        self.assertRegex(MCP_PACKAGE, r"^linkedin-jobs-mcp@\d+\.\d+\.\d+$")
        params = server_params("/usr/local/bin/npx")
        self.addCleanup(shutil.rmtree, params.cwd, True)
        self.assertEqual((params.command, params.args), ("/usr/local/bin/npx", ["-y", MCP_PACKAGE]))
        repo = Path(__file__).resolve().parent.parent
        self.assertFalse(Path(params.cwd).resolve().is_relative_to(repo))
        self.assertNotEqual(Path(params.cwd).resolve(), Path(tempfile.gettempdir()).resolve())
        self.assertEqual(Path(params.cwd).stat().st_mode & 0o777, 0o700)

    def test_without_a_data_folder_one_private_folder_serves_the_whole_process(self):
        from jobhunter.boards.linkedin import private_dir
        first = private_dir(None)
        self.addCleanup(shutil.rmtree, first, True)
        self.assertEqual(private_dir(None), first)
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)

    def test_npx_runs_in_a_private_folder_in_the_data_folder(self):
        from pathlib import Path

        from jobhunter.boards.linkedin import private_dir
        data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, data, True)
        folder = private_dir(data)
        self.assertEqual(folder, data / "npx")
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        folder.chmod(0o755)
        self.assertEqual(private_dir(data).stat().st_mode & 0o777, 0o700)      # tightened again on the next run

    def test_the_session_is_opened_in_the_private_folder(self):
        from pathlib import Path
        seen = []

        @asynccontextmanager
        async def open_session(npx_option, cwd):
            seen.append(cwd)
            yield FakeSession([])
        data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, data, True)
        c = ctx(queries=())
        c.data_dir = data
        LinkedInBoard({"delay_s": 0}, session_factory=open_session).search(c)
        self.assertEqual(seen, [data / "npx"])

    def test_find_npx(self):
        self.assertEqual(find_npx("~/bin/npx").endswith("/bin/npx"), True)
        with unittest.mock.patch("jobhunter.boards.linkedin.shutil.which", return_value=None), \
             unittest.mock.patch("jobhunter.boards.linkedin._npx_candidates", return_value=[]):
            with self.assertRaises(BoardError):
                find_npx(None)


POSTING_PAGE = """<html><body><section class="description">
<div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5">
<p>You will build <strong>Java</strong> services.</p><ul><li>2+ years with Spring Boot</li></ul>
</div></section>
<ul class="description__job-criteria-list">
<li><h3 class="description__job-criteria-subheader">Seniority level</h3>
<span class="description__job-criteria-text">Mid-Senior level</span></li>
<li><h3 class="description__job-criteria-subheader">Employment type</h3>
<span class="description__job-criteria-text">Full-time</span></li>
</ul></body></html>"""


class GuestHttp:
    def __init__(self, status=200, text=POSTING_PAGE, error=None):
        self.status, self.text, self.error, self.calls = status, text, error, []

    def request(self, method, url, **kw):
        self.calls.append(url)
        if self.error:
            raise self.error
        return FakeResponse(status_code=self.status, text=self.text)


def li_job(url="https://www.linkedin.com/jobs/view/backend-engineer-at-acme-4470827854?trk=x"):
    return normalize([li(1, url=url)], max_hours_old=1)[0]


class TestLinkedInDescriptions(unittest.TestCase):
    def test_description_and_criteria_come_from_the_guest_endpoint(self):
        http, logs = GuestHttp(), []
        job = LinkedInBoard({}).enrich(li_job(), ctx(logs=logs, http=http))
        self.assertEqual(http.calls, ["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4470827854"])
        self.assertEqual(job.description, "You will build Java services.\n2+ years with Spring Boot\n\n"
                                          "Seniority level: Mid-Senior level\nEmployment type: Full-time")
        self.assertIs(job.description_is_snippet, False)
        self.assertNotIn("description_refused", job.extra)

    def test_a_refused_request_is_marked_for_retry(self):
        for http in (GuestHttp(status=429, text=""), GuestHttp(status=503, text=""),
                     GuestHttp(error=requests.Timeout("slow"))):
            job = LinkedInBoard({}).enrich(li_job(), ctx(http=http))
            self.assertTrue(job.extra.get("description_refused"), http.status)
            self.assertEqual(job.description, "")

    def test_a_missing_posting_is_not_a_refusal(self):
        job = LinkedInBoard({}).enrich(li_job(), ctx(http=GuestHttp(status=404, text="")))
        self.assertNotIn("description_refused", job.extra)
        self.assertIs(job.description_is_snippet, True)

    def test_a_url_without_a_job_number_makes_no_request(self):
        http = GuestHttp()
        job = LinkedInBoard({}).enrich(li_job("https://www.linkedin.com/company/acme"), ctx(http=http))
        self.assertEqual(http.calls, [])
        self.assertEqual(job.description, "")


class TestLinkedInForbidden(unittest.TestCase):
    def test_a_403_is_a_refusal(self):
        job = LinkedInBoard({}).enrich(li_job(), ctx(http=GuestHttp(status=403, text="")))
        self.assertTrue(job.extra.get("description_refused"))


class TestLinkedInContract(unittest.TestCase):
    """What linkedin-jobs-mcp actually returns: agoTime is null, date is YYYY-MM-DD, under_10_applicants is ignored."""

    def test_the_listing_date_decides_when_ago_time_is_missing(self):
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=5)).date().isoformat()
        raws = [{**li(1), "agoTime": None, "date": today}, {**li(2), "agoTime": None, "date": old}]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
        self.assertEqual([j.id for j in normalize(raws, 1, cutoff_at=cutoff)], ["linkedin_engineer-at-acme-1"])

    def test_ids_do_not_depend_on_a_trailing_slash_or_the_url_form(self):
        cases = {"https://www.linkedin.com/jobs/view/4471288751/?trk=x": "linkedin_4471288751",
                 "https://www.linkedin.com/jobs/view/backend-at-acme-4471288751?trk=x": "linkedin_backend-at-acme-4471288751",
                 "https://www.linkedin.com/jobs/search/?currentJobId=4471288751&keywords=x": "linkedin_4471288751"}
        for url, expected in cases.items():
            self.assertEqual(normalize([li(1, url=url)], 1)[0].id, expected, url)

    def test_ignored_options_are_not_sent(self):
        session = FakeSession([[li(1)]])
        LinkedInBoard({"delay_s": 0}, session_factory=factory(session)).search(ctx())
        self.assertNotIn("under_10_applicants", session.calls[0][1])
        with self.assertRaises(TypeError):
            LinkedInBoard({"under_10_applicants": True})


class TestProcessFolderCleanup(unittest.TestCase):
    def test_the_process_folder_is_removed_when_the_process_exits(self):
        from unittest.mock import patch
        from jobhunter.boards import linkedin
        with patch.object(linkedin, "_process_dir", None), patch("jobhunter.boards.linkedin.atexit.register") as reg:
            folder = linkedin.private_dir(None)
            self.addCleanup(shutil.rmtree, folder, True)
            self.assertEqual([c.args for c in reg.call_args_list], [(linkedin._remove_process_dir,)])
            linkedin._remove_process_dir()
            self.assertFalse(folder.exists())
            again = linkedin.private_dir(None)                  # a later call in the same process makes a new one
            self.addCleanup(shutil.rmtree, again, True)
            self.assertTrue(again.is_dir())


if __name__ == "__main__":
    unittest.main()
