"""A Laya questions checkpoint's fit is an expected 1-10 score that in practice spans only about 2-7, so it reads
low. It is shown min-max scaled to 0-10 between fixed bounds per checkpoint (the 1st and 99th percentile of its real
fits, from `tracker-calibrate`), with the raw score kept beside it. Cut-offs and jobs.db stay on the raw score."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import quantiles
from typing import Any

MIN_FITS = 30                 # fewer real fits than this give unstable bounds
MIN_SPREAD = 0.5              # bounds closer than this would blow small differences up
SCALE_FILE = "fit_scale.json"
_EXPECTED = re.compile(r"^fit \d+(?:\.\d+)?/10$")


def fit_of(score: Any, label: str | None, model: str | None) -> float | None:
    """The job's fit score, or None when its score is a decision placeholder: a Laya checkpoint trained on alert,
    save or skip stores 8, 6 or 3 with no fit label, which is not a fit and must not land in the fit bands."""
    if not isinstance(score, (int, float)):
        return None
    if (model or "").startswith("laya") and not label and float(score).is_integer():
        return None
    return score


def is_expected_label(label: str | None) -> bool:
    """True for an expected-score fit label ("fit 6.42/10"), the only kind that is scaled."""
    return bool(label) and bool(_EXPECTED.match(label))


@dataclass(frozen=True)
class FitScale:
    low: float
    high: float

    def scale(self, raw: float) -> float:
        return min(10.0, max(0.0, (raw - self.low) / (self.high - self.low) * 10))


def calibrate(db_path: Path) -> dict[str, dict]:
    """Bounds for every Laya model with at least MIN_FITS real fits and a spread of at least MIN_SPREAD."""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT source_model, match_score, score_label FROM jobs WHERE source_model LIKE 'laya%'")
        by_model: dict[str, list[float]] = {}
        for model, score, label in rows:
            fit = fit_of(score, label, model)
            if fit is not None:
                by_model.setdefault(model, []).append(float(fit))
    now = datetime.now(timezone.utc).isoformat()
    out = {}
    for model, fits in sorted(by_model.items()):
        if len(fits) < MIN_FITS:
            continue
        cuts = quantiles(fits, n=100, method="inclusive")
        low, high = cuts[0], cuts[98]
        if high - low >= MIN_SPREAD:
            out[model] = {"low": round(low, 4), "high": round(high, 4), "n": len(fits), "computed_at": now}
    return out


def save_scales(data_dir: Path, scales: dict[str, dict]) -> Path:
    path = Path(data_dir) / SCALE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scales, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_scales(data_dir: Path) -> dict[str, FitScale]:
    """The saved bounds, or {} when the file is missing, unreadable or malformed (then nothing is scaled)."""
    try:
        raw = json.loads((Path(data_dir) / SCALE_FILE).read_text(encoding="utf-8"))
        scales = {model: FitScale(float(b["low"]), float(b["high"])) for model, b in raw.items()}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}
    if any(s.high - s.low < MIN_SPREAD for s in scales.values()):
        return {}
    return scales
