"""Job-posting URLs of the ATS sites, parsed into (company slug, job id, url)."""
from __future__ import annotations

import re
from typing import Any, Iterable, NamedTuple
from urllib.parse import parse_qs, urlparse


class Posting(NamedTuple):
    slug: str
    job_id: str
    url: str


_PATTERNS = {
    # job-boards.eu.greenhouse.io boards are served by the same boards API.
    "greenhouse": re.compile(r"^https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([^/?#]+)/jobs/(\d+)"),
    "lever": re.compile(r"^https?://jobs(?:\.eu)?\.lever\.co/([^/?#]+)/([^/?#]+)"),
    "ashby": re.compile(r"^https?://jobs\.ashbyhq\.com/([^/?#]+)/([^/?#]+)"),
    # Dover's links: app.dover.com/apply/<company>/<id> (app.dover.io redirects there), and the older
    # jobs.dover.io/<company>/<id>; "apply" is not the company.
    "dover": re.compile(r"^https?://(?:jobs\.dover\.io|app\.dover\.(?:io|com))/(?:apply/)?([^/?#]+)/([^/?#]+)"),
    "gem": re.compile(r"^https?://jobs\.gem\.com/([^/?#]+)/([^/?#]+)"),
}
_WORKDAY = re.compile(r"^https?://([^./]+)(?:\.[^./]+)?\.myworkdayjobs\.com(/[^?#]*)")
# wd1.myworkdaysite.com/recruiting/<tenant>/<site>/job/...: the tenant is in the path, not the host.
_WORKDAY_SITE = re.compile(r"^https?://[^./]+\.myworkdaysite\.com/recruiting/([^/?#]+)(/[^?#]*)")
_GREENHOUSE_EMBED = re.compile(r"^https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/embed/job_app\?")
_ATS_HOSTS = {"greenhouse.io": "greenhouse", "lever.co": "lever", "ashbyhq.com": "ashby", "adp.com": "adp",
              "myworkdayjobs.com": "workday", "myworkdaysite.com": "workday", "dover.io": "dover", "dover.com": "dover", "gem.com": "gem", "teamtailor.com": "teamtailor",
              "bamboohr.com": "bamboohr", "workable.com": "workable"}


def _workday(url: str) -> Posting | None:
    m = _WORKDAY.match(url) or _WORKDAY_SITE.match(url)
    if not m:
        return None
    segments = [s for s in m.group(2).split("/") if s]
    while segments and segments[-1].lower() in ("apply", "applymanually", "autofillwithresume", "login"):
        segments.pop()
    # Posting pages have a /job/ (or /details/) segment; without one the URL is a careers page, not a job.
    if not {"job", "details"} & {s.lower() for s in segments[:-1]}:
        return None
    return Posting(m.group(1), segments[-1], url)


def _adp(url: str) -> Posting | None:
    parsed = urlparse(url)
    if (parsed.hostname or "") not in ("myjobs.adp.com", "workforcenow.adp.com"):
        return None
    query = parse_qs(parsed.query)
    cid, job_id = (query.get("cid") or [""])[0], (query.get("jobId") or [""])[0]
    return Posting(cid, job_id, url) if cid and job_id else None


def _greenhouse_embed(url: str) -> Posting | None:
    """boards.greenhouse.io/embed/job_app?for=<company>&token=<id>; without `for` the API cannot find the job."""
    query = parse_qs(urlparse(url).query)
    slug, job_id = (query.get("for") or [""])[0], (query.get("token") or [""])[0]
    return Posting(slug, job_id, url) if slug and job_id.isdigit() else None


def parse(ats: str, url: str) -> Posting | None:
    # The URL's real host must be the ATS: "http://localhost#@a.myworkdayjobs.com/..." only looks like Workday.
    if ats_of(url) != ats:
        return None
    if ats == "greenhouse" and _GREENHOUSE_EMBED.match(url or ""):
        return _greenhouse_embed(url)
    if ats == "workday":
        return _workday(url)
    if ats == "adp":
        return _adp(url)
    m = _PATTERNS[ats].match(url or "")
    return Posting(m.group(1), m.group(2), url) if m else None


def postings(ats: str, urls: Iterable[str]) -> list[Posting]:
    """Parsed postings for one ATS, each once (the same job is often linked under two URL forms)."""
    found: dict[tuple[str, str], Posting] = {}
    for url in sorted(urls):
        p = parse(ats, url)
        if p:
            p = p._replace(slug=p.slug.lower())
            found.setdefault((p.slug, p.job_id), p)
    return list(found.values())


def discovered(ctx: Any, ats: str) -> list[Posting]:
    """Postings for one ATS among the links discovery found this run."""
    if ctx.discovery is None:
        ctx.log(f"{ats}: discover is on but this run has no discovery")
        return []
    return postings(ats, ctx.discovery.urls())


def ats_of(url: str, default: str = "") -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return next((name for domain, name in _ATS_HOSTS.items() if host == domain or host.endswith("." + domain)),
                default)
