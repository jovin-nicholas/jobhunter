"""ADP Workforce Now recruitment: companies' postings (by ADP company id, the cid= in a posting link) and discovered
links, read from ADP's public career-center JSON. The recruitment page itself is a JavaScript app with no job in it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from jobhunter.boards.ats_urls import discovered
from jobhunter.boards.common import cutoff, each_listing, is_fresh, is_gone
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

API = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
PAGE, MAX_PAGES = 20, 25


def posting_url(cid: str, job_id: str) -> str:
    return f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&jobId={job_id}"


def external_id(item: dict) -> str:
    """The public job number (the jobId= in a posting link); the internal itemID when a requisition has none."""
    for f in (item.get("customFieldGroup") or {}).get("stringFields") or []:
        if isinstance(f, dict) and (f.get("nameCode") or {}).get("codeValue") == "ExternalJobID" and f.get("stringValue"):
            return str(f["stringValue"])
    return str(item.get("itemID") or "")


def locations(item: dict) -> str:
    names = [((loc.get("nameCode") or {}).get("shortName") or "").strip()
             for loc in item.get("requisitionLocations") or [] if isinstance(loc, dict)]
    return "; ".join(n for n in names if n)


@board("adp")
class AdpBoard:
    @dataclass
    class Options:
        # ADP company ids (the cid= in a posting link), as a list, or as {cid: company name} so alerts show the name.
        companies: list[str] | dict[str, str] = field(default_factory=list)
        discover: bool = True

    default_timeout_s = 1800

    def __init__(self, options: dict):
        self.options = self.Options(**options)

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        limit, seen = cutoff(ctx), set()
        # ADP's API takes company ids (UUIDs) in lower case only; an empty name shows the id instead.
        given = (self.options.companies.items() if isinstance(self.options.companies, dict)
                 else ((c, c) for c in self.options.companies))
        names = {str(cid).lower(): (str(name) if name else str(cid).lower()) for cid, name in given}

        def fetch(cid: str, job_id: str) -> Job | None:
            key = f"adp_{cid}_{job_id}"
            if key in seen or ctx.is_known(key):
                return None
            seen.add(key)
            try:
                raw = ctx.http.get_json(f"{API}/{job_id}", params={"cid": cid, "lang": "en_US"})
            except Exception as e:
                if is_gone(e):
                    ctx.mark_gone(key)
                else:
                    ctx.log(f"adp [{cid}/{job_id}]: failed: {e}")
                return None
            if not isinstance(raw, dict) or not raw.get("requisitionTitle"):
                # An empty record may be a closed posting or a hiccup: only a 404/410 is gone for good.
                ctx.log(f"adp [{cid}/{job_id}]: unexpected answer without a title; tried again next run")
                return None
            if not is_fresh(raw.get("postDate"), limit):
                ctx.mark_stale(key)
                return None
            return Job(id=key, title=str(raw["requisitionTitle"]).strip(), company=names.get(cid, cid),
                       location=locations(raw), url=posting_url(cid, job_id),
                       posted_at=str(raw.get("postDate") or ""),
                       description=html_to_text(raw.get("requisitionDescription") or ""),
                       source="adp", ats="adp", description_is_snippet=False)

        def listed(item: dict, cid: str) -> Job | None:
            job_id = external_id(item)
            key = f"adp_{cid}_{job_id}"
            if not job_id or key in seen or ctx.is_known(key):
                return None
            if not is_fresh(item.get("postDate"), limit):
                seen.add(key)
                ctx.mark_stale(key)                  # dated by the list: no request for its details
                return None
            return fetch(cid, job_id)

        for cid in names:
            items, skip = [], 0
            try:
                for _ in range(MAX_PAGES):
                    data = ctx.http.get_json(API, params={"cid": cid, "lang": "en_US", "$top": PAGE, "$skip": skip})
                    page = [i for i in data.get("jobRequisitions") or [] if isinstance(i, dict)]
                    items += page
                    total = (data.get("meta") or {}).get("totalNumber")
                    skip += len(page)
                    # Without a total, a full page may have more after it; a short one is the last.
                    if not page or (skip >= int(total) if total is not None else len(page) < PAGE):
                        break
            except Exception as e:
                ctx.log(f"adp [{cid}]: {e}")
                if not items:
                    continue
            yield from each_listing(ctx, "adp", items, lambda it, cid=cid: listed(it, cid))
        if self.options.discover:
            yield from each_listing(ctx, "adp", discovered(ctx, "adp"), lambda p: fetch(p.slug, p.job_id))
