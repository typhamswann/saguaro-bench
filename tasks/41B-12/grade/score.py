#!/usr/bin/env python3
"""SaguaroBench curation scorer — copied verbatim into each task's grade/score.py.

Verifier-side. Reads:
    /workspace/submission.json   the agent's output (list of row dicts)
    /grade/truth.json            ground truth + scoring schema:
        {
          "saguaro_id": "41B-13",
          "scored_fields": ["saguaro_id","direction","A","B","C","D","E","note"],
          "tolerances": {"direction": 1.0, "A": 0.011, ...},
          "truth_rows": [
            {"saguaro_id": "41B-13", "year": 2023, "arm": "1",
             "direction": 360, "A": 1.89, ..., "note": ["5 nubbins","5 nubbins!"]},
            ...
            {... "_excluded": true ...}   # skipped: any/no submission accepted
          ]
        }

Writes a JSON object on stdout. test.sh redirects it to
/logs/verifier/reward.json and pulls reward.txt out via jq.

Scoring (mirrors saguaro_curation/rubric.py from the source env):
- Submission must parse as a list of dicts each with at least {saguaro_id, year, arm}.
  Accepts {"rows": [...]} wrapper, or {"submission": "<json-string>"} wrapper.
- Truth rows are keyed by (saguaro_id_canonical, year, arm-string).
- _excluded rows are skipped entirely: their cells don't count and an extra
  submission at that key is not penalized.
- For each non-excluded truth row, score per-cell:
    * direction: numeric ±1.0°
    * A, B, C, D, E: numeric ±0.011 m
    * note: list-of-acceptable match OR Jaccard word-set similarity ≥0.5
            (empty matches empty)
    * saguaro_id: normalized string equality
- Missing truth rows score 0 across all their cells.
- "Extra" predicted rows (not in truth, not _excluded) incur 0.05 each penalty,
  capped at 0.5.
- Final: cell_accuracy_reward = max(0, correct/total - extra_penalty), in [0,1].

Self-contained (stdlib only) — runs under any python3.10+ without pip.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Field schema
# ---------------------------------------------------------------------------
NUMERIC_FIELDS = ("direction", "A", "B", "C", "D", "E")
DEFAULT_TOLERANCES = {
    "direction": 1.0,
    "A": 0.011, "B": 0.011, "C": 0.011, "D": 0.011, "E": 0.011,
}
STRING_FIELDS = ("saguaro_id", "note")
ALL_FIELDS = NUMERIC_FIELDS + STRING_FIELDS  # 8 cells per row

EXTRA_ROW_PENALTY = 0.05
EXTRA_ROW_PENALTY_CAP = 0.5


# ---------------------------------------------------------------------------
# Field matchers
# ---------------------------------------------------------------------------
def _norm_str(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _numeric_match(pred: Any, truth: Any, tol: float) -> bool:
    if truth is None and (pred is None or pred == ""):
        return True
    if truth is None or pred is None or pred == "":
        return False
    try:
        return abs(float(pred) - float(truth)) <= tol
    except (TypeError, ValueError):
        return False


_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "of", "to", "in", "on",
    "and", "or", "but", "with", "for", "at", "by", "from", "as", "that",
    "this", "it", "be", "has", "have", "had",
})


def _word_set(s: Any) -> set:
    return {w for w in _norm_str(s).split() if w and w not in _STOPWORDS}


def _note_match_single(pred: Any, truth: Any) -> bool:
    """Compare one predicted note against one truth note string."""
    p_norm = _norm_str(pred)
    t_norm = _norm_str(truth)
    if p_norm == "" and t_norm == "":
        return True
    if p_norm == t_norm:
        return True
    # Jaccard word-set ≥ 0.5 (ignoring stopwords)
    p_words = _word_set(pred)
    t_words = _word_set(truth)
    if not p_words and not t_words:
        return True
    if not p_words or not t_words:
        return False
    j = len(p_words & t_words) / len(p_words | t_words)
    return j >= 0.5


def _note_match(pred: Any, truth: Any) -> bool:
    """Truth may be a string OR a list of acceptable strings. If list, any
    member matching counts.
    """
    if isinstance(truth, list):
        return any(_note_match_single(pred, t) for t in truth)
    return _note_match_single(pred, truth)


_SAGUARO_ID_RE = re.compile(r"^([A-Za-z0-9]+)-?(.+)$")


def _canon_saguaro_id(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).strip()
    m = _SAGUARO_ID_RE.match(s)
    if m:
        plot, rest = m.group(1), m.group(2).strip()
        try:
            plot = str(int(plot))
        except ValueError:
            pass
        rest = rest.upper()
        return f"{plot}-{rest}"
    return s


def _saguaro_id_match(pred: Any, truth: Any) -> bool:
    return _canon_saguaro_id(pred) == _canon_saguaro_id(truth)


def _field_match(field: str, pred: Any, truth: Any, tolerances: dict) -> bool:
    if field in NUMERIC_FIELDS:
        return _numeric_match(pred, truth, tolerances.get(field, 0.0))
    if field == "saguaro_id":
        return _saguaro_id_match(pred, truth)
    if field == "note":
        return _note_match(pred, truth)
    return _norm_str(pred) == _norm_str(truth)


# ---------------------------------------------------------------------------
# Key + submission parsing
# ---------------------------------------------------------------------------
def _row_key(row: dict) -> tuple:
    return (_canon_saguaro_id(row.get("saguaro_id")), int(row["year"]), str(row["arm"]))


def _is_excluded(row: dict) -> bool:
    return bool(row.get("_excluded"))


def parse_submission(text: str):
    """Returns (rows_list, error_str)."""
    try:
        obj = json.loads(text)
    except Exception as e:
        return None, f"invalid_json: {e}"
    # Accept {"submission": "<json-string>"} wrapper
    if isinstance(obj, dict) and "submission" in obj and len(obj) == 1:
        inner = obj["submission"]
        if isinstance(inner, str):
            try:
                obj = json.loads(inner)
            except Exception as e:
                return None, f"invalid_json (inner submission): {e}"
        elif isinstance(inner, (list, dict)):
            obj = inner
    # Accept {"rows": [...]} wrapper
    if isinstance(obj, dict) and "rows" in obj:
        obj = obj["rows"]
    if not isinstance(obj, list):
        return None, f"submission_not_list: {type(obj).__name__}"
    out = []
    for i, r in enumerate(obj):
        if not isinstance(r, dict):
            return None, f"row_{i}_not_dict"
        for required in ("saguaro_id", "year", "arm"):
            if required not in r:
                return None, f"row_{i}_missing_{required}"
        try:
            r["year"] = int(r["year"])
        except (TypeError, ValueError):
            return None, f"row_{i}_year_not_int"
        r["arm"] = str(r["arm"])
        out.append(r)
    return out, None


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------
def cell_accuracy_reward(pred_rows, truth):
    truth_rows = truth["truth_rows"]
    scored_fields = tuple(truth.get("scored_fields", ALL_FIELDS))
    tolerances = truth.get("tolerances", DEFAULT_TOLERANCES)

    truth_scored = [r for r in truth_rows if not _is_excluded(r)]
    excluded_keys = {_row_key(r) for r in truth_rows if _is_excluded(r)}
    truth_by_key = {_row_key(r): r for r in truth_scored}
    pred_by_key = {_row_key(r): r for r in pred_rows}

    # Per-field stats for diagnostics.
    per_field = {f: {"correct": 0, "total": 0} for f in scored_fields}

    correct_total = 0
    total = 0
    for key, truth_row in truth_by_key.items():
        pred_row = pred_by_key.get(key)
        for field in scored_fields:
            total += 1
            per_field[field]["total"] += 1
            if pred_row is None:
                continue
            if _field_match(field, pred_row.get(field), truth_row.get(field), tolerances):
                correct_total += 1
                per_field[field]["correct"] += 1

    base = correct_total / max(1, total)
    n_extra = len(set(pred_by_key) - set(truth_by_key) - excluded_keys)
    penalty = min(EXTRA_ROW_PENALTY_CAP, EXTRA_ROW_PENALTY * n_extra)

    reward = max(0.0, base - penalty)

    # Row presence stats.
    truth_keys = set(truth_by_key)
    pred_keys_scored = set(pred_by_key) - excluded_keys
    tp = len(truth_keys & pred_keys_scored)
    missing = len(truth_keys - pred_keys_scored)
    extra = len(pred_keys_scored - truth_keys)
    row_p = tp / max(1, len(pred_keys_scored))
    row_r = tp / max(1, len(truth_keys))
    row_f1 = 2 * row_p * row_r / (row_p + row_r) if (row_p + row_r) > 0 else 0.0

    return {
        "cell_accuracy_reward": round(reward, 6),
        "base_cell_accuracy": round(base, 6),
        "extra_row_penalty": round(penalty, 6),
        "row_f1": round(row_f1, 6),
        "rows_truth": len(truth_keys),
        "rows_pred_scored": len(pred_keys_scored),
        "rows_matched": tp,
        "rows_missing": missing,
        "rows_extra": extra,
        "rows_excluded": len(excluded_keys),
        "per_field_accuracy": {
            f: round(s["correct"] / max(1, s["total"]), 6) for f, s in per_field.items()
        },
    }


def main(argv):
    if len(argv) != 3:
        print('{"cell_accuracy_reward": 0.0, "structural_error": '
              '"score.py: expected <submission.json> <truth.json>"}')
        return 0

    sub_path = Path(argv[1])
    truth_path = Path(argv[2])
    truth = json.loads(truth_path.read_text())
    saguaro_id = truth.get("saguaro_id")

    if not sub_path.exists():
        out = {
            "cell_accuracy_reward": 0.0,
            "saguaro_id": saguaro_id,
            "structural_error": "no_submission",
        }
        print(json.dumps(out))
        return 0

    rows, err = parse_submission(sub_path.read_text())
    if err is not None:
        out = {
            "cell_accuracy_reward": 0.0,
            "saguaro_id": saguaro_id,
            "structural_error": err,
        }
        print(json.dumps(out))
        return 0

    result = cell_accuracy_reward(rows, truth)
    result["saguaro_id"] = saguaro_id
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
