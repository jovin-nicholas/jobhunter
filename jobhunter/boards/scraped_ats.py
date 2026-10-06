"""Workday postings found by discovery. Workday has no public job API, so each posting page is downloaded and read: its
JSON-LD JobPosting block when it has one, otherwise the most job-like block of text. (Dover, Gem and ADP read their own
JSON APIs: dover.py, gem.py, adp.py.)"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobhunter.boards.ats_urls import Posting, ats_of, discovered
from jobhunter.boards.common import cutoff, each_listing, is_fresh, is_gone
from jobhunter.models import Job, SearchContext
from jobhunter.page_text import BROWSER_UA, MAX_REDIRECTS, best_description
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

HEADERS = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}


def _ld_location(value: Any) -> str:
    places = value if isinstance(value, list) else [value]
    out = []
    for place in places:
        address = place.get("address") if isinstance(place, dict) else None
        if isinstance(address, dict):
            parts = [address.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
            text = ", ".join(p for p in parts if isinstance(p, str) and p)
            if text:
                out.append(text)
    return "; ".join(out)


def _job_posting(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def read_posting(markup: str) -> dict:
    """title, description, location and posted_at from a posting page ("" when not found), and has_job: whether the
    page shows a job at all (structured job data or a heading). A <title> alone is not a job: closed Workday postings
    and JavaScript-only pages keep the site's title ("Careers")."""
    soup = BeautifulSoup(markup or "", "html.parser")
    info = {"title": "", "description": "", "location": "", "posted_at": ""}
    ld = _job_posting(soup)
    if ld:
        description = str(ld.get("description") or "")
        if "&lt;" in description:
            description = html.unescape(description)
        info.update(title=str(ld.get("title") or ""), description=html_to_text(description),
                    location=_ld_location(ld.get("jobLocation")), posted_at=str(ld.get("datePosted") or ""))
    heading = soup.find("h1")
    heading_text = heading.get_text(" ", strip=True) if heading else ""
    if not info["title"]:
        page_title = soup.title.get_text(strip=True) if soup.title else ""
        info["title"] = heading_text or page_title.split(" | ")[0]
    info["has_job"] = bool(ld) or bool(heading_text)
    if info["has_job"] and not info["description"]:
        info["description"] = best_description(markup)
    return info


class _ScrapedBoard:
    ats = ""
    # Workday answers a closed posting with an ordinary, empty page. A subclass for a site whose empty page may be a
    # posting that needs JavaScript leaves this False, so the posting is tried again next run rather than recorded as gone.
    empty_page_is_gone = False

    @dataclass
    class Options:
        discover: bool = True

    default_timeout_s = 1800              # one page per discovered posting

    def __init__(self, options: dict):
        self.options = self.Options(**options)

    def job_id(self, p: Posting) -> str:
        return f"{self.ats}_{p.job_id}"

    def _get(self, ctx: SearchContext, p: Posting) -> Any:
        """The posting page, following redirects by hand and only within the same ATS (a redirect elsewhere could
        point at a private address); None when a redirect leaves the ATS or there are too many."""
        url = p.url
        for _ in range(MAX_REDIRECTS + 1):
            resp = ctx.http.request("GET", url, headers=HEADERS, allow_redirects=False)
            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            target = urljoin(url, resp.headers.get("Location", ""))
            if ats_of(target) != self.ats:
                ctx.log(f"{self.ats} [{p.slug}/{p.job_id}]: redirect away from {self.ats} not followed")
                return None
            url = target
        ctx.log(f"{self.ats} [{p.slug}/{p.job_id}]: too many redirects; tried again next run")
        return None

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        if not self.options.discover:
            return
        limit = cutoff(ctx)

        def posting(p: Posting) -> Job | None:
            job_id = self.job_id(p)
            if ctx.is_known(job_id):
                return None
            try:
                resp = self._get(ctx, p)
                if resp is None:
                    return None
                if resp.status_code in (404, 410):
                    ctx.log(f"{self.ats} [{p.slug}/{p.job_id}]: gone (HTTP {resp.status_code})")
                    ctx.mark_gone(job_id)
                    return None
                resp.raise_for_status()
            except Exception as e:
                ctx.log(f"{self.ats} [{p.slug}/{p.job_id}]: {'gone' if is_gone(e) else 'failed'}: {e}")
                if is_gone(e):
                    ctx.mark_gone(job_id)
                return None
            info = read_posting(resp.text)
            if not info["has_job"]:
                if self.empty_page_is_gone:
                    ctx.log(f"{self.ats} [{p.slug}/{p.job_id}]: gone (the page has no job)")
                    ctx.mark_gone(job_id)
                else:
                    ctx.log(f"{self.ats} [{p.slug}/{p.job_id}]: no job data on the page; tried again next run")
                return None
            if not is_fresh(info["posted_at"], limit):
                ctx.mark_stale(job_id)
                return None
            return Job(id=job_id, title=info["title"], company=p.slug, location=info["location"], url=p.url,
                       posted_at=info["posted_at"], description=info["description"], source=self.ats, ats=self.ats,
                       description_is_snippet=False if info["description"] else None)

        yield from each_listing(ctx, self.ats, discovered(ctx, self.ats), posting)


@board("workday")
class WorkdayBoard(_ScrapedBoard):
    ats = "workday"
    empty_page_is_gone = True
