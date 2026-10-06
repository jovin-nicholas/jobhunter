"""Gem: companies' job boards on jobs.gem.com and discovered links, read from Gem's public GraphQL API. The job pages
load everything by script, so their HTML holds no job."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from jobhunter.boards.ats_urls import discovered
from jobhunter.boards.common import cutoff, each_listing, is_fresh, parse_time
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

GRAPHQL_URL = "https://jobs.gem.com/api/public/graphql"
_LIST = ("query JobBoardList($boardId: String!) { oatsExternalJobPostings(boardId: $boardId) { jobPostings "
         "{ id extId title } } jobBoardExternal(vanityUrlPath: $boardId) { teamDisplayName } }")
_POSTING = ("query ExternalJobPosting($boardId: String!, $extId: String!) { oatsExternalJobPosting(boardId: $boardId, "
            "extId: $extId) { id title extId descriptionHtml firstPublishedTsSec locations { name } "
            "job { teamDisplayName } } }")


def _query(ctx: SearchContext, operation: str, query: str, variables: dict) -> dict:
    resp = ctx.http.request("POST", GRAPHQL_URL, retry=True, headers={"content-type": "application/json"},
                            json={"operationName": operation, "variables": variables, "query": query})
    resp.raise_for_status()
    body = resp.json() or {}
    if body.get("errors"):
        # GraphQL answers rate limits and server faults with HTTP 200 and an error list: never read that as "closed".
        messages = "; ".join(str(e.get("message") if isinstance(e, dict) else e) for e in body["errors"])
        raise RuntimeError(f"Gem answered with an error: {messages[:200]}")
    return body.get("data") or {}


@board("gem")
class GemBoard:
    @dataclass
    class Options:
        companies: list[str] = field(default_factory=list)   # Gem board names: jobs.gem.com/<name>
        discover: bool = True

    default_timeout_s = 1800

    def __init__(self, options: dict):
        self.options = self.Options(**options)

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        limit, seen = cutoff(ctx), set()

        def fetch(slug: str, ext_id: str, team: str) -> Job | None:
            key = f"gem_{ext_id}"
            if ext_id in seen or ctx.is_known(key):
                return None
            seen.add(ext_id)
            try:
                post = _query(ctx, "ExternalJobPosting", _POSTING, {"boardId": slug, "extId": ext_id}).get(
                    "oatsExternalJobPosting")
            except Exception as e:
                ctx.log(f"gem [{slug}/{ext_id}]: failed: {e}")
                return None
            if not isinstance(post, dict) or not post.get("title"):
                ctx.mark_gone(key)                   # Gem answers a closed posting with null
                return None
            published = parse_time(post.get("firstPublishedTsSec"))
            if not is_fresh(post.get("firstPublishedTsSec"), limit):
                ctx.mark_stale(key)
                return None
            company = ((post.get("job") or {}).get("teamDisplayName") or team or slug).strip()
            where = "; ".join(loc["name"] for loc in post.get("locations") or [] if isinstance(loc, dict) and loc.get("name"))
            return Job(id=key, title=str(post["title"]).strip(), company=company, location=where or "Remote",
                       url=f"https://jobs.gem.com/{slug}/{ext_id}", posted_at=published.isoformat() if published else "",
                       description=html_to_text(post.get("descriptionHtml") or ""), source="gem", ats="gem",
                       description_is_snippet=False)

        for slug in self.options.companies:
            try:
                data = _query(ctx, "JobBoardList", _LIST, {"boardId": slug})
            except Exception as e:
                ctx.log(f"gem [{slug}]: {e}")
                continue
            if not data.get("jobBoardExternal"):
                ctx.log(f"gem [{slug}]: no Gem board named {slug!r} (names are lower case, as in jobs.gem.com/<name>)")
                continue
            team = data["jobBoardExternal"].get("teamDisplayName") or slug
            posts = (data.get("oatsExternalJobPostings") or {}).get("jobPostings") or []
            ext_ids = [str(p["extId"]) for p in posts if isinstance(p, dict) and p.get("extId")]
            yield from each_listing(ctx, "gem", ext_ids, lambda e, slug=slug, team=team: fetch(slug, e, team))
        if self.options.discover:
            yield from each_listing(ctx, "gem", discovered(ctx, "gem"), lambda p: fetch(p.slug, p.job_id, p.slug))
