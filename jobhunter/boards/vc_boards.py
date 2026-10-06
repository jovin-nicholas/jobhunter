"""Jobs from VC portfolio boards (discovery.getro / discovery.consider) whose link is not read by an ATS board this run.
A link to an ATS board that discovers (enabled, with discover on) goes to that board through discovery; any other
link, an ATS one included, stays here. Listings carry no description, so the pipeline fetches each posting's page."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlsplit

from jobhunter.boards.ats_urls import ats_of
from jobhunter.discovery import ATS_BOARDS
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board


@board("vc_boards")
class VcBoardsBoard:
    @dataclass
    class Options:
        pass                              # nothing to set: discovery.getro / discovery.consider say what to read

    default_timeout_s = 900
    # The listings have no description: a job whose posting page gives none is retried, not judged by its title.
    needs_description = True

    def __init__(self, options: dict):
        self.options = self.Options(**options)

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        listings = ctx.discovery.listings() if ctx.discovery is not None and hasattr(ctx.discovery, "listings") else []
        discovering = ATS_BOARDS if getattr(ctx, "discovering", None) is None else ctx.discovering
        keys, urls, handed, kept = set(), set(), Counter(), 0
        for l in listings:
            # Getro and Consider can list the same posting under two keys; it is one job.
            url = _same_url(l.url)
            if l.key in keys or url in urls:
                continue
            keys.add(l.key)
            urls.add(url)
            ats = ats_of(l.url)
            if ats in discovering:
                handed[ats] += 1                 # that board reads it from discovery, with its full description
                continue
            kept += 1                            # an ATS board that is off or not discovering would lose it
            if ctx.is_known(l.key):
                continue
            yield Job(id=l.key, title=l.title, company=l.company, location=l.location or "Remote", url=l.url,
                      posted_at=l.posted_at, description="", source="vc_boards",
                      ats=ats if ats in ATS_BOARDS else "")
        if listings:
            by_board = ", ".join(f"{name} {n}" for name, n in sorted(handed.items()))
            ctx.log(f"vc_boards: {sum(handed.values())} link(s) handed to ATS boards"
                    f"{f' ({by_board})' if by_board else ''}, {kept} kept here")


def _same_url(url: str) -> str:
    """A link as written by either board: scheme, host case, a trailing slash and the #fragment do not matter."""
    parts = urlsplit((url or "").strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}{'?' + parts.query if parts.query else ''}"
