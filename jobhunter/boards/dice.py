"""Dice, through its public MCP server (https://mcp.dice.com/mcp).

search_jobs only returns a summary capped at 500 characters, so `enrich` fetches the full description with
get_job_details, which needs the job's `guid` (not its `id`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterator

from jobhunter.boards.common import cutoff, each_listing, is_fresh
from jobhunter.errors import BoardError
from jobhunter.models import Job, SearchContext
from jobhunter.registry import board
from jobhunter.text_clean import html_to_text

URL = "https://mcp.dice.com/mcp"
# For jobs saved without a guid: detailsPageUrl is https://www.dice.com/job-detail/<guid>?...
_GUID_IN_URL = re.compile(r"/job-detail/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def _parse_mcp(text: str) -> Any:
    """The JSON-RPC reply in an MCP answer: server-sent events ("data: {...}", possibly with notifications before the
    reply) or plain JSON."""
    events = [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:") and line[5:].strip()]
    if not events:
        return json.loads(text) if text.strip() else None
    replies = [e for e in events if isinstance(e, dict) and ("result" in e or "error" in e)]
    return replies[-1] if replies else events[-1]


def _call_tool(http: Any, tool: str, arguments: dict) -> Any:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def post(payload: dict) -> Any:
        resp = http.request("POST", URL, json=payload, headers=headers, retry=True)   # MCP calls are safe to repeat
        if "mcp-session-id" in resp.headers:
            headers["mcp-session-id"] = resp.headers["mcp-session-id"]
        resp.raise_for_status()
        return _parse_mcp(resp.text)

    post({"jsonrpc": "2.0", "method": "initialize", "id": 1,
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "jobhunter", "version": "0.1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    reply = post({"jsonrpc": "2.0", "method": "tools/call", "id": 2,
                  "params": {"name": tool, "arguments": arguments}}) or {}
    if "error" in reply:
        raise BoardError(f"Dice {tool}: {reply['error']}")
    result = reply.get("result", {})
    texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
    if result.get("isError"):
        raise BoardError(f"Dice {tool}: {' '.join(texts)[:200]}")
    return json.loads(texts[0]) if texts else None


def _normalize(raw: dict) -> Job:
    return Job(
        id=f"dice_{raw.get('id') or raw.get('guid') or raw.get('detailsPageUrl', '')}",
        title=raw.get("title", ""),
        company=raw.get("companyName", ""),
        # Fully remote Dice jobs have no jobLocation; Dice lists US jobs, so they are remote within the US.
        location=(raw.get("jobLocation") or {}).get("displayName")
        or ("Remote - US" if raw.get("isRemote") or "Remote" in (raw.get("workplaceTypes") or []) else ""),
        url=raw.get("detailsPageUrl", ""),
        posted_at=raw.get("postedDate", ""),
        description=raw.get("summary", ""),
        source="dice",
        ats="dice",
        description_is_snippet=True,
        extra={"guid": raw.get("guid")},
    )


def _posted_date(max_age_hours: int) -> str:
    return "ONE" if max_age_hours <= 24 else "THREE" if max_age_hours <= 72 else "SEVEN"


@board("dice")
class DiceBoard:
    @dataclass
    class Options:
        jobs_per_page: int = 100          # Dice allows 1-100 (its default is 5)
        max_pages: int = 3

    def __init__(self, options: dict):
        self.options = self.Options(**options)

    def search(self, ctx: SearchContext) -> Iterator[Job]:
        seen: set[str] = set()
        limit = cutoff(ctx)

        def listed(raw: dict) -> Job | None:
            if not is_fresh(raw.get("postedDate"), limit):
                return None
            return _normalize(raw)

        for query in ctx.queries:
            for location in ctx.locations or [""]:
                for page in range(1, self.options.max_pages + 1):
                    # Newest first, only postings Dice itself dates inside max_age_hours (its choice: 1, 3 or 7 days).
                    arguments = {"keyword": query, "location": location, "jobs_per_page": self.options.jobs_per_page,
                                 "page_number": page, "posted_date": _posted_date(ctx.max_age_hours),
                                 "sort": "datePosted"}
                    try:
                        data = _call_tool(ctx.http, "search_jobs", arguments) or {}
                    except Exception as e:
                        ctx.log(f"dice [{query} / {location}]: {e}")
                        break
                    for job in each_listing(ctx, "dice", data.get("data", []), listed):
                        if job.id not in seen:
                            seen.add(job.id)
                            yield job
                    paging = data.get("meta") or data.get("metadata") or {}
                    try:
                        total_pages = int(paging.get("totalPages") or 1)
                    except (AttributeError, TypeError, ValueError):
                        total_pages = 1     # unreadable paging: keep what this page gave, read no further
                    if page >= total_pages:
                        break

    def enrich(self, job: Job, ctx: SearchContext) -> Job:
        guid = job.extra.get("guid")
        if not guid:
            match = _GUID_IN_URL.search(job.url or "")
            guid = match.group(1) if match else None
        description = ""
        if guid:
            try:
                details = _call_tool(ctx.http, "get_job_details", {"job_id": guid}) or {}
                details = details.get("data", details) if isinstance(details, dict) else {}
                description = html_to_text(details.get("description", "")) if isinstance(details, dict) else ""
            except Exception as e:
                ctx.log(f"dice: full description unavailable for {job.id}: {e}")
        if description:
            job.description, job.description_is_snippet = description, False
        else:
            job.description_is_snippet = True
        return job
