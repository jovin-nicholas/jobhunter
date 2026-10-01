"""Ashby: every job of the companies listed (the name in jobs.ashbyhq.com/<name>) and, with `discover: true`, the
Ashby postings discovery finds. One request per company returns all its jobs with their descriptions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from jobhunter.boards.ats_urls import discovered
from jobhunter.boards.common import cutoff, each_listing, is_fresh
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def to_job(raw: dict, slug: str) -> Job:
    return Job(id=f"ashby_{raw['id']}", title=raw.get("title", ""), company=slug, location=raw.get("location") or "",
               url=raw.get("jobUrl", ""), posted_at=raw.get("publishedAt", ""),
               description=html_to_text(raw.get("descriptionHtml") or "") or raw.get("descriptionPlain") or "",
               source="ashby", ats="ashby", description_is_snippet=False)


@board("ashby")
class AshbyBoard:
    @dataclass
    class Options:
        companies: list[str] = field(default_factory=list)
        discover: bool | None = None      # default: on when no companies are listed

    default_timeout_s = 1800              # discovery can mean hundreds of postings

    def __init__(self, options: dict):
        self.options = self.Options(**options)
        self.discover = self.options.discover if self.options.discover is not None else not self.options.companies

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        limit, seen, boards = cutoff(ctx), set(), {}

        def jobs_of(slug: str) -> dict[str, dict] | None:
            """The company's jobs by id; None when its board could not be fetched."""
            if slug not in boards:
                try:
                    data = ctx.http.get_json(BOARD_URL.format(slug=slug))
                    boards[slug] = {j["id"]: j for j in data.get("jobs", []) if isinstance(j, dict) and j.get("id")}
                except Exception as e:
                    ctx.log(f"ashby [{slug}]: {e}")
                    boards[slug] = None
            return boards[slug]

        def listed(raw: dict, slug: str) -> Job | None:
            if raw["id"] in seen or not is_fresh(raw.get("publishedAt"), limit):
                return None
            job = to_job(raw, slug)
            seen.add(raw["id"])
            return job

        for slug in self.options.companies:
            yield from each_listing(ctx, "ashby", (jobs_of(slug) or {}).values(), lambda raw, slug=slug: listed(raw, slug))
        if not self.discover:
            return

        def discovered_posting(p) -> Job | None:
            job_id = f"ashby_{p.job_id}"
            if p.job_id in seen or ctx.is_known(job_id):
                return None
            jobs = jobs_of(p.slug)
            if jobs is None:
                return None
            seen.add(p.job_id)
            raw = jobs.get(p.job_id)
            if raw is None:
                ctx.mark_gone(job_id)          # the company's board loaded and no longer lists it
                return None
            if not is_fresh(raw.get("publishedAt"), limit):
                ctx.mark_stale(job_id)
                return None
            return to_job(raw, p.slug)

        yield from each_listing(ctx, "ashby", discovered(ctx, "ashby"), discovered_posting)
