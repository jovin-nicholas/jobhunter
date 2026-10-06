"""VC portfolio job boards as job sources: Getro collections (Redpoint, Accel, Hyde Park, ...) and Consider boards
(jobs.a16z.com). Fetching and parsing only; discovery decides where each listing goes."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable, Container, NamedTuple

from jobhunter.boards.common import is_fresh, parse_time

GETRO_URL = "https://api.getro.com/api/v2/collections/{id}/search/jobs"
_FLIGHT = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_JOBS_KEY = re.compile(r'"initialData"\s*:\s*\{\s*"jobs"\s*:\s*')


class VcListing(NamedTuple):
    key: str          # getro_<id> / consider_<id>: also the job id vc_boards uses
    source: str       # the board's name, e.g. Redpoint or a16z
    title: str
    company: str
    location: str
    url: str
    posted_at: str


def _web_url(value: Any) -> str:
    url = str(value or "").strip()
    return url if url.startswith(("https://", "http://")) else ""


def getro_listings(http: Any, settings: dict, cutoff: datetime, is_cached: Callable[[str], bool],
                   log: Callable[[str], None], complete: Container[int] = (),
                   finished: set[int] | None = None) -> list[VcListing]:
    """New listings of every collection, newest first; a collection stops at a page with nothing new.

    A cached job counts as seen for that stop only in the collections in `complete`, those a previous run read to a
    real stop: a run that failed or ran out of pages left older jobs unread below its cached ones, so the next run
    pages past what is cached until it reaches them. The collections this run reads to a real stop (an empty page, a
    page reaching the cut-off, or one with nothing new) are added to `finished`."""
    filters = {"job_functions": settings["job_functions"]}
    if settings.get("locations"):
        filters["searchable_locations"] = settings["locations"]
    if settings.get("seniority"):
        filters["seniority"] = settings["seniority"]
    found: list[VcListing] = []
    for cid, name in settings["collections"].items():
        seen: set[str] = set()                   # a job repeated on a later page is not new
        trusted = cid in complete
        stopped = False
        try:
            for page in range(settings.get("max_pages", 10)):
                resp = http.request("POST", GETRO_URL.format(id=cid), retry=True,
                                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                                    json={"hitsPerPage": 20, "page": page, "query": "", "filters": filters})
                resp.raise_for_status()
                jobs = [j for j in ((resp.json() or {}).get("results") or {}).get("jobs") or [] if isinstance(j, dict)]
                if not jobs:
                    stopped = True
                    break
                anything_new = False
                # Newest first (featured jobs aside): once a page reaches the cut-off, every later page is older.
                reached_old = any(not j.get("featured") and not is_fresh(j.get("created_at"), cutoff) for j in jobs)
                for j in jobs:
                    key, url = f"getro_{j.get('id')}", _web_url(j.get("url"))
                    fresh = is_fresh(j.get("created_at"), cutoff)
                    known = key in seen or is_cached(key)
                    if fresh and key not in seen and not (trusted and is_cached(key)) and not j.get("featured"):
                        anything_new = True
                    seen.add(key)
                    if not (fresh and url and j.get("id")) or known:
                        continue
                    posted = parse_time(j.get("created_at"))
                    found.append(VcListing(key, name, str(j.get("title") or "").strip(),
                                           str((j.get("organization") or {}).get("name") or "").strip(),
                                           "; ".join(str(x) for x in j.get("locations") or []), url,
                                           posted.isoformat() if posted else ""))
                if reached_old or not anything_new:
                    stopped = True
                    break
        except Exception as e:
            log(f"getro [{name}]: {e}")
            continue
        if stopped and finished is not None:
            finished.add(cid)
    return found


def _flight_text(page: str) -> str:
    parts = []
    for literal in _FLIGHT.findall(page or ""):
        try:
            parts.append(json.loads(literal))
        except ValueError:
            continue                         # one unreadable chunk; the rest may still hold the jobs
    return "".join(parts)


def consider_listings(http: Any, settings: dict, cutoff: datetime, log: Callable[[str], None]) -> list[VcListing]:
    found: list[VcListing] = []
    for host, name in settings["boards"].items():
        for role in settings["roles"]:
            try:
                resp = http.request("GET", f"https://{host}/jobs", params={"role": role})
                resp.raise_for_status()
                text = _flight_text(resp.text)
                m = _JOBS_KEY.search(text)
                if not m:
                    log(f"consider [{host}]: no job list found on the page")
                    continue
                jobs, _ = json.JSONDecoder().raw_decode(text, m.end())
            except Exception as e:
                log(f"consider [{host}]: {e}")
                continue
            for j in jobs if isinstance(jobs, list) else []:
                url = _web_url(j.get("apply_url")) if isinstance(j, dict) else ""
                if not url or not j.get("id") or not is_fresh(j.get("posted_at"), cutoff):
                    continue
                found.append(VcListing(f"consider_{j['id']}", name, str(j.get("title") or "").strip(),
                                       str(j.get("company_name") or "").strip(), str(j.get("location") or ""), url,
                                       str(j.get("posted_at") or "")))
    return found
