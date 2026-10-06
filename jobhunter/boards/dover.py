"""Dover: its public job board feed (every company on Dover), companies' careers pages, and discovered links, all read
from Dover's own JSON API. The job pages are a JavaScript app with no job in their HTML."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import quote

from jobhunter.boards.ats_urls import discovered
from jobhunter.boards.common import cutoff, each_listing, is_fresh, is_gone
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

API = "https://app.dover.com/api/v1"
FEED_URL = API + "/job-board/jobs/"                    # the one Dover path that keeps its trailing slash
SLUG_URL = API + "/careers-page-slug/{slug}"           # every other path must have none: with one, Dover answers
COMPANY_JOBS_URL = API + "/careers-page/{client_id}/jobs"   # its web app's HTML instead of JSON
POSTING_URL = API + "/inbound/application-portal-job/{job_id}"
PAGE_SIZE, MAX_PAGES = 100, 20


def apply_url(company: str, job_id: str) -> str:
    return f"https://app.dover.com/apply/{quote(company or 'company', safe='')}/{job_id}"


def pages(ctx: SearchContext, url: str, older_than: datetime | None = None, label: str = "") -> list[dict]:
    """Every result of a paged Dover list (count / next / results), at most MAX_PAGES pages. With `older_than`, a list
    sorted newest first (the feed) stops after the first page that reaches that age: nothing fresh can follow. With
    `label`, a page that fails part-way is logged under it and the pages already read are kept."""
    found, offset = [], 0
    for _ in range(MAX_PAGES):
        try:
            data = ctx.http.get_json(url, params={"limit": PAGE_SIZE, "offset": offset})
        except Exception as e:
            if not label or not found:
                raise
            ctx.log(f"dover [{label}]: stopped after {len(found)} listings: {e}")
            break
        results = [r for r in (data.get("results") or []) if isinstance(r, dict) and r.get("id")]
        found += results
        if not data.get("next") or not results:
            break
        if older_than is not None and not is_fresh(results[-1].get("date_posted"), older_than):
            break
        offset += len(results)
    return found


@board("dover")
class DoverBoard:
    @dataclass
    class Options:
        job_board: bool = True                              # Dover's public feed of every company's jobs
        companies: list[str] = field(default_factory=list)  # careers-page names, e.g. moda
        discover: bool = True

    default_timeout_s = 1800

    def __init__(self, options: dict):
        self.options = self.Options(**options)

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        limit, seen = cutoff(ctx), set()

        def fetch(job_id: str, company_hint: str) -> Job | None:
            job_id = job_id.lower()                 # Dover's ids are UUIDs: an upper-case link is the same job
            key = f"dover_{job_id}"
            if job_id in seen or ctx.is_known(key):
                return None
            seen.add(job_id)
            try:
                raw: Any = ctx.http.get_json(POSTING_URL.format(job_id=quote(job_id, safe="")))
            except Exception as e:
                if is_gone(e):
                    ctx.mark_gone(key)
                else:
                    ctx.log(f"dover [{company_hint}/{job_id}]: failed: {e}")
                return None
            if not isinstance(raw, dict) or not raw.get("title") or raw.get("active") is False or raw.get("is_private"):
                ctx.mark_gone(key)
                return None
            if not is_fresh(raw.get("created"), limit):
                ctx.mark_stale(key)
                return None
            company = (raw.get("client_name") or company_hint or "").strip()
            where = "; ".join(loc["name"] for loc in raw.get("locations") or []
                              if isinstance(loc, dict) and loc.get("name"))
            return Job(id=key, title=str(raw["title"]).strip(), company=company, location=where or "Remote",
                       url=apply_url(company, job_id), posted_at=str(raw.get("created") or ""),
                       description=html_to_text(raw.get("user_provided_description") or ""),
                       source="dover", ats="dover", description_is_snippet=False)

        def listed(item: dict, company: str) -> Job | None:
            job_id, date = str(item["id"]).lower(), item.get("date_posted")
            if job_id not in seen and not ctx.is_known(f"dover_{job_id}") and date and not is_fresh(date, limit):
                seen.add(job_id)
                ctx.mark_stale(f"dover_{job_id}")   # dated by the listing: no request for its details
                return None
            return fetch(job_id, company)

        if self.options.job_board:
            try:
                items = pages(ctx, FEED_URL, older_than=limit, label="job board")
            except Exception as e:
                ctx.log(f"dover [job board]: {e}")
                items = []
            yield from each_listing(ctx, "dover", items,
                                    lambda it: listed(it, str((it.get("client") or {}).get("name") or "")))
        for slug in self.options.companies:
            try:
                client = ctx.http.get_json(SLUG_URL.format(slug=quote(slug, safe="")))
                items = pages(ctx, COMPANY_JOBS_URL.format(client_id=client["id"]))
            except Exception as e:
                ctx.log(f"dover [{slug}]: {e}")
                continue
            name = str(client.get("name") or slug)
            yield from each_listing(ctx, "dover", [i for i in items if i.get("is_published", True) and not i.get("is_sample")],
                                    lambda it, name=name: listed(it, name))
        if self.options.discover:
            yield from each_listing(ctx, "dover", discovered(ctx, "dover"), lambda p: fetch(p.job_id, p.slug))
