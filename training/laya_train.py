"""Fine-tune Laya for jobhunter. Two methods share this module:
- questions (train_questions.ipynb, the one to use): stack role, dealbreaker and fit, combined by gates and fitted
  cut-offs;
- alert (train_alert.ipynb, the older checkpoint): one yes/no question, is this job worth an alert?
Both train on pools of your own labelled jobs (read_pool says which fields a row needs) and a plain-text resume.
`jobhunter export-feedback` writes a record of your verdicts, not a pool; turning verdicts into pools is a manual step
today. Every step is a function, so the notebooks only call them and the logic is tested on a laptop."""
from __future__ import annotations

import json
import math
import random
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

import numpy as np

try:
    from jobhunter.posting import condense, split_posting
except ImportError:                      # the Colab upload folder holds posting.py next to this file
    from posting import condense, split_posting

try:
    from laya.common import QTYPES, build_sequence
except ImportError:                      # tests that do not need laya
    QTYPES, build_sequence = {"choice": 0, "score": 1, "noul": 2}, None

LABELS = ("notify", "log", "skip")
ALERT_QUESTION = {
    "type": "noul",
    "instructions": "Is the job in `job` a good fit for the candidate in `candidate`: the right role and level, with "
                    "no dealbreaker, so they should apply this week?",
    "criteria": {
        "false": "a dealbreaker (clearance, citizenship, seniority, excluded role), the wrong role or level, or "
                 "significant skill gaps",
        "true": "right role family and level, no dealbreaker, and most required skills appear in their work",
    },
}


@dataclass
class Settings:
    max_len: int = 2048
    head_max_len: int = 256
    epochs: int = 4
    patience: int = 2
    micro_batch: int = 8
    grad_accum: int = 8                  # effective batch 64; on a GPU with <70 GB (e.g. A100 40GB), the notebook
                                          # instead uses micro_batch=4, grad_accum=16 (still effective batch 64)
    lr_encoder: float = 2.5e-5
    lr_head: float = 1e-4
    weight_decay: float = 0.01
    warmup: float = 0.06
    group_size: int = 4
    sigma_start: float = 0.4
    sigma_end: float = 0.1
    min_recall: float = 0.5
    balance_slack: int = 50
    select_share: float = 0.25
    calibrate_share: float = 0.15
    seed: int = 20261004                 # splits
    train_seed: int = 1                  # sampling, batch order, dropout
    # questions method
    job_max_len: int = 1024              # stack_role and dealbreaker read the job alone; fit uses max_len
    score_sigma: float = 0.5             # Gaussian smoothing of the fit target, in score levels
    fit_gate_fail_share: float = 0.25    # share of gate-failing train jobs the fit question also sees
    question_weights: dict = field(default_factory=lambda: {"stack_role": 1.0, "dealbreaker": 1.0, "fit": 1.0})


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().replace("-", " ").replace("/", " ").split())


def group_key(row: dict) -> tuple[str, str]:
    return _norm(row.get("title")), _norm(row.get("company"))


def read_pool(path: Path, pool: str, questions: bool = False) -> list[dict]:
    """One pool's rows. With `questions`, every row must also carry the labeller's match_score (1-10), stack_role and
    dealbreaker (text or null); `dealbreaker_known` is false for rows whose label source has no dealbreaker field."""
    rows = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        missing = {"job_id", "title", "company", "description", "label"} - set(r)
        if missing or r["label"] not in LABELS:
            raise ValueError(f"{Path(path).name}:{n}: missing {sorted(missing)} or label {r.get('label')!r}")
        if questions:
            score = r.get("match_score")
            if (not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 10
                    or "stack_role" not in r or "dealbreaker" not in r):
                raise ValueError(f"{Path(path).name}:{n}: question training needs match_score 1-10, stack_role and "
                                 f"dealbreaker (got match_score {score!r})")
        rows.append({**r, "location": r.get("location") or "", "pool": pool,
                     "dealbreaker_known": bool(r.get("dealbreaker_known", True))})
    return rows


# ---- Labels for the questions method --------------------------------------------------------------------------------

STACK_ROLES = ("backend", "frontend", "fullstack", "ai", "data", "infra", "other")
GATES = {"stack_roles": ["backend", "fullstack", "ai", "data"], "dealbreaker_at": 0.5}
_COUNTRIES = ("canada", "mexico", "india", "china", "uk", "united kingdom", "germany", "france", "ireland", "poland",
              "brazil", "philippines", "singapore", "japan", "israel", "australia")
# (category, is a dealbreaker target, pattern); the first pattern that matches a part of the text decides that part.
_DEALBREAKER_RULES = (
    ("clearance", True, re.compile(r"clearance|\bsecret\b|polygraph|ts/sci")),
    ("citizenship", True, re.compile(r"citizen|\bus person\b|green card")),
    ("seniority", True, re.compile(r"\b(?:senior|staff|principal|lead|seniority)\b")),
    ("role_family", True, re.compile(r"embedded|firmware|hardware|front-?end|mobile-only|\betl\b|\bbi\b|role family")),
    ("years", False, re.compile(r"\byears?\b|\byrs\b|\d\s*\+")),       # the rules filter required years
    ("location", False, re.compile(r"outside|us-only|location|onsite in|relocation|\b(?:"
                                   + "|".join(_COUNTRIES) + r")\b")),  # the rules filter location
)


def dealbreaker_category(text) -> tuple[str, bool]:
    """The labeller's free-text dealbreaker as (category, target). Each `;`-separated part maps by the first rule that
    matches it (anything unmatched, such as people management or a spoken language, is a real dealbreaker); the text
    is a dealbreaker when any part is, and its category is that part's (else the first part's)."""
    parts = [p.strip() for p in str(text or "").lower().split(";") if p.strip()]
    if not parts:
        return "none", False
    mapped = [next(((name, target) for name, target, rx in _DEALBREAKER_RULES if rx.search(p)), ("other", True))
              for p in parts]
    return next((m for m in mapped if m[1]), mapped[0])


def dealbreaker_target(text) -> bool:
    return dealbreaker_category(text)[1]


def role_target(value) -> str:
    role = str(value or "").strip().lower()
    return role if role in STACK_ROLES else "other"


def label_gates_pass(row: dict) -> bool:
    """The gates applied to the labels: an accepted stack role and no dealbreaker by the mapping."""
    return role_target(row.get("stack_role")) in GATES["stack_roles"] and not dealbreaker_target(row.get("dealbreaker"))


def dealbreaker_counts(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Rows per dealbreaker category, per pool, for the report."""
    out: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        out[r["pool"]][dealbreaker_category(r.get("dealbreaker"))[0]] += 1
    return {pool: dict(sorted(c.items())) for pool, c in sorted(out.items())}


def shingles(row: dict, n: int = 5, min_words: int = 30) -> frozenset[str] | None:
    """Word n-grams of the posting's condensed requirements + responsibilities, or None when that text has fewer than
    `min_words` words (too short to call two postings the same)."""
    parts = split_posting(row.get("description") or "").sections
    text = parts.get("requirements", "") + "\n" + parts.get("responsibilities", "")
    words = re.findall(r"[a-z0-9+#]+", text.lower())
    if len(words) < min_words:
        return None
    return frozenset(" ".join(words[i:i + n]) for i in range(len(words) - n + 1))


def similar_pairs(left: list, right: list | None = None, threshold: float = 0.8,
                  max_df: int = 100) -> list[tuple[int, int]]:
    """Index pairs whose shingle sets have Jaccard similarity >= `threshold`: (i in left, j in right), or i < j within
    `left` when `right` is None. Candidates come from an inverted index of n-grams; an n-gram in more than `max_df`
    indexed texts (boilerplate) proposes no candidates, but every reported similarity is exact."""
    same = right is None
    right = left if same else right
    index: dict[str, list[int]] = defaultdict(list)
    for j, s in enumerate(right):
        for g in s or ():
            index[g].append(j)
    pairs = []
    for i, a in enumerate(left):
        if not a:
            continue
        candidates = set()
        for g in a:
            posting = index.get(g, ())
            if len(posting) <= max_df:
                candidates.update(posting)
        for j in sorted(candidates):
            if same and j <= i:
                continue
            b = right[j]
            if len(a & b) / len(a | b) >= threshold:
                pairs.append((i, j))
    return pairs


def exclude_near_duplicates(rows: list[dict], reference: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Drop every row that is a reference job (benchmark, held-out) or a near-duplicate of one: same id, same
    normalised title + company, or similar requirements + responsibilities text. Returns kept rows and how many each
    rule excluded (a row counts under the first rule that catches it)."""
    ids = {r["job_id"] for r in reference}
    keys = {group_key(r) for r in reference}
    same_id = [r for r in rows if r["job_id"] in ids]
    rest = [r for r in rows if r["job_id"] not in ids]
    candidates = [r for r in rest if group_key(r) not in keys]
    similar = {i for i, _ in similar_pairs([shingles(r) for r in candidates], [shingles(r) for r in reference])}
    kept = [r for i, r in enumerate(candidates) if i not in similar]
    return kept, {"same_id": len(same_id), "same_title_company": len(rest) - len(candidates),
                  "similar_text": len(similar)}


def job_groups(rows: list[dict]) -> list[int]:
    """A group id per row: rows with the same normalised title + company, or similar text, share a group
    (transitively). Ids are numbered by first appearance, so they are stable across runs."""
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    first: dict[tuple[str, str], int] = {}
    for i, r in enumerate(rows):
        union(i, first.setdefault(group_key(r), i))
    for i, j in similar_pairs([shingles(r) for r in rows]):
        union(i, j)
    ids: dict[int, int] = {}
    return [ids.setdefault(find(i), len(ids)) for i in range(len(rows))]


def split_rows(rows: list[dict], settings: Settings, log=print) -> dict[str, list[dict]]:
    """All `old` rows train; `jobhunter` rows go to train, select and calibrate by job group (title + company or
    similar text), keeping the label mix. A group with rows in both pools trains: its `jobhunter` rows never reach
    select or calibrate."""
    from sklearn.model_selection import StratifiedGroupKFold
    groups = job_groups(rows)
    old_groups = {g for g, r in zip(groups, rows) if r["pool"] != "jobhunter"}
    fresh = [(g, r) for g, r in zip(groups, rows) if r["pool"] == "jobhunter"]
    if not fresh:
        raise ValueError("no jobhunter rows: select and calibrate must come from jobhunter's own postings")
    shared = [r for g, r in fresh if g in old_groups]
    free = [(g, r) for g, r in fresh if g not in old_groups]
    if shared:
        log(f"{len(shared)} jobhunter row(s) share a job group with the old pool: assigned to train, never select or "
            "calibrate")
    if not free:
        raise ValueError("every jobhunter row shares a job group with the old pool: nothing left for select/calibrate")
    folds = 20
    sgkf = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=settings.seed)
    fold_of = {}
    for k, (_, idx) in enumerate(sgkf.split([r for _, r in free], [r["label"] for _, r in free],
                                            [g for g, _ in free])):
        for i in idx:
            fold_of[i] = k
    n_select = round(folds * settings.select_share)
    n_calib = round(folds * settings.calibrate_share)
    out = {"train": [r for r in rows if r["pool"] != "jobhunter"] + shared, "select": [], "calibrate": []}
    for i, (_, r) in enumerate(free):
        k = fold_of[i]
        out["select" if k < n_select else "calibrate" if k < n_select + n_calib else "train"].append(r)
    return out


def sample_train(rows: list[dict], settings: Settings) -> list[dict]:
    """Each label keeps at most the smallest label's count plus `balance_slack`."""
    import pandas as pd
    df = pd.DataFrame(rows)
    cap = int(df["label"].value_counts().min()) + settings.balance_slack
    kept = pd.concat([g.sample(n=min(len(g), cap), random_state=settings.train_seed)
                      for _, g in df.groupby("label")])
    kept = kept.sample(frac=1.0, random_state=settings.train_seed)
    return kept.to_dict("records")


def _job(row: dict) -> SimpleNamespace:
    return SimpleNamespace(title=row["title"], company=row["company"], location=row.get("location", ""),
                           description=row["description"])


def alert_state(resume: str, row: dict) -> dict:
    return {"candidate": resume, "job": condense(_job(row))}


def job_state(row: dict) -> dict:
    return {"job": condense(_job(row))}


def _internal(question: dict) -> dict:
    crit = {str(k).lower(): v for k, v in (question.get("criteria") or {}).items()} or None
    return {"t": question["type"], "ins": question["instructions"], "crit": crit}


def build_items(rows: list[dict], resume: str, tok, settings: Settings, question: dict = ALERT_QUESTION) -> list[dict]:
    q = _internal(question)
    items = []
    for r in rows:
        ids, markers = build_sequence(tok, alert_state(resume, r), q, settings.max_len, settings.head_max_len)
        y = int(r["label"] == "notify")
        items.append({"ids": ids, "markers": markers, "qtype": QTYPES["noul"], "target": [1.0 - y, float(y)],
                      "label": y, "gold": r["label"], "job_id": r["job_id"], "cut": len(ids) >= settings.max_len})
    return items


# ---- The questions method: questions, items, sampling ------------------------------------------------------------

QUESTIONS = {
    "stack_role": {
        "type": "choice",
        "instructions": "Which kind of engineering role is the job in `job`?",
        "criteria": {
            "backend": "server-side services, APIs, databases or distributed systems",
            "frontend": "browser or mobile user interfaces only",
            "fullstack": "both user interfaces and server-side work",
            "ai": "machine learning, LLM applications, model training or AI product engineering",
            "data": "data pipelines, warehouses, analytics engineering or data platforms",
            "infra": "cloud infrastructure, DevOps, SRE, platform or security operations",
            "other": "none of the above, or not a software engineering role",
        },
    },
    "dealbreaker": {
        "type": "noul",
        "instructions": "Does the job in `job` require something an early-career software engineer cannot meet: a "
                        "security clearance, a citizenship requirement, a senior, staff or principal level, a role "
                        "family such as embedded, firmware, hardware or pure frontend, or another stated requirement "
                        "such as people management, a specific spoken language or commission-only pay?",
        "criteria": {"false": "no such requirement is stated in the posting",
                     "true": "the posting states one of these requirements"},
    },
    "fit": {
        "type": "score",
        "instructions": "How good a fit is the job in `job` for the candidate in `candidate`, judged by role, level and "
                        "skills shown in their work?",
        "criteria": ["1 - disqualifying mismatch (wrong role, stack or seniority)", "2 - very weak fit",
                     "3 - weak fit, few matching skills", "4 - below-average fit, significant gaps",
                     "5 - possible fit, meaningful gaps", "6 - reasonable fit, some gaps", "7 - good fit, minor gaps",
                     "8 - strong fit, right seniority and core skills", "9 - very strong fit", "10 - exceptional fit"],
    },
}
BUCKETS = {"stack_role": "choice:6-10", "dealbreaker": "noul:2", "fit": "score:6-10"}
OPTION_NAMES = {"stack_role": list(STACK_ROLES), "dealbreaker": ["false", "true"],
                "fit": [str(i) for i in range(1, 11)]}


def question_inputs(settings: Settings) -> dict:
    return {"stack_role": {"state": "job", "max_len": settings.job_max_len},
            "dealbreaker": {"state": "job", "max_len": settings.job_max_len},
            "fit": {"state": "candidate_job", "max_len": settings.max_len}}


def to_internal(question: dict) -> dict:
    """laya's own rendering of a question definition, so training and scoring read identical text."""
    from laya.agent import Agent
    return Agent._to_internal(question)


def smoothed_target(index: int, k: int, sigma: float) -> list[float]:
    w = np.exp(-((np.arange(k) - index) ** 2) / (2 * sigma ** 2))
    return [float(v) for v in w / w.sum()]


def question_target(qid: str, row: dict, settings: Settings) -> tuple[list[float], int]:
    """(target distribution, true option index) for one row; fit's index is laya's 0-based level (score - 1)."""
    if qid == "stack_role":
        i = STACK_ROLES.index(role_target(row.get("stack_role")))
        return [float(j == i) for j in range(len(STACK_ROLES))], i
    if qid == "dealbreaker":
        y = int(dealbreaker_target(row.get("dealbreaker")))
        return [1.0 - y, float(y)], y
    if qid == "fit":
        i = int(row["match_score"]) - 1
        return smoothed_target(i, 10, settings.score_sigma), i
    raise ValueError(f"unknown question {qid!r}")


def build_question_items(rows: list[dict], resume: str, tok, settings: Settings, qid: str) -> list[dict]:
    q = to_internal(QUESTIONS[qid])
    spec = question_inputs(settings)[qid]
    n_options = len(OPTION_NAMES[qid])
    items = []
    for r in rows:
        state = job_state(r) if spec["state"] == "job" else alert_state(resume, r)
        ids, markers = build_sequence(tok, state, q, spec["max_len"], settings.head_max_len)
        if len(markers) != n_options:
            raise ValueError(f"question {qid!r}: {len(markers)} of {n_options} options fit head_max_len "
                             f"{settings.head_max_len}")
        target, label = question_target(qid, r, settings)
        items.append({"ids": ids, "markers": markers, "qtype": QTYPES[q["t"]], "target": target, "label": label,
                      "qid": qid, "gold": r["label"], "job_id": r["job_id"], "cut": len(ids) >= spec["max_len"]})
    return items


def sample_questions(train_rows: list[dict], settings: Settings) -> dict[str, list[dict]]:
    """stack_role: every train job; dealbreaker: every train job whose dealbreaker label is known; fit: every job
    passing the label gates plus a random `fit_gate_fail_share` of the rest, then each label capped at the smallest
    label's count + `balance_slack`."""
    passing = [r for r in train_rows if label_gates_pass(r)]
    failing = [r for r in train_rows if not label_gates_pass(r)]
    extra = random.Random(settings.train_seed).sample(failing, round(len(failing) * settings.fit_gate_fail_share))
    return {"stack_role": list(train_rows),
            "dealbreaker": [r for r in train_rows if r.get("dealbreaker_known", True)],
            "fit": sample_train(passing + extra, settings)}


def class_counts(items_by_q: dict[str, list[dict]]) -> dict[str, dict[str, int]]:
    """Items per true option, per question, for the report."""
    out = {}
    for qid, items in items_by_q.items():
        c = Counter(it["label"] for it in items)
        out[qid] = {name: c[i] for i, name in enumerate(OPTION_NAMES[qid]) if c[i]}
    return out


@dataclass
class Cutoff:
    threshold: float
    precision: float
    recall: float
    alerts: int
    met_rule: bool


def probs_from_logits(logits: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    z = np.asarray(logits, dtype=np.float64) / temperature
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e[:, 1] / e.sum(axis=1)


def _at(probs: np.ndarray, y: np.ndarray, t: float) -> tuple[float, float, int]:
    alert = probs >= t
    k = int((alert & (y == 1)).sum())
    n = int(alert.sum())
    return (k / n if n else 0.0), k / max(1, int((y == 1).sum())), n


def pick_alert_cutoff(probs: np.ndarray, y: np.ndarray, min_recall: float) -> Cutoff:
    """The most alerts worth opening among cut-offs that catch at least `min_recall` of the good jobs; otherwise the
    cut-off that catches the most, flagged."""
    candidates = sorted({float(p) for p in probs}, reverse=True)   # exact values: a rounded-up cut-off would drop its own job
    best = None
    for t in candidates:
        precision, recall, n = _at(probs, y, t)
        if recall >= min_recall and (best is None or precision > best.precision):
            best = Cutoff(float(t), precision, recall, n, True)
    if best is not None:
        return best
    t = float(min(probs[y == 1])) if (y == 1).any() else float(max(probs))
    precision, recall, n = _at(probs, y, t)
    return Cutoff(t, precision, recall, n, False)


def pick_save_cutoff(probs: np.ndarray, gold: list[str], alert_at: float) -> float:
    """Below the alert cut-off: the probability that best separates `log` from `skip` (highest F1 for `log`)."""
    below = [(p, g) for p, g in zip(probs, gold) if p < alert_at and g in ("log", "skip")]
    best_t, best_f1 = alert_at / 2, -1.0
    for t in sorted({float(p) for p, _ in below}):
        tp = sum(p >= t and g == "log" for p, g in below)
        fp = sum(p >= t and g == "skip" for p, g in below)
        fn = sum(p < t and g == "log" for p, g in below)
        f1 = 2 * tp / max(1, 2 * tp + fp + fn)
        if f1 > best_f1:
            best_t, best_f1 = t, f1
    return best_t


def fit_temperature(logits: np.ndarray, y: np.ndarray) -> float:
    """One temperature by L-BFGS on log-temperature (as in Laya's official notebook), clamped like laya applies it."""
    import torch
    import torch.nn.functional as F
    try:
        from laya.agent import clamp_temperature
    except ImportError:
        def clamp_temperature(t):
            return min(5.0, max(0.5, t))
    z = torch.tensor(np.asarray(logits), dtype=torch.float64)
    target = torch.tensor(np.asarray(y), dtype=torch.long)
    log_t = torch.zeros(1, dtype=torch.float64, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=100)

    def closure():
        opt.zero_grad()
        loss = F.cross_entropy(z / log_t.exp(), target)
        loss.backward()
        return loss
    opt.step(closure)
    return float(clamp_temperature(log_t.exp().detach().item()))


def cutoff_table(probs: np.ndarray, y: np.ndarray, around: float) -> list[tuple[float, float, float, int]]:
    rows = []
    for t in sorted({round(around + d, 2) for d in (-0.15, -0.1, -0.05, 0.0, 0.05, 0.1, 0.15)}):
        if 0 < t < 1:
            precision, recall, n = _at(probs, y, t)
            rows.append((t, precision, recall, n))
    return rows


# ---- The questions method: answers, summaries, gates and cut-offs ------------------------------------------------

SUMMARIES = ("expected", "p_good")
SUMMARY_STEP = {"expected": 0.25, "p_good": 0.05}      # spacing of the nearby cut-off table


def softmax_rows(logits, temperature: float = 1.0) -> np.ndarray:
    z = np.asarray(logits, dtype=np.float64) / temperature
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def answers_from_logits(logits_by_q: dict[str, list], temps: dict[str, float] | None = None) -> dict:
    """Each select/calibrate job's answers: stack role (argmax), P(dealbreaker) and the fit distribution over laya's
    levels 0..9, after the bucket temperatures (1.0 when not given)."""
    temps = temps or {}
    role_p = softmax_rows(np.stack(logits_by_q["stack_role"]), temps.get(BUCKETS["stack_role"], 1.0))
    deal_p = softmax_rows(np.stack(logits_by_q["dealbreaker"]), temps.get(BUCKETS["dealbreaker"], 1.0))[:, 1]
    fit_p = softmax_rows(np.stack(logits_by_q["fit"]), temps.get(BUCKETS["fit"], 1.0))
    return {"role": [STACK_ROLES[i] for i in role_p.argmax(axis=1)], "role_p": role_p, "deal_p": deal_p,
            "fit_p": fit_p}


def gates_pass(role: str, deal_p: float, gates: dict = GATES) -> bool:
    return role in gates["stack_roles"] and deal_p < gates["dealbreaker_at"]


def gate_mask(roles: list[str], deal_p, gates: dict = GATES) -> np.ndarray:
    return np.array([gates_pass(r, float(p), gates) for r, p in zip(roles, deal_p)], dtype=bool)


def summarise(fit_p, summary: str) -> np.ndarray:
    """The fit summary per job: `expected` = 1 + sum(i * p_i) over laya's 0-based levels (a 1-10 score), or `p_good` =
    P(score 7-10) = p_6 + p_7 + p_8 + p_9."""
    fit_p = np.atleast_2d(np.asarray(fit_p, dtype=np.float64))
    if summary == "expected":
        return 1.0 + fit_p @ np.arange(fit_p.shape[1])
    if summary == "p_good":
        return fit_p[:, 6:].sum(axis=1)
    raise ValueError(f"unknown summary {summary!r}; use one of {SUMMARIES}")


def _gated_at(s: np.ndarray, gated: np.ndarray, y: np.ndarray, t: float) -> tuple[float, float, int]:
    alert = gated & (s >= t)
    k = int((alert & (y == 1)).sum())
    n = int(alert.sum())
    return (k / n if n else 0.0), k / max(1, int((y == 1).sum())), n


def pick_gated_cutoff(s, gated, y, min_recall: float) -> Cutoff:
    """`pick_alert_cutoff` inside the gates: only gated jobs alert, but good jobs are counted over the whole split, so a
    good job a predicted gate removes counts as missed."""
    s, gated, y = np.asarray(s, dtype=np.float64), np.asarray(gated, dtype=bool), np.asarray(y)
    if not gated.any():                  # nothing can alert; a finite cut-off keeps the report and export valid
        return Cutoff(float(s.max()) if len(s) else 0.0, 0.0, 0.0, 0, False)
    best = None
    for t in sorted({float(v) for v in s[gated]}, reverse=True):
        precision, recall, n = _gated_at(s, gated, y, t)
        if recall >= min_recall and (best is None or precision > best.precision):
            best = Cutoff(float(t), precision, recall, n, True)
    if best is not None:
        return best
    good = gated & (y == 1)
    t = float(s[good].min()) if good.any() else float(s[gated].max())
    precision, recall, n = _gated_at(s, gated, y, t)
    return Cutoff(t, precision, recall, n, False)


def gated_cutoff_table(s, gated, y, around: float, step: float) -> list[tuple[float, float, float, int]]:
    """(cut-off, worth opening, caught, alerts) at three steps either side of `around`."""
    s, gated, y = np.asarray(s, dtype=np.float64), np.asarray(gated, dtype=bool), np.asarray(y)
    rows = []
    for d in range(-3, 4):
        t = round(around + d * step, 4)
        precision, recall, n = _gated_at(s, gated, y, t)
        rows.append((t, precision, recall, n))
    return rows


def average_precision(s, gated, y) -> float:
    """Average precision for good jobs among gated jobs (no cut-off involved); 0.0 without a gated good job."""
    from sklearn.metrics import average_precision_score
    s, gated, y = np.asarray(s), np.asarray(gated, dtype=bool), np.asarray(y)
    if not (gated & (y == 1)).any():
        return 0.0
    return float(average_precision_score(y[gated], s[gated]))


def choose_summary(fit_p, gated, y) -> tuple[str, dict[str, float]]:
    """The summary with the higher average precision (`expected` on a tie), and both values."""
    aps = {name: average_precision(summarise(fit_p, name), gated, y) for name in SUMMARIES}
    return max(SUMMARIES, key=lambda name: (aps[name], name == "expected")), aps


def decide(gated, s, alert_at: float, save_at: float) -> np.ndarray:
    """notify / log / skip per job: gates first, then the fit summary against the cut-offs."""
    gated, s = np.asarray(gated, dtype=bool), np.asarray(s, dtype=np.float64)
    return np.where(gated & (s >= alert_at), "notify", np.where(gated & (s >= save_at), "log", "skip"))


def decide_from_answers(answers: dict, training: dict) -> tuple[str, float | None]:
    """The exported checkpoint's decision for one job from laya's answers (Agent.system_one's `answers`):
    `stack_role` (choice), `dealbreaker` (noul) and, only when the gates pass, `fit` (score, probabilities keyed
    "0".."9"). Returns (decision, fit summary or None when the fit question was not needed)."""
    gates, cut = training["gates"], training["cutoffs"]
    if not gates_pass(answers["stack_role"]["choice"], float(answers["dealbreaker"]["noul"]), gates):
        return "skip", None
    probs = answers["fit"]["probabilities"]
    value = float(summarise([float(probs[str(i)]) for i in range(len(probs))], cut["summary"])[0])
    return str(decide([True], [value], cut["alert_at"], cut["save_at"])[0]), value


def floor4(x: float) -> float:
    """A cut-off rounded down to 4 decimals, so rounding never lifts it above the job it was fitted on."""
    return math.floor(round(float(x) * 1e4, 6)) / 1e4      # round first: 0.0003 * 1e4 is 2.9999999999999996


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson range for k successes out of n."""
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - r) / d, (c + r) / d


def load_base(base_dir: str, device):
    """Laya's model and tokenizer from a checkpoint folder (the base download, or a fine-tuned one)."""
    import os

    from laya.agent import _fix_tokenizer_config, _load_tokenizer
    from laya.common import build_model
    from safetensors.torch import load_file
    try:
        from transformers.initialization import no_init_weights
    except ImportError:
        from transformers.modeling_utils import no_init_weights
    _fix_tokenizer_config(base_dir)
    cfg = json.loads(Path(base_dir, "rl_agent_config.json").read_text())
    tok = _load_tokenizer(os.path.join(base_dir, "tokenizer"), cfg)
    with no_init_weights():
        model = build_model(cfg, encoder_dir=os.path.join(base_dir, "encoder"), pretrained=False)
    model.load_state_dict(load_file(os.path.join(base_dir, "model.safetensors")), strict=True)
    try:
        model.encoder.config.reference_compile = False
    except Exception:
        pass
    return model.to(device), tok, cfg


def _forward(model, batch, device):
    import torch
    amp = device.type == "cuda"
    with torch.autocast(device.type, dtype=torch.bfloat16, enabled=amp):
        logits, _ = model(batch["input_ids"].to(device), batch["attention_mask"].to(device),
                          batch["marker_pos"].to(device), batch["marker_mask"].to(device), batch["qtype"].to(device))
    return logits.float()


def predict_logits(model, items, settings: Settings, device, pad_id: int = 0) -> np.ndarray:
    import torch
    from laya.common import collate_items
    model.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(items), settings.micro_batch * 2):
            chunk = items[i:i + settings.micro_batch * 2]
            out.append(_forward(model, collate_items([chunk], pad_id), device)[:, :2].cpu().numpy())
    return np.concatenate(out) if out else np.zeros((0, 2))


@dataclass
class TrainResult:
    state: dict
    epoch: int
    cutoff: Cutoff
    history: list


def _loss(logits, batch, settings: Settings, sigma: float, device):
    """Soft cross-entropy plus Laya's calibrated-decision reward term, as in the official fine-tuning notebook."""
    import torch
    from laya import proper_reward
    mask = batch["marker_mask"].to(device)
    target = batch["target"].to(device)
    qtype = batch["qtype"].to(device)
    k = mask.sum(-1, keepdim=True).float()
    eps = torch.randn((settings.group_size,) + logits.shape, device=device) * sigma * mask
    eps = (eps - eps.sum(-1, keepdim=True) / k) * mask
    z = logits.detach().unsqueeze(0) + eps
    q = torch.softmax(z.masked_fill(~mask, -1e4), -1)
    with torch.no_grad():
        r = proper_reward(q, target.unsqueeze(0), qtype, mask, w_sph=0.75, w_rps=1.0)
        adv = (r - r.mean(0, keepdim=True)) / (r.std() + 1e-6)
    logp = -(((z - logits.unsqueeze(0)) ** 2) * mask).sum(-1) / (2 * sigma ** 2)
    loss_rl = -(adv * logp).mean()
    loss_ce = -(target * torch.log_softmax(logits.masked_fill(~mask, -1e4), -1)).sum(-1).mean()
    return loss_rl + loss_ce


def train(model, train_items, select_items, settings: Settings, device, log=print, on_best=None,
         pad_id: int = 0) -> TrainResult:
    import torch
    from laya.common import collate_items
    from transformers import get_linear_schedule_with_warmup
    from transformers.trainer_pt_utils import LengthGroupedSampler
    torch.manual_seed(settings.train_seed)
    random.seed(settings.train_seed)
    enc = [p for n, p in model.named_parameters() if n.startswith("encoder.")]
    head = [p for n, p in model.named_parameters() if not n.startswith("encoder.")]
    opt = torch.optim.AdamW([{"params": enc, "lr": settings.lr_encoder}, {"params": head, "lr": settings.lr_head}],
                            weight_decay=settings.weight_decay)
    steps = math.ceil(len(train_items) / (settings.micro_batch * settings.grad_accum)) * settings.epochs
    sched = get_linear_schedule_with_warmup(opt, int(steps * settings.warmup), max(1, steps))
    sampler = LengthGroupedSampler(settings.micro_batch, lengths=[len(it["ids"]) for it in train_items],
                                   generator=torch.Generator().manual_seed(settings.train_seed))
    y_select = np.array([it["label"] for it in select_items])
    best, history, stale = None, [], 0
    for epoch in range(1, settings.epochs + 1):
        model.train()
        sigma = settings.sigma_start + (settings.sigma_end - settings.sigma_start) * (epoch - 1) / max(1, settings.epochs - 1)
        order = list(sampler)
        total, n = 0.0, 0
        for b, start in enumerate(range(0, len(order), settings.micro_batch)):
            chunk = [train_items[i] for i in order[start:start + settings.micro_batch]]
            batch = collate_items([chunk], pad_id)
            loss = _loss(_forward(model, batch, device), batch, settings, sigma, device) / settings.grad_accum
            loss.backward()
            total, n = total + float(loss.detach()) * settings.grad_accum, n + 1
            if (b + 1) % settings.grad_accum == 0 or start + settings.micro_batch >= len(order):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                sched.step()
                opt.zero_grad(set_to_none=True)
        probs = probs_from_logits(predict_logits(model, select_items, settings, device, pad_id))
        cutoff = pick_alert_cutoff(probs, y_select, settings.min_recall)
        history.append({"epoch": epoch, "loss": round(total / max(1, n), 4), "precision": round(cutoff.precision, 4),
                        "recall": round(cutoff.recall, 4), "met_rule": cutoff.met_rule})
        log(f"epoch {epoch}: loss {history[-1]['loss']} | select: {cutoff.alerts} alerts, "
            f"{cutoff.precision:.0%} worth opening, caught {cutoff.recall:.0%}" + ("" if cutoff.met_rule else " (rule not met)"))
        better = best is None or (cutoff.met_rule, cutoff.precision) > (best.cutoff.met_rule, best.cutoff.precision)
        if better:
            state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            best, stale = TrainResult(state, epoch, cutoff, history), 0
            if on_best:
                on_best(best)
        else:
            stale += 1
            if stale >= settings.patience:
                log(f"stopping: no improvement for {settings.patience} epochs")
                break
    best.history = history
    return best


# ---- The questions method: loss, predictions, training loop -------------------------------------------------------

def advantage(r):
    """Group-relative advantage, normalised per row across that row's noise samples (r: [group, rows]), with the
    row's std floored at the whole batch's std: a row whose rewards vary widely is still scaled to unit, but a
    near-saturated row (a confident, correct dealbreaker or stack_role row, whose reward barely moves across the
    noise samples) gets a near-zero advantage instead of being blown up to unit scale, as in the official recipe."""
    import torch
    return (r - r.mean(0, keepdim=True)) / (torch.maximum(r.std(0, keepdim=True), r.std()) + 1e-6)


def question_loss(logits, batch, settings: Settings, sigma: float, device):
    """Per row: soft cross-entropy plus Laya's calibrated-decision reward term (RPS for score rows), weighted by
    `settings.question_weights`. Returns (mean weighted loss, unweighted per-row losses, detached)."""
    import torch
    from laya import proper_reward
    mask = batch["marker_mask"].to(device)
    target = batch["target"].to(device)
    qtype = batch["qtype"].to(device)
    k = mask.sum(-1, keepdim=True).float()
    eps = torch.randn((settings.group_size,) + logits.shape, device=device) * sigma * mask
    eps = (eps - eps.sum(-1, keepdim=True) / k) * mask
    z = logits.detach().unsqueeze(0) + eps
    q = torch.softmax(z.masked_fill(~mask, -1e4), -1)
    with torch.no_grad():
        adv = advantage(proper_reward(q, target.unsqueeze(0), qtype, mask, w_sph=0.75, w_rps=1.0))
    logp = -(((z - logits.unsqueeze(0)) ** 2) * mask).sum(-1) / (2 * sigma ** 2)
    per_row = -(adv * logp).mean(0) - (target * torch.log_softmax(logits.masked_fill(~mask, -1e4), -1)).sum(-1)
    w = torch.tensor([settings.question_weights.get(m["qid"], 1.0) for m in batch["meta"]], device=device)
    return (w * per_row).sum() / len(per_row), per_row.detach()


def predict_question_logits(model, items, settings: Settings, device, pad_id: int = 0) -> list[np.ndarray]:
    """Each item's full option logits (2, 7 or 10 values), in item order."""
    import torch
    from laya.common import collate_items
    model.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(items), settings.micro_batch * 2):
            chunk = items[i:i + settings.micro_batch * 2]
            logits = _forward(model, collate_items([chunk], pad_id), device).cpu().numpy()
            out += [logits[j, :len(it["markers"])].copy() for j, it in enumerate(chunk)]
    return out


def select_cutoff(logits_by_q: dict[str, list], select_rows: list[dict], settings: Settings) -> Cutoff:
    """Epoch selection, end to end on the select split: argmax role, P(dealbreaker) >= 0.5 and the expected score."""
    a = answers_from_logits(logits_by_q)
    y = np.array([int(r["label"] == "notify") for r in select_rows])
    return pick_gated_cutoff(summarise(a["fit_p"], "expected"), gate_mask(a["role"], a["deal_p"]), y,
                             settings.min_recall)


def train_questions(model, train_items, select_items: dict[str, list[dict]], select_rows: list[dict],
                    settings: Settings, device, log=print, on_best=None, pad_id: int = 0) -> TrainResult:
    """Train all three questions together; keep the epoch with the best (rule met, worth opening) on select.
    `select_items[qid]` holds one item per select row, in `select_rows` order."""
    import torch
    from laya.common import collate_items
    from transformers import get_linear_schedule_with_warmup
    from transformers.trainer_pt_utils import LengthGroupedSampler
    torch.manual_seed(settings.train_seed)
    random.seed(settings.train_seed)
    enc = [p for n, p in model.named_parameters() if n.startswith("encoder.")]
    head = [p for n, p in model.named_parameters() if not n.startswith("encoder.")]
    opt = torch.optim.AdamW([{"params": enc, "lr": settings.lr_encoder}, {"params": head, "lr": settings.lr_head}],
                            weight_decay=settings.weight_decay)
    steps = math.ceil(len(train_items) / (settings.micro_batch * settings.grad_accum)) * settings.epochs
    sched = get_linear_schedule_with_warmup(opt, int(steps * settings.warmup), max(1, steps))
    sampler = LengthGroupedSampler(settings.micro_batch, lengths=[len(it["ids"]) for it in train_items],
                                   generator=torch.Generator().manual_seed(settings.train_seed))
    best, history, stale = None, [], 0
    for epoch in range(1, settings.epochs + 1):
        model.train()
        sigma = settings.sigma_start + (settings.sigma_end - settings.sigma_start) * (epoch - 1) / max(1, settings.epochs - 1)
        order = list(sampler)
        sums, counts = Counter(), Counter()
        for b, start in enumerate(range(0, len(order), settings.micro_batch)):
            chunk = [train_items[i] for i in order[start:start + settings.micro_batch]]
            batch = collate_items([chunk], pad_id)
            loss, per_row = question_loss(_forward(model, batch, device), batch, settings, sigma, device)
            (loss / settings.grad_accum).backward()
            for m, v in zip(batch["meta"], per_row.tolist()):
                sums[m["qid"]] += v
                counts[m["qid"]] += 1
            if (b + 1) % settings.grad_accum == 0 or start + settings.micro_batch >= len(order):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                sched.step()
                opt.zero_grad(set_to_none=True)
        logits = {qid: predict_question_logits(model, its, settings, device, pad_id) for qid, its in select_items.items()}
        cutoff = select_cutoff(logits, select_rows, settings)
        by_q = {qid: round(sums[qid] / counts[qid], 4) for qid in sorted(counts)}
        history.append({"epoch": epoch, "loss": round(sum(sums.values()) / max(1, sum(counts.values())), 4),
                        "loss_by_question": by_q, "alerts": cutoff.alerts, "precision": round(cutoff.precision, 4),
                        "recall": round(cutoff.recall, 4), "met_rule": cutoff.met_rule})
        log(f"epoch {epoch}: loss {history[-1]['loss']} {by_q} | select: {cutoff.alerts} alerts, "
            f"{cutoff.precision:.0%} worth opening, caught {cutoff.recall:.0%}" + ("" if cutoff.met_rule else " (rule not met)"))
        if best is None or (cutoff.met_rule, cutoff.precision) > (best.cutoff.met_rule, best.cutoff.precision):
            state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            best, stale = TrainResult(state, epoch, cutoff, history), 0
            if on_best:
                on_best(best)
        else:
            stale += 1
            if stale >= settings.patience:
                log(f"stopping: no improvement for {settings.patience} epochs")
                break
    best.history = history
    return best


def input_health(rows: list[dict], items: list[dict]) -> dict:
    parts = [split_posting(r["description"]) for r in rows]
    return {"postings": len(rows), "no_headings": sum(p.headings == 0 for p in parts),
            "empty_requirements": sum(p.empty_requirements for p in parts),
            "unknown_headings": sum(p.unknown_headings > 0 for p in parts), "cut": sum(it["cut"] for it in items)}


def _write_weights(state, base_dir, out_dir) -> Path:
    """A fresh checkpoint folder: the weights in the base checkpoint's dtype, plus its tokenizer and encoder config."""
    import torch
    from safetensors.torch import load_file, save_file
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    base_weights = Path(base_dir) / "model.safetensors"
    dtype = None
    if base_weights.is_file():
        for v in load_file(str(base_weights)).values():
            if torch.is_floating_point(v):
                dtype = v.dtype
                break

    def _cast(v: "torch.Tensor") -> "torch.Tensor":
        return v.to(dtype) if dtype is not None and torch.is_floating_point(v) else v

    save_file({k: _cast(v).contiguous().cpu() for k, v in state.items() if isinstance(v, torch.Tensor)},
              str(out / "model.safetensors"))
    shutil.copytree(Path(base_dir) / "tokenizer", out / "tokenizer")
    shutil.copytree(Path(base_dir) / "encoder", out / "encoder")
    return out


def export_checkpoint(state, base_dir, base_cfg, out_dir, settings: Settings, question: dict, alert_at: float,
                      save_at: float, temperature: float, report: dict) -> None:
    out = _write_weights(state, base_dir, out_dir)
    cfg = json.loads(json.dumps(base_cfg))
    cfg["max_len"], cfg["head_max_len"] = settings.max_len, settings.head_max_len
    cfg.setdefault("temperature_by_options", {})["noul:2"] = round(float(temperature), 4)
    cfg["training"] = {"decision_method": {"own": "alert"},
                       # floor4, not round: a cut-off must never be rounded up past the job it was fitted on.
                       "alert": {"question": question, "alert_at": floor4(alert_at), "save_at": floor4(save_at)},
                       "report": report}
    (out / "rl_agent_config.json").write_text(json.dumps(cfg, indent=2))


def report_text(report: dict) -> str:
    s, lines = report["select"], []
    lines.append(f"Select split at alert {report['alert_at']:.0%} / save {report['save_at']:.0%}: {s['alerts']} alerts, "
                 f"{s['precision']:.0%} worth opening, caught {s['recall']:.0%} of good jobs")
    if not s["met_rule"]:
        lines.append("!! No cut-off caught half the good jobs: the model is weak; do not switch to it.")
    if s["precision"] < 2 * report["base_rate"]:
        lines.append(f"!! Alerts are barely better than alerting at random (worth opening {s['precision']:.0%} "
                     f"against a {report['base_rate']:.0%} base rate): do not switch to this model.")
    lines.append(f"Calibration error: {report['ece_before']:.3f} -> {report['ece_after']:.3f} (temperature {report['temperature']:.2f})")
    lines.append("Nearby cut-offs (cut-off, worth opening, caught, alerts):")
    lines += [f"  {t:.2f}  {p:.0%}  {r:.0%}  {n}" for t, p, r, n in report["table"]]
    lines.append(f"Probability range on select: {report['p_min']:.2f} to {report['p_max']:.2f}")
    if report["p_max"] < report["alert_at"] or report["p_max"] - report["p_min"] < 0.2:
        lines.append("!! Probabilities sit in a narrow band or never reach the alert cut-off: check before switching.")
    lines.append("Confusion (rows: label; columns: alert / save / skip): " + json.dumps(report["confusion"]))
    for split, h in report["health"].items():
        lines.append(f"Input health, {split}: {h}")
    return "\n".join(lines)


# ---- The questions method: calibration, report, export, base checkpoint ------------------------------------------

def _plain_json(v):
    """json.dumps fallback for numpy values and dataclasses in the report."""
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    if hasattr(v, "__dataclass_fields__"):
        return asdict(v)
    raise TypeError(f"not JSON serialisable: {type(v).__name__}")


def true_labels(rows: list[dict]) -> dict[str, np.ndarray]:
    """Each question's true option index per row."""
    return {"stack_role": np.array([STACK_ROLES.index(role_target(r.get("stack_role"))) for r in rows]),
            "dealbreaker": np.array([int(dealbreaker_target(r.get("dealbreaker"))) for r in rows]),
            "fit": np.array([int(r["match_score"]) - 1 for r in rows])}


def _exclude_unknown_dealbreaker(logits_by_q: dict[str, list], truth_by_q: dict[str, np.ndarray],
                                 rows: list[dict]) -> tuple[dict[str, list], dict[str, np.ndarray]]:
    """Deviation requested by the controller: the select/calibrate logits hold one dealbreaker entry per row, even on
    rows where `dealbreaker_known` is False (the notebook builds dealbreaker items for every row so gating still
    works on them). Those rows' dealbreaker target is meaningless, so calibrating or scoring the dealbreaker bucket
    must leave them out; `stack_role` and `fit` are returned unchanged."""
    known = [bool(r.get("dealbreaker_known", True)) for r in rows]
    if all(known):
        return logits_by_q, truth_by_q
    logits_out, truth_out = dict(logits_by_q), dict(truth_by_q)
    logits_out["dealbreaker"] = [v for v, k in zip(logits_by_q["dealbreaker"], known) if k]
    truth_out["dealbreaker"] = np.asarray(truth_by_q["dealbreaker"])[known]
    return logits_out, truth_out


def fit_bucket_temperatures(logits_by_q: dict[str, list], labels_by_q: dict[str, np.ndarray]) -> dict[str, float]:
    """One temperature per laya bucket (choice:6-10, noul:2, score:6-10), fitted on the calibrate split and clamped
    to [0.5, 5] as laya applies it."""
    return {BUCKETS[qid]: fit_temperature(np.stack(logits_by_q[qid]), labels_by_q[qid]) for qid in QUESTIONS}


def calibration_errors(logits_by_q: dict[str, list], labels_by_q: dict[str, np.ndarray],
                       temps: dict[str, float] | None = None) -> dict[str, float]:
    """Expected calibration error of each question's top answer (laya's answer_confidence), per bucket."""
    from laya import ece_score
    out = {}
    for qid in QUESTIONS:
        p = softmax_rows(np.stack(logits_by_q[qid]), (temps or {}).get(BUCKETS[qid], 1.0))
        out[BUCKETS[qid]] = round(float(ece_score(p.max(axis=1), p.argmax(axis=1) == labels_by_q[qid])), 4)
    return out


def question_metrics(answers: dict, rows: list[dict]) -> dict:
    """Per question on the select split: stack-role accuracy and confusion, dealbreaker precision and recall, fit mean
    absolute error and Spearman correlation of the expected score; each with its majority-class share. Dealbreaker
    predictions exist for every row (gating needs them even when dealbreaker_known is False), but deviation requested
    by the controller: rows whose true dealbreaker is unknown are left out of its accuracy, precision and recall. When
    every row is unknown, the dealbreaker metrics are None rather than NaN (no known row to measure against)."""
    from scipy.stats import spearmanr
    truth = true_labels(rows)
    role_pred = np.array([STACK_ROLES.index(r) for r in answers["role"]])
    known = np.array([bool(r.get("dealbreaker_known", True)) for r in rows])
    deal_pred = (np.asarray(answers["deal_p"]) >= GATES["dealbreaker_at"]).astype(int)[known]
    deal_true = truth["dealbreaker"][known]
    expected = summarise(answers["fit_p"], "expected")
    scores = truth["fit"] + 1
    confusion = {STACK_ROLES[t]: {STACK_ROLES[p]: int(((truth["stack_role"] == t) & (role_pred == p)).sum())
                                  for p in range(len(STACK_ROLES)) if ((truth["stack_role"] == t) & (role_pred == p)).any()}
                 for t in range(len(STACK_ROLES)) if (truth["stack_role"] == t).any()}
    rho = spearmanr(expected, scores).statistic if len(set(scores)) > 1 and len(set(expected)) > 1 else 0.0
    if known.any():
        tp = int(((deal_pred == 1) & (deal_true == 1)).sum())
        dealbreaker = {"accuracy": float((deal_pred == deal_true).mean()),
                       "majority": float(max(deal_true.mean(), 1 - deal_true.mean())),
                       "precision": tp / max(1, int(deal_pred.sum())), "recall": tp / max(1, int(deal_true.sum()))}
    else:
        dealbreaker = {"accuracy": None, "majority": None, "precision": None, "recall": None}
    return {
        "stack_role": {"accuracy": float((role_pred == truth["stack_role"]).mean()),
                       "majority": Counter(truth["stack_role"].tolist()).most_common(1)[0][1] / len(rows),
                       "confusion": confusion},
        "dealbreaker": dealbreaker,
        "fit": {"mae": float(np.abs(expected - scores).mean()), "spearman": float(rho)},
    }


def build_questions_report(sel_logits: dict[str, list], cal_logits: dict[str, list], select_rows: list[dict],
                           calibrate_rows: list[dict], settings: Settings,
                           extra: dict | None = None) -> tuple[dict, dict[str, float], dict]:
    """Calibrate per bucket on the calibrate split, then on the select split: choose the fit summary by average
    precision among gated jobs, fit each summary's alert and save cut-offs, and report. Returns (report, bucket
    temperatures, the exported `cutoffs`)."""
    cal_truth = true_labels(calibrate_rows)
    sel_truth = true_labels(select_rows)
    raw_temps = fit_bucket_temperatures(*_exclude_unknown_dealbreaker(cal_logits, cal_truth, calibrate_rows))
    # Round once, here, so the report's answers/cut-offs and the exported config use identical values.
    temps = {k: round(float(v), 4) for k, v in raw_temps.items()}
    a = answers_from_logits(sel_logits, temps)
    y = np.array([int(r["label"] == "notify") for r in select_rows])
    gold = [r["label"] for r in select_rows]
    gated = gate_mask(a["role"], a["deal_p"])
    chosen, aps = choose_summary(a["fit_p"], gated, y)
    summaries = {}
    for name in SUMMARIES:
        s = summarise(a["fit_p"], name)
        cut = pick_gated_cutoff(s, gated, y, settings.min_recall)
        save_at = pick_save_cutoff(s[gated], [g for g, k in zip(gold, gated) if k], cut.threshold)
        lo, hi = wilson(round(cut.precision * cut.alerts), cut.alerts)
        summaries[name] = {
            "average_precision": round(aps[name], 4), "alert_at": floor4(cut.threshold), "save_at": floor4(save_at),
            "select": asdict(cut), "worth_range": [round(lo, 4), round(hi, 4)],
            "per100": round(100 * cut.alerts / max(1, len(y)), 1),
            "table": gated_cutoff_table(s, gated, y, cut.threshold, SUMMARY_STEP[name]),
            "no_gates": asdict(pick_gated_cutoff(s, np.ones_like(gated), y, settings.min_recall)),
            "range": [round(float(s[gated].min()), 4), round(float(s[gated].max()), 4)] if gated.any() else [0.0, 0.0],
        }
    decided = decide(gated, summarise(a["fit_p"], chosen), summaries[chosen]["alert_at"], summaries[chosen]["save_at"])
    ece_logits, ece_truth = _exclude_unknown_dealbreaker(sel_logits, sel_truth, select_rows)
    report = {
        "summary": chosen, "summaries": summaries,
        "select_jobs": len(y), "good_jobs": int(y.sum()), "gated_jobs": int(gated.sum()),
        "base_rate": float(y[gated].mean()) if gated.any() else 0.0,
        "confusion": {g: {d: int(((np.array(gold) == g) & (decided == d)).sum()) for d in LABELS} for g in LABELS},
        "temperatures": temps,
        "ece_before": calibration_errors(ece_logits, ece_truth), "ece_after": calibration_errors(ece_logits, ece_truth, temps),
        "questions": question_metrics(a, select_rows),
        **(extra or {}),
    }
    cutoffs = {"summary": chosen, "alert_at": summaries[chosen]["alert_at"], "save_at": summaries[chosen]["save_at"]}
    return json.loads(json.dumps(report, default=_plain_json)), temps, cutoffs


def questions_report_text(report: dict) -> str:
    chosen = report["summary"]
    c = report["summaries"][chosen]
    s = c["select"]
    lines = [f"Base checkpoint: {report.get('base', 'unknown')}",
             f"Select split ({report['select_jobs']} jobs, {report['good_jobs']} good, {report['gated_jobs']} pass the "
             f"gates, base rate among gated {report['base_rate']:.0%})"]
    for name, r in report["summaries"].items():
        sel, ng = r["select"], r["no_gates"]
        lines.append(f"  {name}{' (chosen)' if name == chosen else ''}: average precision {r['average_precision']:.3f}; "
                     f"alert at {r['alert_at']:.4g}, save at {r['save_at']:.4g}: {sel['alerts']} alerts "
                     f"({r['per100']} per 100), {sel['precision']:.0%} worth opening "
                     f"[{r['worth_range'][0]:.0%}-{r['worth_range'][1]:.0%}], caught {sel['recall']:.0%}; "
                     f"without gates: {ng['alerts']} alerts, {ng['precision']:.0%} worth opening, caught {ng['recall']:.0%}")
        lines += [f"      {t:.4g}  {p:.0%}  {rc:.0%}  {n}" for t, p, rc, n in r["table"]]
    q = report["questions"]
    lines.append(f"stack_role: accuracy {q['stack_role']['accuracy']:.0%} (majority {q['stack_role']['majority']:.0%}); "
                 f"confusion {json.dumps(q['stack_role']['confusion'])}")
    if q["dealbreaker"]["accuracy"] is None:
        lines.append("dealbreaker: n/a (no row with a known dealbreaker)")
    else:
        lines.append(f"dealbreaker: precision {q['dealbreaker']['precision']:.0%}, recall {q['dealbreaker']['recall']:.0%}, "
                     f"accuracy {q['dealbreaker']['accuracy']:.0%} (majority {q['dealbreaker']['majority']:.0%})")
    lines.append(f"fit: mean absolute error {q['fit']['mae']:.2f}, Spearman {q['fit']['spearman']:.2f}")
    lines.append(f"Temperatures {report['temperatures']}; calibration error {report['ece_before']} -> {report['ece_after']}")
    lines.append("Confusion (rows: label; columns: notify / log / skip): " + json.dumps(report["confusion"]))
    for key in ("class_counts", "dealbreaker_counts", "health", "history", "best_epoch"):
        if key in report:
            lines.append(f"{key}: {json.dumps(report[key])}")
    if report["gated_jobs"] == 0:
        lines.append("!! No select job passes the predicted gates: the stack_role or dealbreaker question is broken.")
    if "fallback" in str(report.get("base", "")):
        lines.append("!! Trained from the root laya checkpoint because typed-decisions could not be loaded.")
    if not s["met_rule"]:
        lines.append("!! No cut-off caught half the good jobs: the model is weak; do not switch to it.")
    if s["precision"] <= report["base_rate"]:
        lines.append(f"!! Notify alerts are no better than the gated base rate ({s['precision']:.0%} against "
                     f"{report['base_rate']:.0%}): do not switch to this model.")
    width = c["range"][1] - c["range"][0]
    if width < (2.0 if chosen == "expected" else 0.2):
        lines.append(f"!! The fit summary sits in a narrow band ({c['range'][0]:.3g} to {c['range'][1]:.3g}): check "
                     "before switching.")
    for qid in ("stack_role", "dealbreaker"):
        if q[qid]["accuracy"] is not None and q[qid]["accuracy"] < q[qid]["majority"] + 0.05:
            lines.append(f"!! {qid} accuracy {q[qid]['accuracy']:.0%} is near its majority class "
                         f"({q[qid]['majority']:.0%}): the question has not learned much.")
    return "\n".join(lines)


def export_questions_checkpoint(state, base_dir, base_cfg, out_dir, settings: Settings, temps: dict[str, float],
                                cutoffs: dict, report: dict) -> None:
    from laya.agent import clamp_temperature
    out = _write_weights(state, base_dir, out_dir)
    cfg = json.loads(json.dumps(base_cfg))
    cfg["max_len"], cfg["head_max_len"] = settings.max_len, settings.head_max_len
    carried = cfg.setdefault("temperature_by_options", {})
    # Buckets we did not fit (e.g. a base checkpoint's choice:11+) are carried as-is: clamp them so Agent(out) never
    # has to warn about an invalid or out-of-range temperature on load. Buckets we did fit overwrite them below.
    carried.update({k: clamp_temperature(v) for k, v in carried.items() if k not in temps})
    carried.update({k: round(float(v), 4) for k, v in temps.items()})
    cfg["training"] = {"decision_method": {"own": "questions"}, "questions": QUESTIONS,
                       "inputs": question_inputs(settings), "gates": GATES,
                       # floor4, not round: a cut-off must never be rounded up past the job it was fitted on.
                       "cutoffs": {"summary": cutoffs["summary"], "alert_at": floor4(cutoffs["alert_at"]),
                                   "save_at": floor4(cutoffs["save_at"])},
                       "report": report}
    (out / "rl_agent_config.json").write_text(json.dumps(cfg, indent=2, default=_plain_json))


BASE_REPO = "convaiinnovations/laya"
BASE_SUBFOLDER = "typed-decisions"


def download_base(subfolder: str | None) -> str:
    """One checkpoint folder of convaiinnovations/laya: a subfolder, or the root checkpoint when None."""
    import os
    from huggingface_hub import snapshot_download
    prefix = f"{subfolder}/" if subfolder else ""
    root = snapshot_download(BASE_REPO, allow_patterns=[prefix + name for name in (
        "rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*")])
    return os.path.join(root, subfolder) if subfolder else root


def load_question_base(device, log=print, download=download_base, load=load_base):
    """The typed-decisions checkpoint (trained multi-question at 1,024 / 256); if it cannot be loaded, the root laya
    checkpoint, said loudly. Returns (model, tok, cfg, base_dir, note) where note goes into the report. The failure
    is reported by exception class only: the exception's own message can carry a local cache path (home directory,
    username), so neither the note nor the log line repeats it."""
    try:
        base = download(BASE_SUBFOLDER)
        model, tok, cfg = load(base, device)
        return model, tok, cfg, base, BASE_SUBFOLDER
    except Exception as e:
        why = type(e).__name__
        log(f"!! {BASE_SUBFOLDER} could not be loaded ({why}); falling back to the root laya checkpoint")
    base = download(None)
    model, tok, cfg = load(base, device)
    return model, tok, cfg, base, f"root laya (fallback: {BASE_SUBFOLDER} not found ({why}))"


# ---- Arithmetic for an offline benchmark of your own ----------------------------------------------------------------
# Not used by training or by jobhunter: for comparing a new checkpoint's alerts with what you run today, on jobs you
# have labelled yourself. The baseline is yours to measure: {"alerts": N, "caught": K, "good": G}, alert_summary's
# fields for the setup you run now.


def alert_summary(decisions: dict[str, str], labels: dict[str, str], keep: set[str] | None = None) -> dict:
    """Alerts among the jobs in `keep` (all labelled jobs when None): how many, per 100 benchmark jobs, worth opening
    with its 95% range, and good jobs caught out of every good job in `labels`."""
    ids = [i for i in labels if keep is None or i in keep]
    alerts = [i for i in ids if decisions.get(i) == "notify"]
    k = sum(labels[i] == "notify" for i in alerts)
    lo, hi = wilson(k, len(alerts))
    return {"alerts": len(alerts), "per100": round(100 * len(alerts) / max(1, len(labels)), 1),
            "worth": k / len(alerts) if alerts else 0.0, "worth_range": [round(lo, 3), round(hi, 3)],
            "caught": k, "good": sum(v == "notify" for v in labels.values())}


def go_no_go(hybrid: dict, baseline: dict) -> bool:
    """GO only if, inside the hybrid, more alerts are worth opening than the baseline's (your current setup on the
    same labelled jobs, {"alerts": N, "caught": K}) without catching fewer good jobs."""
    return hybrid["worth"] > baseline["caught"] / baseline["alerts"] and hybrid["caught"] >= baseline["caught"]
