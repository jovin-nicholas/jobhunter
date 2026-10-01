"""Lever: every posting of the companies listed (the name in jobs.lever.co/<name>) and, with `discover: true`, the
Lever postings discovery finds."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import urlparse

from jobhunter.boards.ats_urls import discovered
from jobhunter.boards.common import cutoff, each_listing, is_fresh, is_gone
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

LIST_URL = "https://api.lever.co/v0/postings/{slug}"
DETAIL_URL = "https://api.lever.co/v0/postings/{slug}/{job_id}"
EU_DETAIL_URL = "https://api.eu.lever.co/v0/postings/{slug}/{job_id}"     # jobs.eu.lever.co postings live only here


def _description(raw: dict) -> str:
    # descriptionPlain is only the intro; requirements and responsibilities are in `lists`.
    parts = [raw.get("descriptionPlain") or html_to_text(raw.get("description") or "")]
    for section in raw.get("lists") or []:
        if isinstance(section, dict):
            parts.append(f"{section.get('text', '')}\n{html_to_text(section.get('content') or '')}")
    parts.append(raw.get("additionalPlain") or "")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def to_job(raw: dict, slug: str) -> Job:
    return Job(id=f"lever_{raw.get('id', '')}", title=raw.get("text", ""), company=slug,
               location=(raw.get("categories") or {}).get("location") or "", url=raw.get("hostedUrl", ""),
               posted_at=str(raw.get("createdAt", "")), description=_description(raw), source="lever", ats="lever",
               description_is_snippet=False)


@board("lever")
class LeverBoard:
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
            if not raw.get("id") or raw["id"] in seen or not is_fresh(raw.get("createdAt"), limit):
                return None
            job = to_job(raw, slug)
            seen.add(raw["id"])
            return job

        for slug in self.options.companies:
            try:
                listed_raw = ctx.http.get_json(LIST_URL.format(slug=slug), params={"mode": "json"})
            except Exception as e:
                ctx.log(f"lever [{slug}]: {e}")
                continue
            yield from each_listing(ctx, "lever", listed_raw if isinstance(listed_raw, list) else [],
                                    lambda raw, slug=slug: listed(raw, slug))
        if not self.discover:
            return

        def discovered_posting(p) -> Job | None:
            job_id = f"lever_{p.job_id}"
            if p.job_id in seen or ctx.is_known(job_id):
                return None
            seen.add(p.job_id)
            try:
                eu = (urlparse(p.url).hostname or "").endswith(".eu.lever.co")
                detail = EU_DETAIL_URL if eu else DETAIL_URL
                raw = ctx.http.get_json(detail.format(slug=p.slug, job_id=p.job_id))
            except Exception as e:
                ctx.log(f"lever [{p.slug}/{p.job_id}]: {'gone' if is_gone(e) else 'failed'}: {e}")
                if is_gone(e):
                    ctx.mark_gone(job_id)
                return None
            if isinstance(raw, dict) and "postings" in raw:
                raw = (raw["postings"] or [None])[0]
            if not (isinstance(raw, dict) and raw.get("id")):
                ctx.mark_gone(job_id)
                return None
            if not is_fresh(raw.get("createdAt"), limit):
                ctx.mark_stale(job_id)
                return None
            return to_job(raw, p.slug)

        yield from each_listing(ctx, "lever", discovered(ctx, "lever"), discovered_posting)
