"""Hyde Park Venture Partners' portfolio job board (Getro). Listings have no description, so the pipeline fetches each
new job's posting page."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from jobhunter.boards.ats_urls import ats_of
from jobhunter.boards.common import cutoff, each_listing, is_fresh, parse_time
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board

SEARCH_URL = "https://api.getro.com/api/v2/collections/{collection}/search/jobs"


@board("hydepark")
class HydeParkBoard:
    @dataclass
    class Options:
        collection_id: int = 112
        job_functions: list[str] = field(default_factory=lambda: ["Software Engineering"])
        hits_per_page: int = 100

    def __init__(self, options: dict):
        self.options = self.Options(**options)

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        o = self.options
        payload = {"hits_per_page": o.hits_per_page, "page": 0, "filters": {"job_functions": o.job_functions},
                   "query": ""}
        resp = ctx.http.request("POST", SEARCH_URL.format(collection=o.collection_id), json=payload,
                                headers={"Accept": "application/json"}, retry=True)
        resp.raise_for_status()   # one endpoint: if it fails, the whole board has failed
        limit = cutoff(ctx)

        def listed(item: dict) -> Job | None:
            url, created = (item.get("url") or "").strip(), item.get("created_at")
            if not item.get("id") or not url or not created or not is_fresh(created, limit):
                return None
            posted = parse_time(created)
            return Job(id=f"hydepark_{item['id']}", title=(item.get("title") or "").strip(),
                       company=((item.get("organization") or {}).get("name") or "Unknown").strip(),
                       location=", ".join(item.get("locations") or []), url=url,
                       posted_at=posted.isoformat() if posted else "", description="", source="hydepark",
                       ats=ats_of(url, "hydepark"))

        yield from each_listing(ctx, "hydepark", (resp.json().get("results") or {}).get("jobs", []), listed)
