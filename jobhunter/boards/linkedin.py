"""LinkedIn job search through the `linkedin-jobs-mcp` server, started with npx (needs Node.js) and spoken to with the
`mcp` package.

The server has one tool, search_linkedin_jobs, whose listings have no description. `enrich` reads each new job's
description from LinkedIn's guest job-posting endpoint: one small page per job without a login, where the full job
page was mostly refused with HTTP 429.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup

from jobhunter.boards.common import cutoff, is_fresh
from jobhunter.errors import BoardSkipped
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

_DIGITS = re.compile(r"\d+")
GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{number}"
_JOB_NUMBER = re.compile(r"/view/[^?#]*?(\d{6,})/?(?:[?#]|$)")


def job_number(url: str) -> str | None:
    """The numeric posting id at the end of a /jobs/view/<slug>-<number> URL."""
    m = _JOB_NUMBER.search(url or "")
    return m.group(1) if m else None


def read_guest_posting(markup: str) -> str:
    """The description, then the job criteria ("Seniority level: Mid-Senior level") as lines."""
    soup = BeautifulSoup(markup or "", "html.parser")
    block = soup.find(class_="show-more-less-html__markup")
    text = html_to_text(str(block)) if block else ""
    criteria = []
    for heading in soup.find_all("h3", class_="description__job-criteria-subheader"):
        value = heading.find_next_sibling("span")
        if value and value.get_text(strip=True):
            criteria.append(f"{heading.get_text(' ', strip=True)}: {value.get_text(' ', strip=True)}")
    return "\n\n".join(part for part in (text, "\n".join(criteria)) if part)


class _RateLimited(Exception):
    pass


def clean_company(raw: str) -> str:
    """LinkedIn repeats the company name across lines ("Apple\\n   \\n Apple")."""
    parts = [p.strip() for p in (raw or "").split("\n") if p.strip()]
    return parts[0] if parts else (raw or "").strip()


def normalize(raw_jobs: list, max_hours_old: int, log: Callable[[str], None] | None = None,
              cutoff_at: datetime | None = None) -> list[Job]:
    jobs = []
    for raw in raw_jobs:
        try:
            job = _listing(raw, max_hours_old, cutoff_at)
        except Exception as e:
            if log:
                log(f"linkedin: skipped a listing it could not read ({type(e).__name__}: {e})")
            continue
        if job is not None:
            jobs.append(job)
    return jobs


def _job_key(url: str) -> str:
    """job-notifier's id: the /jobs/view/<slug-number> part (kept as is so imported ids match), without a trailing
    slash; the currentJobId number for search-page links."""
    if "view/" in url:
        return url.split("view/")[1].split("?")[0].split("#")[0].rstrip("/")
    current = parse_qs(urlparse(url).query).get("currentJobId")
    return current[0] if current else url


def _listing(raw: Any, max_hours_old: int, cutoff_at: datetime | None = None) -> Job | None:
    if not isinstance(raw, dict):
        return None
    # linkedin-jobs-mcp currently returns agoTime as null; its date (YYYY-MM-DD) is then the only age.
    if cutoff_at is not None and not raw.get("agoTime") and not is_fresh(raw.get("date"), cutoff_at):
        return None
    ago = (raw.get("agoTime") or "").strip().lower()
    if any(unit in ago for unit in ("day", "week", "month", "year")):
        return None
    if "hour" in ago:
        numbers = _DIGITS.findall(ago)
        if numbers and int(numbers[0]) > max_hours_old:
            return None
    url = (raw.get("jobUrl") or "").strip()
    if not url:
        return None      # job-notifier stored these as "linkedin_", merging unrelated jobs into one row
    return Job(id=f"linkedin_{_job_key(url)}", title=raw.get("position") or "",
               company=clean_company(raw.get("company") or ""), location=raw.get("location") or "",
               url=url, posted_at=raw.get("date") or "", description="", source="linkedin", ats="linkedin")


def _npx_candidates() -> list[Path]:
    def version(p: Path) -> list[int]:
        return [int(x) for x in _DIGITS.findall(p.parts[-3])]
    nvm = sorted(Path.home().glob(".nvm/versions/node/*/bin/npx"), key=version, reverse=True)
    return nvm + [Path("/opt/homebrew/bin/npx"), Path("/usr/local/bin/npx")]


def find_npx(configured: str | None) -> str:
    """npx from settings, PATH, or the usual install places (cron runs with a short PATH)."""
    if configured:
        return str(Path(configured).expanduser())
    found = shutil.which("npx")
    if found:
        return found
    for path in _npx_candidates():
        if path.exists():
            return str(path)
    raise BoardSkipped("needs Node.js (npx): install it, set boards.linkedin.npx to its path, or leave linkedin "
                       "out of boards")


@asynccontextmanager
async def _stdio_session(npx_option: str | None):
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as e:
        raise BoardSkipped("needs the `mcp` package: .venv/bin/pip install -r requirements.txt") from e
    params = StdioServerParameters(command=find_npx(npx_option), args=["-y", "linkedin-jobs-mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@board("linkedin")
class LinkedInBoard:
    default_timeout_s = 1200              # one search per query and location, 5 s apart

    @dataclass
    class Options:
        date_since_posted: str = "past 24 hours"
        limit: int = 25
        delay_s: float = 5.0          # between searches; LinkedIn answers 429 when called faster
        max_hours_old: int = 1        # drop listings LinkedIn marks as older ("2 hours ago"), when it says
        npx: str | None = None        # full path to npx when it is not on PATH

    def __init__(self, options: dict, session_factory: Callable[[str | None], Any] | None = None):
        self.options = self.Options(**options)
        self._open_session = session_factory or _stdio_session

    def enrich(self, job: Job, ctx: SearchContext) -> Job:
        number = job_number(job.url)
        if not number:
            return job
        try:
            resp = ctx.http.request("GET", GUEST_URL.format(number=number))
        except (requests.ConnectionError, requests.Timeout) as e:
            ctx.log(f"linkedin: description for {job.id} unavailable ({type(e).__name__}); retried next run")
            job.extra["description_refused"] = True
            return job
        if resp.status_code in (403, 429) or resp.status_code >= 500:
            ctx.log(f"linkedin: description for {job.id} refused (HTTP {resp.status_code}); retried next run")
            job.extra["description_refused"] = True
            return job
        description = read_guest_posting(resp.text) if resp.status_code == 200 else ""
        if description:
            job.description, job.description_is_snippet = description, False
        else:
            job.description_is_snippet = True
        return job

    def search(self, ctx: SearchContext) -> list[Job]:
        return asyncio.run(self._search(ctx))

    async def _search(self, ctx: SearchContext) -> list[Job]:
        o, jobs, seen = self.options, [], set()
        async with self._open_session(o.npx) as session:
            for query in ctx.queries:
                for location in ctx.locations or [""]:
                    found = await self._call(session, ctx, query, location)
                    for job in normalize(found, o.max_hours_old, ctx.log, cutoff(ctx)):
                        if job.id not in seen:
                            seen.add(job.id)
                            jobs.append(job)
                    await asyncio.sleep(o.delay_s * random.uniform(1.0, 1.3))
        return jobs

    async def _call(self, session: Any, ctx: SearchContext, query: str, location: str) -> list:
        o = self.options
        arguments = {"keyword": query, "location": location, "dateSincePosted": o.date_since_posted,
                     "limit": str(o.limit)}     # the server ignores under_10_applicants, so it is not sent
        for attempt in range(3):
            try:
                resp = await session.call_tool("search_linkedin_jobs", arguments=arguments)
                if not resp.content:
                    return []
                raw = json.loads(resp.content[0].text)
                if isinstance(raw, dict) and not raw.get("success", True):
                    error = str(raw.get("error", "unknown error"))
                    if "429" in error or "too many requests" in error.lower():
                        raise _RateLimited(error)
                    ctx.log(f"linkedin [{query} / {location}]: {error}")
                    return []
                return raw if isinstance(raw, list) else raw.get("jobs", [])
            except _RateLimited as e:
                if attempt == 2:
                    ctx.log(f"linkedin [{query} / {location}]: still rate limited after 3 tries ({e})")
                    return []
                await asyncio.sleep(o.delay_s * 2 ** attempt)
            except Exception as e:
                ctx.log(f"linkedin [{query} / {location}]: {e}")
                return []
        return []
