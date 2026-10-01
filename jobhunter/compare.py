"""Side-by-side check before switching from job-notifier: how both apps decided the same jobs."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from jobhunter.importer import read_jobs

DECIDED = ("notified", "logged", "skipped", "filtered")


@dataclass
class Comparison:
    pairs: Counter = field(default_factory=Counter)            # (old status, new status) -> jobs
    disagreements: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.pairs.values())

    @property
    def agreement(self) -> float:
        same = sum(n for (old, new), n in self.pairs.items() if old == new)
        return same / self.total if self.total else 0.0


def compare(old_db: Path, new_db: Path, since: str | None = None) -> Comparison:
    old = {r["id"]: r for r in read_jobs(old_db) if r.get("status") in DECIDED}
    result = Comparison()
    for r in read_jobs(new_db):
        if r.get("status") not in DECIDED or r["id"] not in old or (since and (r.get("run_at") or "") < since):
            continue
        before, after = old[r["id"]]["status"], r["status"]
        result.pairs[(before, after)] += 1
        if before != after:
            result.disagreements.append({"id": r["id"], "title": r.get("title"), "company": r.get("company"),
                                         "old": before, "new": after, "old_score": old[r["id"]].get("match_score"),
                                         "new_score": r.get("match_score"), "filter_reason": r.get("filter_reason")})
    return result


def report(c: Comparison, limit: int = 20) -> str:
    if not c.total:
        return "no jobs decided by both apps yet (run jobhunter alongside job-notifier first)"
    lines = [f"{c.total} job(s) decided by both; same decision for {c.agreement:.0%}", "", "job-notifier -> jobhunter"]
    for (before, after), n in sorted(c.pairs.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {before:9} -> {after:9} {n}")
    if c.disagreements:
        lines += ["", f"first {min(limit, len(c.disagreements))} differences:"]
        for d in c.disagreements[:limit]:
            reason = f", {d['filter_reason']}" if d["filter_reason"] else ""
            lines.append(f"  {d['old']} -> {d['new']}: {d['title']} at {d['company']} "
                         f"(scores {d['old_score']} / {d['new_score']}{reason})")
    return "\n".join(lines)
