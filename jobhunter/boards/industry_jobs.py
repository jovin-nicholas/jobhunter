"""Job posts on dev.to, from its RSS feeds for the #jobs and #hiring tags (free, no key). Most posts under those tags
are articles, so only posts that read like an opening are kept."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator

from jobhunter.boards.common import cutoff, each_listing
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text
from jobhunter.text_match import has_term, word_text

FEED_URL = "https://dev.to/feed/tag/{tag}"
OPENING_SIGNALS = ["we are hiring", "we're hiring", "job opening", "open position", "apply now", "join our team",
                   "now hiring", "hiring now", "position available"]
ROLE_WORDS = ["engineer", "engineers", "engineering", "developer", "developers", "backend", "frontend", "full stack",
              "fullstack", "software", "sde"]
ARTICLE_WORDS = ["background jobs", "job queue", "cron job", "job offer", "offer negotiation", "how to", "tutorial",
                 "guide", "top", "best", "nobody is", "why", "api", "firebase", "supabase", "bullmq", "trigger.dev"]
JOB_CATEGORIES = {"jobs", "job", "hiring", "career"}


def is_likely_opening(title: str, description: str, categories: list[str]) -> bool:
    """True for a post that reads like an opening. Article words count only in the title ("How to …", "Top 10 …"):
    a hiring post's text often says "our API", "top engineers" or "why join us"."""
    if any(has_term(word_text(title), t) for t in ARTICLE_WORDS):
        return False
    words = word_text(f"{title} {description}")
    role = any(has_term(words, t) for t in ROLE_WORDS)
    opening = title.strip().lower().startswith("[hiring]") or any(has_term(words, t) for t in OPENING_SIGNALS)
    if opening and role:
        return True
    tagged = any(c.strip().lower() in JOB_CATEGORIES for c in categories)
    return tagged and role and (has_term(words, "apply") or has_term(words, "position"))


def _published(text: str | None) -> datetime | None:
    try:
        dt = parsedate_to_datetime(text) if text else None
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


@board("industry_jobs")
class IndustryJobsBoard:
    @dataclass
    class Options:
        tags: list[str] = field(default_factory=lambda: ["jobs", "hiring"])

    def __init__(self, options: dict):
        self.options = self.Options(**options)

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        limit, seen = cutoff(ctx), set()
        for tag in self.options.tags:
            try:
                resp = ctx.http.request("GET", FEED_URL.format(tag=tag))
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
            except Exception as e:
                ctx.log(f"industry_jobs [{tag}]: {e}")
                continue
            yield from each_listing(ctx, "industry_jobs", root.iter("item"),
                                    lambda item: self._post(item, limit, seen))

    @staticmethod
    def _post(item, limit, seen: set[str]) -> Job | None:
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        description = html_to_text(item.findtext("description") or "")
        published = _published(item.findtext("pubDate"))
        if not url or url in seen or (published and published < limit):
            return None
        if not is_likely_opening(title, description, [c.text or "" for c in item.findall("category")]):
            return None
        seen.add(url)
        return Job(id=f"devto_{url.split('/')[-1]}", title=title, company="Dev.to Community", location="Remote",
                   url=url, posted_at=published.isoformat() if published else "", description=description,
                   source="industry_jobs", ats="devto")
