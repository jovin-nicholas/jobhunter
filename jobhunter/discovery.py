"""Links to job postings on ATS sites (Greenhouse, Lever, Ashby, Workday, Dover, ADP, Gem) for boards with
`discover: true`: links in public GitHub job-list READMEs (free) and, when configured, Google Custom Search results
(100 free queries a day). The links are fetched once per run, however many boards ask for them.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable, Mapping

from jobhunter.boards.ats_urls import ats_of
from jobhunter.settings import DiscoverySettings

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
                 log: Callable[[str], None] = print, env: Mapping[str, str] | None = None):
        self.settings, self.queries, self.http, self.log = settings, queries, http, log
        self.env = os.environ if env is None else env
        self._urls: set[str] | None = None
        self._lock = threading.Lock()

    def urls(self) -> set[str]:
        # Boards search in parallel threads; the lock makes the first caller fetch and the others wait for it.
        with self._lock:
            if self._urls is None:
                self._urls = self._github() | self._google()
                self.log(f"discovery: {len(self._urls)} ATS job links")
            return set(self._urls)

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
