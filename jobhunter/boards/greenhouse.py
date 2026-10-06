"""Greenhouse: every job of the companies listed (the token in boards.greenhouse.io/<token>) and, with
`discover: true`, the Greenhouse postings discovery finds."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Iterator

from jobhunter.boards.ats_urls import discovered
from jobhunter.boards.common import cutoff, each_listing, is_fresh, is_gone
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

LIST_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
DETAIL_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"


def published_at(raw: dict) -> str | None:
    """When the posting was first published, for freshness and the stored date: an edit moves `updated_at`, so an old
    posting edited today would otherwise pass max_age_hours. `updated_at` only when nothing else is given."""
    return next((raw[k] for k in ("first_published", "created_at", "posted_at", "updated_at") if raw.get(k)), None)


_TAG = re.compile(r"<[a-zA-Z/!]")


def _content_html(content: str) -> str:
    """`content` is HTML escaped once (&lt;p&gt;). Unescaping twice would turn escaped text such as List&amp;lt;String&amp;gt;
    into a tag and drop it, so a second pass is made only when the first one produced no tags at all."""
    once = html.unescape(content or "")
    return html.unescape(once) if not _TAG.search(once) and "&lt;" in once else once


def to_job(raw: dict, slug: str) -> Job:
    content = _content_html(raw.get("content") or "")
    return Job(
        id=f"greenhouse_{raw['id']}",
        title=raw.get("title", ""),
        company=raw.get("company_name") or slug,
        location=(raw.get("location") or {}).get("name", ""),
        url=raw.get("absolute_url", ""),
        posted_at=published_at(raw) or "",
        description=html_to_text(content),
        source="greenhouse",
        ats="greenhouse",
        description_is_snippet=False,
    )


@board("greenhouse")
class GreenhouseBoard:
    @dataclass
    class Options:
        companies: list[str] = field(default_factory=list)
        discover: bool | None = None      # default: on when no companies are listed

    default_timeout_s = 1800              # discovery can mean hundreds of postings

    def __init__(self, options: dict):
        self.options = self.Options(**options)
        self.discover = self.options.discover if self.options.discover is not None else not self.options.companies

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        limit, seen = cutoff(ctx), set()

        def listed(raw: dict, slug: str) -> Job | None:
            key = str(raw["id"])
            if key in seen or not is_fresh(published_at(raw), limit):
                return None
            job = to_job(raw, slug)
            seen.add(key)
            return job

        for slug in self.options.companies:
            try:
                data = ctx.http.get_json(LIST_URL.format(slug=slug), params={"content": "true"})
            except Exception as e:
                ctx.log(f"greenhouse [{slug}]: {e}")
                continue
            yield from each_listing(ctx, "greenhouse", data.get("jobs", []), lambda raw, slug=slug: listed(raw, slug))
        if not self.discover:
            return

        def discovered_posting(p) -> Job | None:
            job_id = f"greenhouse_{p.job_id}"
            if p.job_id in seen or ctx.is_known(job_id):
                return None
            seen.add(p.job_id)
            try:
                raw = ctx.http.get_json(DETAIL_URL.format(slug=p.slug, job_id=p.job_id))
            except Exception as e:
                ctx.log(f"greenhouse [{p.slug}/{p.job_id}]: {'gone' if is_gone(e) else 'failed'}: {e}")
                if is_gone(e):
                    ctx.mark_gone(job_id)
                return None
            if not is_fresh(published_at(raw), limit):
                ctx.mark_stale(job_id)
                return None
            return to_job(raw, p.slug)

        yield from each_listing(ctx, "greenhouse", discovered(ctx, "greenhouse"), discovered_posting)
