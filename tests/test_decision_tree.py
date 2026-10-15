"""Tests for the interpretable not-pass decision tree (src/decision_tree).

Run: python tests/test_decision_tree.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import history  # noqa: E402
from src import decision_tree as DT  # noqa: E402

PAST = "2026-05-01"


def _tmp_db():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(p)
    return p


def _snap(conn, sid, rank, day, *, course="C769", attempted="Submitted",
          extra=None):
    ej = {"CourseStartDate": PAST, "CourseStatus": "Registered",
          "TermDaysLeft": "60", "DaysSinceLastCourseContact": "5",
          "StudentEmail": "s@wgu.edu", "TextingPreference": "Opted In",
          "TotalCompletedCU": "20", "TotalAttemptedCU": "24",
          "EnrolledCU": "12", "TermOTPStatus": "On Track"}
    if extra:
        ej.update(extra)
    momentum = {1: "Low", 2: "Med Low", 3: "Med", 4: "Med High", 5: "High"}[rank]
    conn.execute("INSERT OR IGNORE INTO collections (collected_at, "
                 "collected_date, bucket, row_count) VALUES (?, ?, ?, 1)",
                 (f"{day}T09:00:00", day, day))
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (collected_at, collected_date, "
        "student_id, course_code, name, momentum, momentum_rank, "
        "latest_task_status, extra_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"{day}T09:00:00", day, sid, course, f"S{sid}", momentum, rank,
         attempted, json.dumps(ej)))


def _outcome(conn, sid, outcome, ranks, *, course="C769", attempted="Submitted",
             extra=None):
    for i, rk in enumerate(ranks):
        _snap(conn, sid, rk, f"2026-06-{10+i:02d}", course=course,
              attempted=attempted, extra=extra)
    conn.execute("INSERT INTO outcomes (student_id, course_code, outcome) "
                 "VALUES (?, ?, ?)", (sid, course, outcome))


def _build(seed):
    db = _tmp_db()
    conn = history._connect(db)
    try:
        with conn:
            seed(conn)
    finally:
        conn.close()
    return db


# ---- pure CART unit tests (no DB) ---------------------------------------

def test_auc_perfect_and_random():
    # perfectly separating scores → AUC 1.0
    assert DT.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    # reversed → 0.0
    assert DT.auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    # all ties → 0.5
    assert abs(DT.auc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]) - 0.5) < 1e-9
    # single class → None
    assert DT.auc([0.1, 0.2], [1, 1]) is None


def test_tree_recovers_known_threshold():
    # avg_rank <= 2 → not-pass; > 2 → pass. Tree should split near there and
    # separate the two groups in probability.
    rows, labels = [], []
    for _ in range(20):
        rows.append({"avg_rank": 1.0, "min_rank": 1.0, "trend": 0.0,
                     "completion_ratio": 0.5, "enrolled_cu": 12,
                     "never_attempted": 0.0, "gone_silent": 0.0,
                     "otp_behind": 0.0})
        labels.append(1)
    for _ in range(20):
        rows.append({"avg_rank": 5.0, "min_rank": 5.0, "trend": 0.0,
                     "completion_ratio": 0.9, "enrolled_cu": 12,
                     "never_attempted": 0.0, "gone_silent": 0.0,
                     "otp_behind": 0.0})
        labels.append(0)
    tree = DT.DecisionTree(max_depth=2, min_leaf=5).fit(rows, labels)
    p_low = tree.predict_proba([rows[0]])[0]
    p_high = tree.predict_proba([rows[-1]])[0]
    assert p_low > 0.7 and p_high < 0.3, (p_low, p_high)


def test_missing_numeric_imputed_not_crash():
    rows = [{"avg_rank": 2.0, "min_rank": 1.0, "trend": 0.0,
             "completion_ratio": None, "enrolled_cu": None,
             "never_attempted": 1.0, "gone_silent": 0.0, "otp_behind": 1.0}
            for _ in range(10)]
    labels = [1, 0] * 5
    tree = DT.DecisionTree(max_depth=2, min_leaf=3).fit(rows, labels)
    # predicting a row with the same missing fields must not raise
    assert 0.0 <= tree.predict_proba([rows[0]])[0] <= 1.0


def test_depth_and_leaf_limits_respected():
    rows, labels = [], []
    for i in range(60):
        rows.append({"avg_rank": float(i % 5 + 1), "min_rank": 1.0, "trend": 0.0,
                     "completion_ratio": 0.5, "enrolled_cu": 12,
                     "never_attempted": float(i % 2), "gone_silent": 0.0,
                     "otp_behind": 0.0})
        labels.append(i % 3 == 0)
    tree = DT.DecisionTree(max_depth=2, min_leaf=8).fit(rows, labels)
    for lf in tree.leaves():
        assert lf.n >= 8 or lf.n == tree.root.n, lf.n   # min_leaf honoured
        assert len(lf.path) <= 2, lf.path               # depth honoured


# ---- action mapping ------------------------------------------------------

def test_never_attempted_leaf_maps_to_call_action():
    node = DT._Node()
    fi = DT.FEATURE_NAMES.index("never_attempted")
    node.path = [(fi, ">", 0.5)]
    node.prob = 0.4
    assert "Call to unblock" in DT.suggested_action(node, base_rate=0.06)
    assert "never attempted a task" in DT.rule_text(node)


def test_low_risk_leaf_maps_to_monitor():
    node = DT._Node()
    node.path = [(DT.FEATURE_NAMES.index("avg_rank"), ">", 4.0)]
    node.prob = 0.01
    assert DT.suggested_action(node, base_rate=0.06) == "Monitor — no action needed"


# ---- end-to-end over a seeded DB ----------------------------------------

def test_training_examples_from_db():
    def seed(c):
        for i in range(10):
            _outcome(c, f"L{i}", "not_passed" if i < 7 else "passed", [1, 1, 1],
                     attempted="")                        # never attempted
        for i in range(12):
            _outcome(c, f"H{i}", "passed", [5, 5, 5])
    db = _build(seed)
    try:
        rows, labels = DT.training_examples(db_path=db)
        assert len(rows) == 22 and sum(labels) == 7
        # never-attempted flag surfaced for the low group
        low = [r for r, y in zip(rows, labels) if r["avg_rank"] < 2]
        assert all(r["never_attempted"] == 1.0 for r in low)
        tree = DT.DecisionTree(max_depth=3, min_leaf=4).fit(rows, labels)
        assert tree.base_rate > 0
        desc = DT.describe(tree)
        assert desc and desc[0]["prob"] >= desc[-1]["prob"]   # sorted riskiest 1st
    finally:
        os.unlink(db)


def test_cross_val_runs_and_reports_both():
    def seed(c):
        for i in range(15):
            _outcome(c, f"L{i}", "not_passed" if i < 10 else "passed", [1, 2, 1])
        for i in range(25):
            _outcome(c, f"H{i}", "passed", [5, 4, 5])
    db = _build(seed)
    try:
        rows, labels = DT.training_examples(db_path=db)
        cv = DT.cross_val_auc(rows, labels, folds=4, max_depth=2, min_leaf=4)
        assert cv["n"] == len(labels)
        assert cv["tree_auc"] is not None and cv["isotonic_auc"] is not None
        assert 0.0 <= cv["tree_auc"] <= 1.0
    finally:
        os.unlink(db)


def test_path_matches_conditions_and_imputation():
    n = DT._Node()
    fi_na = DT.FEATURE_NAMES.index("never_attempted")
    fi_cr = DT.FEATURE_NAMES.index("completion_ratio")
    n.path = [(fi_na, ">", 0.5), (fi_cr, "<=", 0.9)]
    med = {k: 0.0 for k in DT.FEATURE_NAMES}
    assert DT.path_matches(n, {"never_attempted": 1.0, "completion_ratio": 0.5}, med)
    # fails the never_attempted condition
    assert not DT.path_matches(n, {"never_attempted": 0.0, "completion_ratio": 0.5}, med)
    # fails the completion-ratio ceiling
    assert not DT.path_matches(n, {"never_attempted": 1.0, "completion_ratio": 0.95}, med)
    # missing completion_ratio → imputed with median 0.0 (0.0 <= 0.9 → passes)
    assert DT.path_matches(n, {"never_attempted": 1.0, "completion_ratio": None}, med)


def test_live_and_resolved_examples():
    def seed(c):
        _outcome(c, "P1", "passed", [5, 5, 5])
        _outcome(c, "F1", "not_passed", [1, 1, 1])
        # live in-progress student (no outcome), snapshots on the same days so
        # they're in the latest collection
        for i, rk in enumerate([2, 2, 3]):
            _snap(c, "L1", rk, f"2026-06-{10+i:02d}")
    db = _build(seed)
    try:
        live = DT.live_examples(db_path=db, now=datetime(2026, 8, 2))
        assert {x["student_id"] for x in live} == {"L1"}, live
        assert "avg_rank" in live[0]["feats"]
        res = DT.resolved_examples(db_path=db)
        assert {x["student_id"] for x in res} == {"P1", "F1"}
        assert any(x["outcome"] == "not_passed" for x in res)
        # feature dict + label present on rich records
        assert all("feats" in x and "label" in x for x in res)
    finally:
        os.unlink(db)


def test_empty_db_graceful():
    def seed(c):
        pass
    db = _build(seed)
    try:
        rows, labels = DT.training_examples(db_path=db)
        assert rows == [] and labels == []
    finally:
        os.unlink(db)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception as ex:
            failed += 1
            print(f"FAIL  {fn.__name__}: {ex}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
