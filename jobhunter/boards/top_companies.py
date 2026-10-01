"""Jobs at a few large companies with public job APIs: Amazon (amazon.jobs search) and the Greenhouse boards named in
`greenhouse` (SoFi and Stripe by default). Microsoft's old search API is gone, so it is not included."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Iterator

from jobhunter.boards.common import cutoff, each_listing, is_fresh
from jobhunter.boards.greenhouse import LIST_URL as GREENHOUSE_LIST_URL, to_job as greenhouse_job
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

AMAZON_URL = "https://www.amazon.jobs/en/search.json"


def _amazon_location(item: dict) -> str:
    """With the country, so the location filter never has to guess: "Seattle, Washington, USA", "Berlin, BE, DEU"."""
    normalized = (item.get("normalized_location") or "").strip()
    if normalized:
        return normalized
    parts = [(item.get(k) or "").strip() for k in ("city", "state", "country_code")]
    return ", ".join(p for p in parts if p) or (item.get("location") or "").strip()


def _amazon_job(item: dict, limit, seen: set[str]) -> Job | None:
    path = (item.get("job_path") or "").strip()
    digits = re.findall(r"\d+", path)
    job_id = f"top_amazon_{digits[0] if digits else item.get('id') or path}"
    if not path or job_id in seen or not is_fresh(item.get("posted_date"), limit):
        return None
    text = "\n".join(item.get(k) or "" for k in ("description", "basic_qualifications", "preferred_qualifications"))
    job = Job(id=job_id, title=item.get("title", ""), company="Amazon", location=_amazon_location(item),
              url=path if path.startswith("http") else "https://www.amazon.jobs/" + path.lstrip("/"),
              posted_at=item.get("posted_date", ""), description=html_to_text(html.unescape(text)),
              source="top_companies", ats="amazon", description_is_snippet=False)
    seen.add(job_id)
    return job


def _greenhouse_job(raw: dict, slug: str, label: str, limit, seen: set[str]) -> Job | None:
    job_id = f"top_{slug}_{raw.get('id')}"
    if job_id in seen or not is_fresh(raw.get("updated_at"), limit):
        return None
    job = greenhouse_job(raw, slug)
    job.id, job.company, job.source = job_id, label, "top_companies"
    seen.add(job_id)
    return job


@board("top_companies")
class TopCompaniesBoard:
    @dataclass
    class Options:
        amazon: bool = True
        amazon_queries: int = 4       # the first N search queries, to keep the request count down
        greenhouse: dict[str, str] = field(default_factory=lambda: {"sofi": "SoFi", "stripe": "Stripe"})

    def __init__(self, options: dict):
        self.options = self.Options(**options)

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        limit, seen = cutoff(ctx), set()
        if self.options.amazon:
            for query in ctx.queries[: self.options.amazon_queries]:
                # amazon.jobs ignores loc_query; the country filter and newest-first sort are what it honours.
                params = {"base_query": query, "normalized_country_code[]": "USA", "sort": "recent", "offset": 0,
                          "result_limit": 25}
                try:
                    data = ctx.http.get_json(AMAZON_URL, params=params)
                except Exception as e:
                    ctx.log(f"top_companies [amazon / {query}]: {e}")
                    continue
                yield from each_listing(ctx, "top_companies", data.get("jobs", []),
                                        lambda item: _amazon_job(item, limit, seen))
        for slug, label in self.options.greenhouse.items():
            try:
                data = ctx.http.get_json(GREENHOUSE_LIST_URL.format(slug=slug), params={"content": "true"})
            except Exception as e:
                ctx.log(f"top_companies [{slug}]: {e}")
                continue
            yield from each_listing(ctx, "top_companies", data.get("jobs", []),
                                    lambda raw, slug=slug, label=label: _greenhouse_job(raw, slug, label, limit, seen))
