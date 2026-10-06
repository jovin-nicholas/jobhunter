"""Ashby: every job of the companies listed (the name in jobs.ashbyhq.com/<name>) and, with `discover: true`, the
Ashby postings discovery finds. One request per company returns all its jobs with their descriptions.

Some companies turn Ashby's public posting API off (404) while their hosted job board still works. Those are read the
way the hosted page reads them: one request lists the job ids, then one per new job gives its date and description."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from jobhunter.boards.ats_urls import discovered
from jobhunter.boards.common import cutoff, each_listing, is_fresh, is_gone
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
HOSTED_URL = "https://jobs.ashbyhq.com/api/non-user-graphql"
_HOSTED_BOARD = ("query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: jobBoardWithTeams("
                 "organizationHostedJobsPageName: $organizationHostedJobsPageName) { jobPostings { id title } } }")
_HOSTED_POSTING = ("query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) { jobPosting("
                   "organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) "
                   "{ id title locationName publishedDate descriptionHtml } }")


def _graphql(ctx: SearchContext, operation: str, query: str, variables: dict) -> dict:
    resp = ctx.http.request("POST", f"{HOSTED_URL}?op={operation}", retry=True,
                            json={"operationName": operation, "variables": variables, "query": query})
    resp.raise_for_status()
    return (resp.json() or {}).get("data") or {}


def hosted_ids(ctx: SearchContext, slug: str) -> list[str] | None:
    """The job ids on a company's hosted board, or None when it has none."""
    board = _graphql(ctx, "ApiJobBoardWithTeams", _HOSTED_BOARD, {"organizationHostedJobsPageName": slug}).get("jobBoard")
    if not isinstance(board, dict):
        return None
    return [p["id"] for p in board.get("jobPostings") or [] if isinstance(p, dict) and p.get("id")]


def hosted_posting(ctx: SearchContext, slug: str, job_id: str) -> dict | None:
    """One hosted posting in the posting API's shape, so to_job reads both."""
    p = _graphql(ctx, "ApiJobPosting", _HOSTED_POSTING,
                 {"organizationHostedJobsPageName": slug, "jobPostingId": job_id}).get("jobPosting")
    if not isinstance(p, dict) or not p.get("id"):
        return None
    return {"id": p["id"], "title": p.get("title") or "", "location": p.get("locationName") or "",
            "jobUrl": f"https://jobs.ashbyhq.com/{slug}/{p['id']}", "publishedAt": p.get("publishedDate") or "",
            "descriptionHtml": p.get("descriptionHtml") or ""}


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
        limit, seen, boards, hosted = cutoff(ctx), set(), {}, set()

        def jobs_of(slug: str) -> dict[str, dict | None] | None:
            """The company's jobs by id; None when its board could not be fetched. A hosted board's jobs start as
            None and are read one by one (raw_of), only when needed."""
            if slug not in boards:
                try:
                    data = ctx.http.get_json(BOARD_URL.format(slug=slug))
                    boards[slug] = {j["id"]: j for j in data.get("jobs", []) if isinstance(j, dict) and j.get("id")}
                except Exception as e:
                    boards[slug] = None
                    if is_gone(e):
                        try:
                            ids = hosted_ids(ctx, slug)
                        except Exception:
                            ids = None
                        if ids is not None:
                            boards[slug] = dict.fromkeys(ids)
                            hosted.add(slug)
                    if boards[slug] is None:
                        ctx.log(f"ashby [{slug}]: {e}")
            return boards[slug]

        def raw_of(slug: str, job_id: str) -> dict | None:
            jobs = boards[slug]
            if jobs.get(job_id) is None and slug in hosted:
                jobs[job_id] = hosted_posting(ctx, slug, job_id)
            return jobs.get(job_id)

        def hosted_listed(job_id: str, slug: str) -> Job | None:
            if job_id in seen or ctx.is_known(f"ashby_{job_id}"):
                return None                    # known: not worth a request to read it again
            seen.add(job_id)
            raw = raw_of(slug, job_id)
            if raw is None:
                return None
            if not is_fresh(raw.get("publishedAt"), limit):
                ctx.mark_stale(f"ashby_{job_id}")
                return None
            return to_job(raw, slug)

        def listed(raw: dict, slug: str) -> Job | None:
            if raw["id"] in seen or not is_fresh(raw.get("publishedAt"), limit):
                return None
            job = to_job(raw, slug)
            seen.add(raw["id"])
            return job

        for slug in self.options.companies:
            jobs = jobs_of(slug) or {}
            if slug in hosted:
                yield from each_listing(ctx, "ashby", list(jobs), lambda job_id, slug=slug: hosted_listed(job_id, slug))
            else:
                yield from each_listing(ctx, "ashby", jobs.values(), lambda raw, slug=slug: listed(raw, slug))
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
            if p.job_id not in jobs:
                ctx.mark_gone(job_id)          # the company's board loaded and no longer lists it
                return None
            raw = raw_of(p.slug, p.job_id)
            if raw is None:
                return None
            if not is_fresh(raw.get("publishedAt"), limit):
                ctx.mark_stale(job_id)
                return None
            return to_job(raw, p.slug)

        yield from each_listing(ctx, "ashby", discovered(ctx, "ashby"), discovered_posting)
