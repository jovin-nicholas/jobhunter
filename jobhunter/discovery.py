"""Links to job postings on ATS sites (Greenhouse, Lever, Ashby, Workday, Dover, ADP, Gem) for boards with
`discover: true`: links in public GitHub job-list READMEs (free) and, when configured, Google Custom Search results
(100 free queries a day). The links are fetched once per run, however many boards ask for them.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from jobhunter.boards.ats_urls import ats_of
from jobhunter.boards.common import is_fresh, parse_time
from jobhunter.settings import DiscoverySettings
from jobhunter.vc_sources import VcListing, consider_listings, getro_listings

_URL = re.compile(r"https?://[^\s)\"'<>\[\]]+")
ATS_BOARDS = {"greenhouse", "lever", "ashby", "workday", "dover", "adp", "gem"}


def ats_links(text: str) -> set[str]:
    """Links to postings on the ATS sites jobhunter reads, from <a href> attributes (the job lists are HTML tables),
    markdown and bare links alike, kept by their host rather than by a substring ("clever.com" is not Lever).
    Only &amp; is decoded: a full HTML unescape would turn "&region=" into "®ion="."""
    urls = (url.replace("&amp;", "&").rstrip(".,;:!") for url in _URL.findall(text or ""))
    return {url for url in urls if ats_of(url) in ATS_BOARDS}
GOOGLE_URL = "https://www.googleapis.com/customsearch/v1"


class Discovery:
    def __init__(self, settings: DiscoverySettings, queries: list[str], http: Any,
                 log: Callable[[str], None] = print, env: Mapping[str, str] | None = None,
                 cache_path: Path | None = None, max_age_hours: int = 72):
        self.settings, self.queries, self.http, self.log = settings, queries, http, log
        self.env = os.environ if env is None else env
        # VC board listings within max_age_hours are kept here between runs, so Getro paging stops at what it has
        # seen once a run has read that collection to a real stop; None (a dry run) reads and writes nothing.
        self.cache_path, self.max_age_hours = cache_path, max_age_hours
        self._urls: set[str] | None = None
        self._listings: list[VcListing] | None = None
        self.failures: list[str] = []       # VC sources that could not be read this run, for vc_boards' summary
        self._lock = threading.Lock()

    def urls(self) -> set[str]:
        # Boards search in parallel threads; the lock makes the first caller fetch and the others wait for it.
        with self._lock:
            if self._urls is None:
                lists = self._github() | self._google()
                vc = {l.url for l in self._listings_unlocked() if ats_of(l.url) in ATS_BOARDS}
                self._urls = lists | vc
                self.log(f"discovery: {len(self._urls)} ATS job links ({len(lists)} from job lists, "
                         f"{len(vc)} from VC boards)")
            return set(self._urls)

    def listings(self) -> list[VcListing]:
        """VC board listings within max_age_hours, one per key: the cached ones plus whatever is new this run."""
        with self._lock:
            return list(self._listings_unlocked())

    def _listings_unlocked(self) -> list[VcListing]:
        if self._listings is None:
            now = datetime.now(timezone.utc)
            cutoff, stamp = now - timedelta(hours=self.max_age_hours), now.isoformat()
            listings, first_seen, complete = self._read_cache()

            def fresh(l: VcListing) -> bool:
                # An undated listing ages from when a run first saw it, so the cache cannot keep it for ever.
                if parse_time(l.posted_at) is None:
                    return is_fresh(first_seen.get(l.key, stamp), cutoff)
                return is_fresh(l.posted_at, cutoff)
            cached = {l.key: l for l in listings if fresh(l)}
            new: list[VcListing] = []
            finished: set[int] = set()
            if self.settings.getro:
                new += getro_listings(self.http, self.settings.getro, cutoff, cached.__contains__, self.log,
                                      complete=complete, finished=finished, failures=self.failures)
            if self.settings.consider:
                new += consider_listings(self.http, self.settings.consider, cutoff, self.log, self.failures)
            for listing in new:
                cached.setdefault(listing.key, listing)
            self._listings = list(cached.values())
            self._write_cache(self._listings, {key: first_seen.get(key, stamp) for key in cached}, finished)
        return self._listings

    def _read_cache(self) -> tuple[list[VcListing], dict[str, str], set[int]]:
        """(listings, when each was first seen, the Getro collections the last run read to a real stop). The first
        format, a bare list of listings, says nothing about collections, so none of them counts as read to the end."""
        if self.cache_path is None:
            return [], {}, set()
        name = self.cache_path.name
        try:
            if not self.cache_path.exists():
                return [], {}, set()
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self.log(f"discovery: {name} could not be read ({type(e).__name__}); starting the VC board cache again")
            return [], {}, set()
        first_seen: dict[str, str] = {}
        complete: set[int] = set()
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict) and isinstance(data.get("listings"), list):
            rows = data["listings"]
            if isinstance(data.get("first_seen"), dict):
                first_seen = {k: v for k, v in data["first_seen"].items() if isinstance(v, str)}
            if isinstance(data.get("complete"), list):
                complete = {c for c in data["complete"] if isinstance(c, int) and not isinstance(c, bool)}
        else:
            self.log(f"discovery: {name} does not hold a list of listings; starting the VC board cache again")
            return [], {}, set()
        good = [VcListing(*row) for row in rows
                if isinstance(row, list) and len(row) == len(VcListing._fields) and all(isinstance(v, str) for v in row)]
        if len(good) < len(rows):
            self.log(f"discovery: {len(rows) - len(good)} unreadable listing(s) in {name} dropped")
        return good, first_seen, complete

    def _write_cache(self, listings: list[VcListing], first_seen: dict[str, str], complete: set[int]) -> None:
        if self.cache_path is None or not (self.settings.getro or self.settings.consider):
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"listings": [list(l) for l in listings], "first_seen": first_seen,
                                       "complete": sorted(complete)}), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError as e:
            self.log(f"discovery: could not save {self.cache_path.name} ({type(e).__name__})")

    def _github(self) -> set[str]:
        found: set[str] = set()
        for url in self.settings.github_readmes:
            try:
                resp = self.http.request("GET", url)
                resp.raise_for_status()
            except Exception as e:
                self.log(f"discovery: {url}: {e}")
                continue
            links = ats_links(resp.text)
            self.log(f"discovery: {len(links)} links in {url}")
            found |= links
        return found

    def _google(self) -> set[str]:
        cfg = self.settings.google
        if not cfg:
            return set()
        key, cx = self.env.get(cfg["api_key_env"], ""), self.env.get(cfg["cx_env"], "")
        found: set[str] = set()
        for query in self.queries:
            try:
                resp = self.http.request("GET", GOOGLE_URL, params={"key": key, "cx": cx, "q": query,
                                                                    "dateRestrict": "d2", "num": 10})
            except Exception as e:
                # The exception text includes the request URL, and with it the API key: report only its type.
                self.log(f"discovery: Google search {query!r} failed ({type(e).__name__})")
                continue
            if resp.status_code == 429:
                self.log("discovery: Google search quota used up (100 free queries a day); stopping for this run")
                break
            if resp.status_code != 200:
                self.log(f"discovery: Google search {query!r}: HTTP {resp.status_code}")
                continue
            try:
                items = resp.json().get("items", [])
                found |= {item["link"] for item in items if isinstance(item, dict) and isinstance(item.get("link"), str)
                          and ats_of(item["link"]) in ATS_BOARDS}
            except (ValueError, AttributeError, TypeError):
                self.log(f"discovery: Google search {query!r} answered with something that is not JSON; skipped")
        return found
