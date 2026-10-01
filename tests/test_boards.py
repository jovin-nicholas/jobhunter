import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from jobhunter.boards.dice import DiceBoard
from jobhunter.boards.greenhouse import GreenhouseBoard
from jobhunter.http import Http
from jobhunter.models import Job, SearchContext
from jobhunter.text_clean import html_to_text
from tests.helpers import FakeResponse

GUID = "ba5e69de-1111-2222-3333-444455556666"


class FakeSession:
    def __init__(self):
        self.calls, self.headers = [], {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return FakeResponse(json_data={"ok": True})


class TestHttp(unittest.TestCase):
    def test_requests_to_the_same_host_are_spaced_out(self):
        http = Http(min_interval_s=0.5, session=FakeSession())
        with patch("jobhunter.http.time.monotonic", side_effect=[0.0, 0.1, 0.1]), \
             patch("jobhunter.http.time.sleep") as sleep:
            http.get_json("https://a.example/1")
            http.get_json("https://a.example/2")
            http.get_json("https://b.example/1")
        self.assertEqual(sleep.call_count, 1)
        self.assertAlmostEqual(sleep.call_args.args[0], 0.4)

    def test_default_timeout_is_applied(self):
        session = FakeSession()
        Http(min_interval_s=0, timeout_s=7, session=session).get_json("https://a.example/1")
        self.assertEqual(session.calls[0][2]["timeout"], 7)


class TestHtmlToText(unittest.TestCase):
    def test_line_breaks_only_at_block_boundaries(self):
        self.assertEqual(html_to_text("<p>We <b>are</b> hiring.</p><ul><li>Java</li><li>Spring <span>Boot</span></li>"
                                      "</ul>line<br>two"), "We are hiring.\nJava\nSpring Boot\nline\ntwo")


class FakeDice:
    """Answers the MCP exchange: initialize, notifications/initialized, tools/call."""

    def __init__(self, tools):
        self.tools, self.tool_calls = tools, []

    def request(self, method, url, json=None, headers=None, **kw):
        if json["method"] == "initialize":
            return FakeResponse(text='data: {"jsonrpc": "2.0", "id": 1, "result": {}}', headers={"mcp-session-id": "s1"})
        if json["method"] == "notifications/initialized":
            return FakeResponse(status_code=202, text="")
        name, args = json["params"]["name"], json["params"]["arguments"]
        self.tool_calls.append((name, args, dict(headers)))
        out = self.tools[name](args)
        content = [{"type": "text", "text": out if isinstance(out, str) else _json.dumps(out)}]
        body = {"jsonrpc": "2.0", "id": 2, "result": {"content": content, "isError": isinstance(out, str)}}
        return FakeResponse(text="data: " + _json.dumps(body))


_json = json


def dice_job(i="abc", guid=GUID, posted_hours_ago=2):
    return {"id": i, "guid": guid, "title": "Backend Engineer", "companyName": "Acme",
            "jobLocation": {"displayName": "Austin, Texas, USA"}, "summary": "x" * 500,
            "detailsPageUrl": f"https://www.dice.com/job-detail/{guid}?utm_source=x",
            "postedDate": (datetime.now(timezone.utc) - timedelta(hours=posted_hours_ago)).isoformat()}


def ctx(http, logs=None, locations=("Austin, TX",)):
    return SearchContext(["backend engineer"], list(locations), 72, http, (logs if logs is not None else []).append)


class TestDice(unittest.TestCase):
    def test_search_normalises_and_dedupes_across_locations(self):
        fake = FakeDice({"search_jobs": lambda a: {"data": [dice_job()]}})
        jobs = list(DiceBoard({}).search(ctx(fake, locations=["Austin, TX", "Remote"])))
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual((job.id, job.company, job.location, job.source, job.description_is_snippet),
                         ("dice_abc", "Acme", "Austin, Texas, USA", "dice", True))
        self.assertEqual(job.extra["guid"], GUID)
        self.assertEqual(fake.tool_calls[0][2]["mcp-session-id"], "s1")

    def test_enrich_fetches_the_full_description_by_guid(self):
        fake = FakeDice({"get_job_details": lambda a: {"description": "<p>Build <b>APIs</b></p>", "skills": []}})
        board = DiceBoard({})
        job = board.enrich(Job("dice_abc", "t", "c", "l", "https://www.dice.com/", extra={"guid": GUID}), ctx(fake))
        self.assertEqual((job.description, job.description_is_snippet), ("Build APIs", False))
        self.assertEqual(fake.tool_calls[0][1], {"job_id": GUID})

    def test_enrich_falls_back_to_the_guid_in_the_url(self):
        fake = FakeDice({"get_job_details": lambda a: {"description": "Full text"}})
        job = DiceBoard({}).enrich(Job("dice_abc", "t", "c", "l", f"https://www.dice.com/job-detail/{GUID}?x=1"),
                                   ctx(fake))
        self.assertEqual(fake.tool_calls[0][1], {"job_id": GUID})
        self.assertEqual(job.description, "Full text")

    def test_enrich_failure_keeps_the_summary_and_flags_it(self):
        fake = FakeDice({"get_job_details": lambda a: "Job not found"})
        job = DiceBoard({}).enrich(Job("dice_abc", "t", "c", "l", "u", description="summary", extra={"guid": GUID}),
                                   ctx(fake))
        self.assertEqual((job.description, job.description_is_snippet), ("summary", True))

    def test_one_failing_search_does_not_stop_the_others(self):
        answers = iter(["rate limited", {"data": [dice_job()]}])
        fake, logs = FakeDice({"search_jobs": lambda a: next(answers)}), []
        jobs = list(DiceBoard({}).search(ctx(fake, logs, locations=["Austin, TX", "Remote"])))
        self.assertEqual(len(jobs), 1)
        self.assertTrue(any("rate limited" in line for line in logs), logs)


class FakeGreenhouse:
    def __init__(self, by_slug):
        self.by_slug, self.calls = by_slug, []

    def get_json(self, url, **kw):
        self.calls.append((url, kw))
        slug = url.rstrip("/").split("/")[-2]
        value = self.by_slug[slug]
        if isinstance(value, Exception):
            raise value
        return value


def gh_job(i, hours_ago, **kw):
    updated = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {"id": i, "title": "Software Engineer", "updated_at": updated, "location": {"name": "Remote - US"},
            "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
            "content": "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;", **kw}


class TestGreenhouse(unittest.TestCase):
    def test_lists_fresh_jobs_per_company(self):
        fake = FakeGreenhouse({"acme": {"jobs": [gh_job(1, 1), gh_job(2, 200), gh_job(3, 2, company_name="Acme Inc")]}})
        jobs = list(GreenhouseBoard({"companies": ["acme"]}).search(ctx(fake)))
        self.assertEqual([j.id for j in jobs], ["greenhouse_1", "greenhouse_3"])
        self.assertEqual((jobs[0].company, jobs[1].company), ("acme", "Acme Inc"))
        self.assertEqual((jobs[0].description, jobs[0].location, jobs[0].description_is_snippet),
                         ("Build & ship", "Remote - US", False))
        self.assertEqual(fake.calls[0], ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", {"params": {"content": "true"}}))

    def test_one_failing_company_does_not_stop_the_others(self):
        fake, logs = FakeGreenhouse({"gone": RuntimeError("HTTP 404"), "acme": {"jobs": [gh_job(1, 1)]}}), []
        jobs = list(GreenhouseBoard({"companies": ["gone", "acme"]}).search(ctx(fake, logs)))
        self.assertEqual(len(jobs), 1)
        self.assertTrue(any("gone" in line and "404" in line for line in logs), logs)



class TestDiceRemote(unittest.TestCase):
    def test_remote_job_without_a_location_is_remote_us(self):
        raw = dice_job()
        raw["jobLocation"], raw["isRemote"], raw["workplaceTypes"] = None, True, ["Remote"]
        fake = FakeDice({"search_jobs": lambda a: {"data": [raw]}})
        self.assertEqual(list(DiceBoard({}).search(ctx(fake)))[0].location, "Remote - US")


class TestHostSpacing(unittest.TestCase):
    def test_linkedin_hosts_share_one_slower_spacing(self):
        http = Http(min_interval_s=0.5, session=FakeSession())
        with patch("jobhunter.http.time.monotonic", return_value=0.0), patch("jobhunter.http.time.sleep") as sleep:
            http.request("GET", "https://www.linkedin.com/jobs/view/1")
            http.request("GET", "https://in.linkedin.com/jobs/view/2")
            http.request("GET", "https://example.com/1")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [3.0])


class TestDiceRequests(unittest.TestCase):
    def test_recent_postings_newest_first_and_many_per_page(self):
        fake = FakeDice({"search_jobs": lambda a: {"data": [dice_job()], "meta": {"page": 1, "totalPages": 1}}})
        list(DiceBoard({}).search(ctx(fake)))
        args = fake.tool_calls[0][1]
        self.assertEqual({k: args[k] for k in ("jobs_per_page", "posted_date", "sort", "page_number")},
                         {"jobs_per_page": 100, "posted_date": "THREE", "sort": "datePosted", "page_number": 1})

    def test_posted_date_follows_max_age_hours(self):
        for hours, expected in ((12, "ONE"), (24, "ONE"), (72, "THREE"), (100, "SEVEN"), (500, "SEVEN")):
            fake = FakeDice({"search_jobs": lambda a: {"data": []}})
            c = ctx(fake)
            c.max_age_hours = hours
            list(DiceBoard({}).search(c))
            self.assertEqual(fake.tool_calls[0][1]["posted_date"], expected, hours)

    def test_further_pages_are_read_up_to_max_pages(self):
        pages = {1: [dice_job("a")], 2: [dice_job("b")], 3: [dice_job("c")]}
        fake = FakeDice({"search_jobs": lambda a: {"data": pages[a["page_number"]], "meta": {"totalPages": 5}}})
        jobs = list(DiceBoard({"max_pages": 2}).search(ctx(fake)))
        self.assertEqual([j.id for j in jobs], ["dice_a", "dice_b"])
        self.assertEqual([c[1]["page_number"] for c in fake.tool_calls], [1, 2])

    def test_old_postings_are_skipped(self):
        fake = FakeDice({"search_jobs": lambda a: {"data": [dice_job("new"), dice_job("old", posted_hours_ago=500)]}})
        self.assertEqual([j.id for j in DiceBoard({}).search(ctx(fake))], ["dice_new"])

    def test_the_reply_is_found_among_other_stream_events(self):
        from jobhunter.boards.dice import _parse_mcp
        text = ('event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/message", "params": {}}\n\n'
                'event: message\ndata: {"jsonrpc": "2.0", "id": 2, "result": {"content": []}}\n')
        self.assertEqual(_parse_mcp(text), {"jsonrpc": "2.0", "id": 2, "result": {"content": []}})


class TestDicePagingValues(unittest.TestCase):
    def test_an_odd_paging_value_ends_the_paging_but_keeps_the_jobs(self):
        fake = FakeDice({"search_jobs": lambda a: {"data": [dice_job("a")], "meta": {"totalPages": "many"}}})
        self.assertEqual([j.id for j in DiceBoard({}).search(ctx(fake))], ["dice_a"])
        fake = FakeDice({"search_jobs": lambda a: {"data": [dice_job("b")], "meta": ["not", "a", "dict"]}})
        self.assertEqual([j.id for j in DiceBoard({}).search(ctx(fake))], ["dice_b"])


if __name__ == "__main__":
    unittest.main()
