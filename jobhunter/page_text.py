"""Full job descriptions from a posting's web page, for boards that list jobs without one (LinkedIn, Hyde Park) or
whose detail call failed (Dice).

Only public http(s) addresses are fetched: a board can hand back any URL, and one pointing at this machine or the
local network must never be requested.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, UnicodeDammit

from jobhunter.text_clean import html_to_text
from jobhunter.text_match import has_term, word_text

MAX_CHARS = 8000
MIN_USEFUL_CHARS = 200
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")
# Job boards and ATS sites: redirects are followed and larger pages allowed.
TRUSTED_DOMAINS = ("linkedin.com", "amazon.jobs", "greenhouse.io", "ashbyhq.com", "lever.co", "myworkdayjobs.com",
                   "dover.com", "dover.io", "gem.com", "adp.com", "dev.to", "dice.com", "getro.com", "monster.com")
_LIMITS = {True: (15, 2_000_000), False: (8, 400_000)}      # trusted? -> (timeout in s, max bytes)
MAX_REDIRECTS = 5
_REDIRECTS = {301, 302, 303, 307, 308}
# LinkedIn's search-widget text, which some pages return instead of the posting.
_PLACEHOLDERS = ("this button displays the currently selected search type",
                 "when expanded it provides a list of search options",
                 "search inputs to match the current selection")
_HINTS = ("responsibilities", "requirements", "qualifications", "preferred", "you will", "we are looking for",
          "about the role", "must have", "engineer", "role")
_DESCRIPTION_BLOCK = re.compile(r"job-description|jobdescription|description|job-details|posting-description|"
                                r"jobs-description-content|description__text", re.I)

Resolver = Callable[[str], list[str]]


class PageUnavailable(Exception):
    """The page refused us (429, 5xx) or did not answer in time. The job is retried next run instead of being scored
    without its description."""


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # is_global also refuses shared address space (100.64.0.0/10, e.g. Tailscale) and the other special ranges.
    return addr.is_global and not addr.is_multicast


def classify(url: str, resolve: Resolver = _resolve) -> tuple[bool, bool]:
    """(allowed, trusted) for a URL: only http(s) to public addresses is allowed."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().strip(".")
    if parsed.scheme not in ("http", "https") or not host or host == "localhost" \
            or host.endswith((".local", ".localhost")):
        return False, False
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = resolve(host)
        except OSError:
            addresses = []          # DNS failed: the request fails on its own and reaches nothing private
    if any(not _is_public(ip) for ip in addresses):
        return False, False
    return True, any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS)


def _is_placeholder(text: str) -> bool:
    low = " ".join(text.lower().split())
    return any(p in low for p in _PLACEHOLDERS)


def _job_likeness(text: str) -> int:
    words = word_text(text)
    return len(text) + 150 * sum(has_term(words, h) for h in _HINTS)


def best_description(markup: str) -> str:
    """The most job-like block of a posting page as plain text; the page's whole text when no block stands out."""
    soup = BeautifulSoup(markup or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    candidates = []
    for tag in soup.find_all(True, class_=_DESCRIPTION_BLOCK) + soup.find_all(True, id=_DESCRIPTION_BLOCK):
        text = html_to_text(str(tag))
        if text and not _is_placeholder(text):
            candidates.append((_job_likeness(text), text))
    if candidates:
        return max(candidates, key=lambda c: c[0])[1][:MAX_CHARS]
    text = html_to_text(str(soup.body or soup))
    return "" if _is_placeholder(text) else text[:MAX_CHARS]


def needs_page_fetch(description: str | None, is_snippet: bool | None) -> bool:
    return is_snippet is True or len((description or "").strip()) < MIN_USEFUL_CHARS


_CHARSET = re.compile(r"charset=\s*[\"']?([\w.-]+)", re.I)


def _decode(raw: bytes, resp: Any) -> str:
    """The page's text: the charset its Content-Type declares, else detected from the page (meta tag, then UTF-8).
    requests alone assumes ISO-8859-1 for text/html without a charset, which garbles UTF-8 pages."""
    declared = _CHARSET.search((getattr(resp, "headers", None) or {}).get("Content-Type", "") or "")
    if declared:
        try:
            return raw.decode(declared.group(1), errors="replace")
        except LookupError:
            pass
    return UnicodeDammit(raw, is_html=True, user_encodings=["utf-8"]).unicode_markup or ""


def fetch_description(http: Any, url: str, log: Callable[[str], None] = print, resolve: Resolver = _resolve) -> str:
    """Plain-text description from the posting page at `url`; "" when the page is missing, not allowed or has no
    text. Raises PageUnavailable when the site refused or did not answer (after the client's retries).

    Redirects are followed by hand, on job-board sites only and at most MAX_REDIRECTS times, and every address on the
    way is checked, so a redirect cannot lead to this machine or the local network."""
    allowed, trusted = classify(url, resolve)
    if not allowed:
        log(f"page fetch: not fetching {url!r} (not a public http(s) address)")
        return ""
    timeout, max_bytes = _LIMITS[trusted]
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            resp = http.request("GET", current, headers={"User-Agent": BROWSER_UA}, timeout=timeout,
                                allow_redirects=False, stream=True)
            location = (getattr(resp, "headers", None) or {}).get("Location")
            if resp.status_code in _REDIRECTS and location:
                resp.close()
                if not trusted:
                    log(f"page fetch: {url} redirects elsewhere; not followed for a site outside the job boards")
                    return ""
                current = urljoin(current, location)
                if not classify(current, resolve)[0]:
                    log(f"page fetch: {url} redirects to {current!r}, not a public address; not followed")
                    return ""
                continue
            try:
                # A job board answering 403 is blocking us for now, not saying the job is gone.
                if resp.status_code == 429 or resp.status_code >= 500 or (trusted and resp.status_code == 403):
                    raise PageUnavailable(f"{current}: HTTP {resp.status_code}")
                resp.raise_for_status()
                chunks, total = [], 0
                for chunk in resp.iter_content(chunk_size=16384):
                    total += len(chunk)
                    if total > max_bytes:
                        log(f"page fetch: {current} is larger than {max_bytes} bytes; skipped")
                        return ""
                    chunks.append(chunk)
                markup = _decode(b"".join(chunks), resp)
            finally:
                resp.close()
            return best_description(markup)
        log(f"page fetch: {url} gave more than {MAX_REDIRECTS} redirects; stopped")
        return ""
    except PageUnavailable:
        raise
    except (requests.ConnectionError, requests.Timeout) as e:
        raise PageUnavailable(f"{current}: {type(e).__name__}") from e
    except Exception as e:
        log(f"page fetch: {current}: {e}")
        return ""
