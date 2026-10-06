"""Is this working? Reply rates for the numbers panel: every figure carries how many applications it rests on, and a
split with fewer than MIN_N is marked `few` so the page greys it out. Pure: application dicts in, numbers out."""
from __future__ import annotations

from datetime import date
from statistics import median
from typing import Any, Callable

from jobhunter.tracker import REPLY_STAGES

MIN_N = 5
FIT_BANDS = ("<5", "5–7", "≥7")


def first_reply(app: dict) -> str | None:
    """The date of the first screen, interview, offer or acceptance, or None when the company never answered."""
    dates = [e["at"] for e in app["events"] if e["kind"] == "stage" and e["value"] in REPLY_STAGES]
    return min(dates) if dates else None


def fit_band(score: Any) -> str | None:
    if score is None:
        return None
    return "<5" if score < 5 else "5–7" if score < 7 else "≥7"


def _week(applied_at: str) -> str:
    year, week, _ = date.fromisoformat(applied_at).isocalendar()
    return f"{year}-W{week:02d}"


def _rate(apps: list[dict]) -> dict:
    n = len(apps)
    replied = sum(first_reply(a) is not None for a in apps)
    return {"n": n, "replied": replied, "rate": replied / n if n else None, "few": n < MIN_N}


def _split(apps: list[dict], key: Callable[[dict], str | None]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for a in apps:
        label = key(a)
        if label:
            groups.setdefault(label, []).append(a)
    return [{"label": label, **_rate(group)} for label, group in sorted(groups.items())]


def reply_numbers(apps: list[dict]) -> dict:
    days = [max(0, (date.fromisoformat(r) - date.fromisoformat(a["applied_at"])).days)
            for a in apps if (r := first_reply(a))]
    return {
        "overall": _rate(apps),
        "by_board": _split(apps, lambda a: a.get("board") or "other"),
        "by_resume": _split(apps, lambda a: a.get("resume_id") or None),
        "by_fit": sorted(_split(apps, lambda a: fit_band(a.get("fit_score"))),
                         key=lambda r: FIT_BANDS.index(r["label"])),
        "by_week": _split(apps, lambda a: _week(a["applied_at"])),
        "median_days_to_reply": median(days) if days else None,
        "replies_counted": len(days),
    }
