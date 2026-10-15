"""Interpretable decision-tree model for student not-pass risk → faculty actions.

WHY THIS EXISTS (read before trusting it): our isotonic momentum-risk model
(``history.momentum_risk_*``) already extracts ~all the signal a single near-static
Momentum-Indicator window offers (see ``data_analysis/DATA_REQUEST.md`` §4). A tree
will NOT beat it on raw accuracy — with only ~30 resolved non-passers a deep tree
just memorises noise. So this model is deliberately SHALLOW and its job is
different: turn the orthogonal signals the isotonic curve can't combine
(never-attempted, gone-silent, completion ratio, term pace) into a handful of
legible ``if/then`` leaves, each mapped to a concrete faculty ACTION. It emits a
calibrated leaf probability as a byproduct, and reports its own cross-validated
AUC against the isotonic baseline so we never over-trust it (the "accuracy report"
half of the design).

Pure stdlib — NO numpy/sklearn. The app ships as a lean PyInstaller build and its
logic is deliberately dependency-free (see requirements.txt); a few hundred lines
of CART keep it that way. Trains on the resolved ``outcomes`` archive joined to the
snapshot history, mirroring the exact query patterns ``momentum_risk_calibration``
already uses against the real schema.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Optional

from src.config import HISTORY_DB
from src import history as H


# ----------------------------------------------------------------------
# Feature set — small and legible ON PURPOSE (a shallow tree needs few axes).
# Each entry: (key, kind) where kind is "num" (CART finds a threshold) or "bool"
# (0/1). Ordered as they'll appear in the feature matrix.
# ----------------------------------------------------------------------
FEATURES: list[tuple[str, str]] = [
    ("avg_rank", "num"),          # momentum trajectory average (core signal)
    ("min_rank", "num"),          # deepest slump (best single MI summary, §4b)
    ("trend", "num"),             # recent-half minus earlier-half (>0 recovering)
    ("completion_ratio", "num"),  # durable capability: CU completed / attempted
    ("enrolled_cu", "num"),       # fewer enrolled CUs = riskier (§4c)
    ("never_attempted", "bool"),  # §12 attempting ≈ passing (orthogonal to MI)
    ("gone_silent", "bool"),      # §18 contacted, never replied (risk amplifier)
    ("otp_behind", "bool"),       # TermOTPStatus == "Behind"
]
FEATURE_NAMES = [k for k, _ in FEATURES]

# Human labels for rule rendering.
FEATURE_LABELS = {
    "avg_rank": "avg momentum rank",
    "min_rank": "min momentum rank",
    "trend": "momentum trend",
    "completion_ratio": "completion ratio",
    "enrolled_cu": "enrolled CUs",
    "never_attempted": "never attempted a task",
    "gone_silent": "gone silent",
    "otp_behind": "behind on term pace",
}


# ----------------------------------------------------------------------
# Training-example extraction (mirrors momentum_risk_calibration's joins)
# ----------------------------------------------------------------------
def _series_and_latest(conn):
    """Per (student, course): the FULL momentum-rank series (most stable, like the
    isotonic calibration) and the latest snapshot row (for durable fields)."""
    series = defaultdict(list)
    for s in conn.execute("SELECT student_id, course_code, momentum_rank "
                          "FROM snapshots WHERE momentum_rank IS NOT NULL "
                          "ORDER BY collected_date"):
        series[(s["student_id"], s["course_code"])].append(s["momentum_rank"])
    latest = {}
    for r in conn.execute(
            "SELECT s.* FROM snapshots s JOIN (SELECT student_id, course_code, "
            "MAX(collected_at) mx FROM snapshots GROUP BY student_id, course_code) m "
            "ON s.student_id=m.student_id AND s.course_code=m.course_code "
            "AND s.collected_at=m.mx"):
        latest[(r["student_id"], r["course_code"])] = r
    return series, latest


def _responded_set(conn) -> set:
    """Students who have EVER replied — one query, mirroring engagement_ledger's
    'responds' rule (any inbound on a known channel, or any outbound call). Batched
    on the caller's connection so we don't open one per student (fast, and avoids a
    per-student connection churn that locks the DB file on Windows). Degrades to an
    empty set if the notes table is absent."""
    try:
        rows = conn.execute("SELECT DISTINCT student_id, channel, direction "
                            "FROM notes WHERE created_at != ''").fetchall()
    except Exception:
        return set()
    out = set()
    for r in rows:
        c = r["channel"]
        if c not in H._CHANNEL_COST:
            continue
        if r["direction"] == "inbound" or (r["direction"] == "outbound"
                                           and c == "call"):
            out.add(r["student_id"])
    return out


def _feature_row(h: list, r, e: dict, responded: set) -> dict:
    """Build the raw (pre-imputation) feature dict for one (student, course).
    Numeric features may be None (missing); imputed to the column median at fit."""
    avg = sum(h) / len(h)
    half = len(h) // 2 or 1
    trend = (sum(h[half:]) / max(1, len(h) - half)) - (sum(h[:half]) / half)
    comp = H._to_int(H._ej_get(e, "TotalCompletedCU"))
    att = H._to_int(H._ej_get(e, "TotalAttemptedCU"))
    ratio = (comp / att) if (att and att > 0 and comp is not None) else None
    lts = r["latest_task_status"] if r is not None else ""
    never = not H._has_attempted(lts, e)
    dslc = H._to_int(H._ej_get(e, "DaysSinceLastCourseContact"))
    sid = r["student_id"] if r is not None else ""
    silent = (dslc is not None and bool(sid) and sid not in responded)
    return {
        "avg_rank": avg,
        "min_rank": float(min(h)),
        "trend": trend,
        "completion_ratio": ratio,
        "enrolled_cu": H._to_int(H._ej_get(e, "EnrolledCU")),
        "never_attempted": 1.0 if never else 0.0,
        "gone_silent": 1.0 if silent else 0.0,
        "otp_behind": 1.0 if H._ej_get(e, "TermOTPStatus") == "Behind" else 0.0,
    }


def _resolved_rich(db_path) -> list[dict]:
    """Every resolved (student, course) with a momentum reading, as a rich record:
    ``{student_id, course_code, name, feats, outcome, label}``. Shared by
    training_examples (features+labels) and resolved_examples (the rich list for
    the rule inspector)."""
    conn = H._connect(db_path)
    try:
        series, latest = _series_and_latest(conn)
        responded = _responded_set(conn)
        out = []
        for o in conn.execute("SELECT student_id, course_code, outcome FROM outcomes"):
            key = (o["student_id"], o["course_code"])
            h = series.get(key)
            if not h:
                continue
            r = latest.get(key)
            try:
                e = json.loads(r["extra_json"] or "{}") if r is not None else {}
            except Exception:
                e = {}
            out.append({
                "student_id": o["student_id"], "course_code": o["course_code"],
                "name": (r["name"] if r is not None else "") or "",
                "feats": _feature_row(h, r, e, responded),
                "outcome": o["outcome"],
                "label": 1 if o["outcome"] == "not_passed" else 0,
            })
    finally:
        conn.close()
    return out


def training_examples(*, db_path=HISTORY_DB) -> tuple[list[dict], list[int]]:
    """Extract (feature_dicts, labels) for every resolved (student, course) that
    has a momentum reading. label = 1 for not_passed, 0 for passed. Mirrors the
    isotonic calibration's cohort so the two models are compared on equal footing."""
    rich = _resolved_rich(db_path)
    return [x["feats"] for x in rich], [x["label"] for x in rich]


def resolved_examples(*, db_path=HISTORY_DB) -> list[dict]:
    """The rich resolved records (id/name/feats/outcome) — for the 'who were the
    students behind this rule?' inspector."""
    return _resolved_rich(db_path)


def live_examples(*, db_path=HISTORY_DB, now=None) -> list[dict]:
    """In-progress, UNRESOLVED caseload students (from the latest snapshot) with
    their feature dicts + id/name. Gated exactly like momentum_risk_students
    (course underway, not resolved, has a momentum reading), so a rule learned on
    resolved data is applied to the same kind of live student. Records:
    ``{student_id, course_code, name, feats}``."""
    from datetime import datetime
    today = (now or datetime.now()).date()
    conn = H._connect(db_path)
    try:
        series, _latest = _series_and_latest(conn)
        responded = _responded_set(conn)
        resolved = {(r["student_id"], r["course_code"]) for r in
                    conn.execute("SELECT student_id, course_code FROM outcomes")}
        col = conn.execute("SELECT collected_at FROM collections "
                           "ORDER BY collected_at DESC LIMIT 1").fetchone()
        if col is None:
            return []
        rows = conn.execute("SELECT * FROM snapshots WHERE collected_at = ?",
                            (col["collected_at"],)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        key = (r["student_id"], r["course_code"])
        if key in resolved:
            continue
        try:
            e = json.loads(r["extra_json"] or "{}")
        except Exception:
            e = {}
        status = H._ej_get(e, "CourseStatus")
        start = H._parse_date(H._ej_get(e, "CourseStartDate"))
        days_into = (today - start).days if start else None
        if status == "Planned" or days_into is None or days_into <= 0:
            continue
        h = series.get(key) or ([r["momentum_rank"]] if r["momentum_rank"] else [])
        if not h:
            continue
        out.append({
            "student_id": r["student_id"], "course_code": r["course_code"],
            "name": r["name"] or "", "feats": _feature_row(h, r, e, responded),
        })
    return out


def path_matches(node: "_Node", feats: dict, medians: dict) -> bool:
    """Does a student's feature dict satisfy every condition on a leaf's path?
    Missing numerics are imputed with the trained tree's ``medians`` (same as at
    fit), so live/resolved students route through the tree identically to training."""
    for fi, op, thr in node.path:
        name = FEATURE_NAMES[fi]
        v = feats.get(name)
        if v is None:
            v = medians.get(name, 0.0)
        v = float(v)
        if op == "<=" and not (v <= thr):
            return False
        if op == ">" and not (v > thr):
            return False
    return True


def _matrix(rows: list[dict], medians: Optional[dict] = None):
    """Turn feature dicts into a float matrix, imputing missing numerics with the
    column median (computed here if not supplied, so predict() reuses fit medians)."""
    if medians is None:
        medians = {}
        for k in FEATURE_NAMES:
            vals = sorted(x[k] for x in rows if x.get(k) is not None)
            medians[k] = vals[len(vals) // 2] if vals else 0.0
    X = [[(x[k] if x.get(k) is not None else medians[k]) for k in FEATURE_NAMES]
         for x in rows]
    return X, medians


# ----------------------------------------------------------------------
# CART (Gini) — shallow, pure Python
# ----------------------------------------------------------------------
def _gini(pos: int, n: int) -> float:
    if n == 0:
        return 0.0
    p = pos / n
    return 1.0 - p * p - (1 - p) * (1 - p)


class _Node:
    __slots__ = ("feat", "thr", "left", "right", "prob", "n", "pos", "path")

    def __init__(self):
        self.feat = None      # int feature index (internal) or None (leaf)
        self.thr = None       # split threshold: go left if value <= thr
        self.left = None
        self.right = None
        self.prob = None      # Laplace-smoothed leaf not-pass probability
        self.n = 0            # samples reaching this leaf
        self.pos = 0          # not-passers among them
        self.path = []        # list of (feat_idx, op, thr) constraints to reach here


class DecisionTree:
    """A shallow CART classifier for not-pass risk. ``max_depth`` and
    ``min_leaf`` keep it legible and overfit-resistant on the scarce non-passers.
    ``predict_proba`` returns Laplace-smoothed leaf not-pass probability."""

    def __init__(self, max_depth: int = 3, min_leaf: int = 8,
                 min_impurity_decrease: float = 1e-4):
        self.max_depth = max_depth
        self.min_leaf = min_leaf
        self.min_impurity_decrease = min_impurity_decrease
        self.root: Optional[_Node] = None
        self.medians: dict = {}
        self.base_rate: float = 0.0

    # ---- fit -------------------------------------------------------
    def fit(self, rows: list[dict], labels: list[int]) -> "DecisionTree":
        X, self.medians = _matrix(rows)
        y = list(labels)
        self.base_rate = (sum(y) / len(y)) if y else 0.0
        idx = list(range(len(y)))
        self.root = self._grow(X, y, idx, depth=0, path=[])
        return self

    def _leaf(self, y, idx, path) -> _Node:
        node = _Node()
        node.n = len(idx)
        node.pos = sum(y[i] for i in idx)
        # Laplace smoothing toward the base rate keeps tiny leaves honest.
        node.prob = (node.pos + 1) / (node.n + 2)
        node.path = path
        return node

    def _grow(self, X, y, idx, depth, path) -> _Node:
        pos = sum(y[i] for i in idx)
        # Pure, too small, or too deep → leaf.
        if (depth >= self.max_depth or len(idx) < 2 * self.min_leaf
                or pos == 0 or pos == len(idx)):
            return self._leaf(y, idx, path)
        best = self._best_split(X, y, idx)
        if best is None:
            return self._leaf(y, idx, path)
        feat, thr, left_idx, right_idx = best
        node = _Node()
        node.feat, node.thr = feat, thr
        node.n, node.pos, node.path = len(idx), pos, path
        node.left = self._grow(X, y, left_idx, depth + 1,
                               path + [(feat, "<=", thr)])
        node.right = self._grow(X, y, right_idx, depth + 1,
                                path + [(feat, ">", thr)])
        return node

    def _best_split(self, X, y, idx):
        n = len(idx)
        parent = _gini(sum(y[i] for i in idx), n)
        best_gain, best = 0.0, None
        for f in range(len(FEATURE_NAMES)):
            vals = sorted({X[i][f] for i in idx})
            for a, b in zip(vals, vals[1:]):
                thr = (a + b) / 2.0
                li = [i for i in idx if X[i][f] <= thr]
                ri = [i for i in idx if X[i][f] > thr]
                if len(li) < self.min_leaf or len(ri) < self.min_leaf:
                    continue
                gl = _gini(sum(y[i] for i in li), len(li))
                gr = _gini(sum(y[i] for i in ri), len(ri))
                weighted = (len(li) * gl + len(ri) * gr) / n
                gain = parent - weighted
                if gain > best_gain + 1e-12:
                    best_gain, best = gain, (f, thr, li, ri)
        if best is None or best_gain < self.min_impurity_decrease:
            return None
        return best

    # ---- predict ---------------------------------------------------
    def _leaf_for(self, xrow) -> _Node:
        node = self.root
        while node.feat is not None:
            node = node.left if xrow[node.feat] <= node.thr else node.right
        return node

    def predict_proba(self, rows: list[dict]) -> list[float]:
        X, _ = _matrix(rows, self.medians)
        return [self._leaf_for(x).prob for x in X]

    # ---- introspection ---------------------------------------------
    def leaves(self) -> list[_Node]:
        out = []

        def walk(n):
            if n.feat is None:
                out.append(n)
            else:
                walk(n.left)
                walk(n.right)

        if self.root is not None:
            walk(self.root)
        return out


# ----------------------------------------------------------------------
# Rule + action rendering (the "direct faculty actions" half)
# ----------------------------------------------------------------------
def _render_condition(feat_idx: int, op: str, thr: float) -> str:
    name = FEATURE_NAMES[feat_idx]
    label = FEATURE_LABELS[name]
    kind = dict(FEATURES)[name]
    if kind == "bool":
        # threshold sits at 0.5; "> 0.5" means the flag is TRUE.
        is_true = (op == ">")
        return label if is_true else f"not {label}"
    return f"{label} {op} {thr:.2f}"


def rule_text(node: _Node) -> str:
    """Human-readable AND-joined path to a leaf."""
    if not node.path:
        return "(all students)"
    return " AND ".join(_render_condition(f, op, thr) for f, op, thr in node.path)


def _path_has(node: _Node, feature: str, want_true: bool) -> bool:
    """Does the leaf's path constrain ``feature`` to the given boolean truth?"""
    fi = FEATURE_NAMES.index(feature)
    for f, op, thr in node.path:
        if f == fi:
            return (op == ">") == want_true
    return False


def suggested_action(node: _Node, base_rate: float) -> str:
    """Map a leaf to a concrete faculty action via a first-match priority ladder
    over its path constraints and calibrated risk. Ordered by intervention urgency
    and grounded in the validated FINDINGS signals."""
    high = node.prob >= max(2 * base_rate, 0.15)
    if _path_has(node, "never_attempted", True):
        return "Call to unblock the first task — attempting ≈ passing (§12)"
    if _path_has(node, "gone_silent", True):
        return "Escalate: multi-channel + mentor loop — non-response amplifies risk (§18)"
    if high:
        return "Priority coaching contact on the current task (highest-risk leaf)"
    if _path_has(node, "otp_behind", True):
        return "Pace check-in — behind on term progress"
    if node.prob >= base_rate:
        return "Light-touch check-in text"
    return "Monitor — no action needed"


def describe(tree: DecisionTree) -> list[dict]:
    """Structured summary of every leaf: rule, n, not-passers, probability, lift
    over base rate, and the suggested action. Sorted riskiest-first."""
    out = []
    for lf in tree.leaves():
        out.append({
            "rule": rule_text(lf),
            "n": lf.n,
            "not_passers": lf.pos,
            "prob": lf.prob,
            "lift": (lf.prob / tree.base_rate) if tree.base_rate else float("nan"),
            "action": suggested_action(lf, tree.base_rate),
        })
    out.sort(key=lambda d: -d["prob"])
    return out


# ----------------------------------------------------------------------
# Evaluation — AUC + k-fold CV vs the isotonic baseline (accuracy report)
# ----------------------------------------------------------------------
def auc(scores: list[float], labels: list[int]) -> Optional[float]:
    """Rank-based ROC AUC (Mann-Whitney). None if only one class present."""
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0        # 1-based, ties share mean rank
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    sum_pos = sum(ranks[i] for i in range(len(scores)) if labels[i] == 1)
    n_pos, n_neg = len(pos), len(neg)
    return (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _fit_isotonic(rows: list[dict], labels: list[int]) -> list[float]:
    """Refit the shipped isotonic momentum-risk model (avg-rank bucket → PAVA
    not-pass rate) on the given rows ONLY — so the CV comparison trains both models
    on the same training fold and neither peeks at the held-out labels."""
    counts = [[0, 0] for _ in H._RISK_BUCKETS]
    for x, y in zip(rows, labels):
        bi = H._avg_rank_bucket_idx(x["avg_rank"])
        counts[bi][1] += 1
        if y == 1:
            counts[bi][0] += 1
    return H._isotonic_noninc([(c[0], c[1]) for c in counts])


def _isotonic_scores(rows: list[dict], prob: list[float]) -> list[float]:
    """Score rows with an already-fit isotonic ``prob`` table (the exact curve the
    shipped model interpolates), so the tree is judged against what it augments."""
    return [H._interp_risk(x["avg_rank"], prob) for x in rows]


def cross_val_auc(rows: list[dict], labels: list[int], *, folds: int = 5,
                  max_depth: int = 3, min_leaf: int = 8, seed: int = 0,
                  drop_features: tuple = ()) -> dict:
    """Stratified k-fold out-of-sample AUC for the tree, alongside the isotonic
    baseline's AUC on the SAME held-out rows. This is the honest accuracy report:
    if the tree can't beat isotonic out-of-sample, trust it only for its legible
    action leaves, not for ranking.

    ``drop_features`` temporarily removes features from the model (e.g.
    ``('never_attempted',)`` to check how much lift is the near-tautological
    never-attempted signal vs. genuinely orthogonal durable signal)."""
    import random
    if drop_features:
        saved = list(FEATURE_NAMES)
        FEATURE_NAMES[:] = [f for f in saved if f not in drop_features]
        try:
            return cross_val_auc(rows, labels, folds=folds, max_depth=max_depth,
                                 min_leaf=min_leaf, seed=seed)
        finally:
            FEATURE_NAMES[:] = saved
    rng = random.Random(seed)
    pos_idx = [i for i, y in enumerate(labels) if y == 1]
    neg_idx = [i for i, y in enumerate(labels) if y == 0]
    rng.shuffle(pos_idx)
    rng.shuffle(neg_idx)
    fold_of = {}
    for grp in (pos_idx, neg_idx):        # stratify: spread each class over folds
        for rank, i in enumerate(grp):
            fold_of[i] = rank % folds
    tree_scores, iso_scores, all_labels = [], [], []
    for f in range(folds):
        tr = [i for i in range(len(labels)) if fold_of[i] != f]
        te = [i for i in range(len(labels)) if fold_of[i] == f]
        if not te:
            continue
        tr_rows = [rows[i] for i in tr]
        tr_y = [labels[i] for i in tr]
        if len(set(tr_y)) < 2:
            continue
        t = DecisionTree(max_depth=max_depth, min_leaf=min_leaf).fit(tr_rows, tr_y)
        iso_prob = _fit_isotonic(tr_rows, tr_y)      # baseline, same training fold
        te_rows = [rows[i] for i in te]
        tree_scores += t.predict_proba(te_rows)
        iso_scores += _isotonic_scores(te_rows, iso_prob)
        all_labels += [labels[i] for i in te]
    return {
        "tree_auc": auc(tree_scores, all_labels),
        "isotonic_auc": auc(iso_scores, all_labels),
        "n": len(all_labels),
        "n_not_pass": sum(all_labels),
        "folds": folds,
    }
