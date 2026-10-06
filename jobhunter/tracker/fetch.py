"""Opt-in (tracker.fetch_titles): the title and company of a pasted job link, from the page's JSON-LD JobPosting or
its <title>. Only the server fetches, only public http(s) addresses (page_text.classify), with no redirects followed;
any failure gives empty fields, so quick add never fails or waits long because of it."""
from __future__ import annotations

import json
from typing import Any

from bs4 import BeautifulSoup

from jobhunter.page_text import BROWSER_UA, _decode, _resolve, classify

MAX_BYTES = 2_000_000
TIMEOUT_S = 10


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def _job_posting(data: Any) -> dict | None:
    items = data if isinstance(data, list) else (data.get("@graph") or [data]) if isinstance(data, dict) else []
    for item in items:
        if isinstance(item, dict):
            kind = item.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return item
    return None


def _from_markup(markup: str) -> tuple[str, str]:
    soup = BeautifulSoup(markup, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            posting = _job_posting(json.loads(script.string or ""))
        except ValueError:
            continue
        if posting:
            org = posting.get("hiringOrganization")
            company = org.get("name") if isinstance(org, dict) else org if isinstance(org, str) else ""
            return _clean(posting.get("title")), _clean(company)
    return _clean(soup.title.string if soup.title else ""), ""


def title_and_company(http: Any, url: str, resolve: Any = _resolve) -> tuple[str, str]:
    try:
        if not classify(url, resolve)[0]:
            return "", ""
        resp = http.request("GET", url, headers={"User-Agent": BROWSER_UA}, timeout=TIMEOUT_S,
                            allow_redirects=False, stream=True, retry=False)
        try:
            if resp.status_code != 200:
                return "", ""
            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=16384):
                total += len(chunk)
                if total > MAX_BYTES:
                    return "", ""
                chunks.append(chunk)
            markup = _decode(b"".join(chunks), resp)
        finally:
            resp.close()
        return _from_markup(markup)
    except Exception:
        return "", ""
