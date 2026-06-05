"""Generate per-saguaro CURATION tasks under tasks/ in DeepSWE-style layout.

For each saguaro in the v1 25-saguaro 41B set, this writes:

    tasks/<sid>/
        task.toml                 Harbor schema
        instruction.md            short pointer to brief.md
        brief.md                  task statement + opaque asset inventory +
                                  output schema + per-year canonical arm count
        assets/
            datasheets/
                sheet_A.png       hand-redacted (v2) — opaque (year hidden)
                sheet_B.png       hand-redacted (v2) — opaque
            photos/
                photo_001.jpg     opaque (year hidden, mixed across years)
                photo_002.jpg     ...
        grade/
            truth.json            v2 truth_rows (with notes/excluded) +
                                  scoring schema (scored_fields, tolerances)
            score.py              stdlib-only per-cell scorer
        environment/Dockerfile    FROM saguaro-bench-base; COPYs assets to
                                  /workspace, grade/ to /grade (root-locked),
                                  agent user owns /workspace
        tests/test.sh             python3 /grade/score.py /workspace/submission.json
                                  /grade/truth.json | tee /logs/verifier/reward.json
                                  jq -r .cell_accuracy_reward → /logs/verifier/reward.txt

Inputs (resolved relative to --source-repo, default =
saguaro_arm_matching_env next to this repo):

    {source_repo}/data/dataset.json
        v1 dataset — used ONLY for the 25-saguaro composition and the
        per-saguaro photo manifest.

    {source_repo}/data/curation_dataset_v2.json
        v2 dataset — provides the canonical-arm truth_rows that include
        all paper-faithful note overrides + _excluded rows accumulated
        during QA.

    {source_repo}/data/curation_workdir_v2/saguaro_sheet_map.json
        saguaro_id -> [hand-redacted v2 sheet basenames]. Used to pick which
        page in data/assets/datasheets_v2_hand_redacted/<plot>/*.png to
        bundle for the 2023 + 2026 datasheets.

    {source_repo}/data/assets/datasheets_v2_hand_redacted/<plot>/<file>.png
    {source_repo}/data/assets/photos/<sid>_<year>_photo_<n>.jpg

Sheet/photo bundles are renamed to opaque IDs at build time:
    {year, page}-bearing v2 names  ->  sheet_A.png, sheet_B.png  (order is
        deterministic per saguaro via a seeded shuffle, so the same task
        always gets the same opaque mapping)
    {sid, year, n}-bearing v1 names -> photo_001.jpg, photo_002.jpg, ...
        (years interleaved via the same seed)

When a hand-redacted v2 sheet is missing for (saguaro, year), the generator
falls back to the v1 auto-redacted PNG and marks
metadata.redaction_status = "mixed" or "auto" in task.toml.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = REPO_ROOT / "tasks"
LIB_DIR = REPO_ROOT / "scripts" / "lib"

sys.path.insert(0, str(LIB_DIR))
from brief import build_brief  # noqa: E402

# Tolerances baked into each task's truth.json. Mirrors saguaro_curation/rubric.py.
SCORED_FIELDS = ["saguaro_id", "direction", "A", "B", "C", "D", "E", "note"]
TOLERANCES = {
    "direction": 1.0,
    "A": 0.011, "B": 0.011, "C": 0.011, "D": 0.011, "E": 0.011,
}


def find_default_source_repo() -> Path:
    candidates = [
        REPO_ROOT.parent / "saguaro_arm_matching_env",
        REPO_ROOT.parent.parent / "saguaro-rl" / "saguaro_arm_matching_env",
    ]
    for c in candidates:
        if (c / "data" / "dataset.json").exists():
            return c
    raise FileNotFoundError(
        "Could not locate saguaro_arm_matching_env. Pass --source-repo explicitly."
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-repo", type=Path, default=None)
    p.add_argument("--only", action="append", default=None,
                   help="Restrict to these saguaro_ids (repeat to add more)")
    p.add_argument("--clean", action="store_true",
                   help="Wipe tasks/ before regenerating")
    return p.parse_args()


def derive_plot(sid: str) -> str:
    return sid.split("-", 1)[0]


def pick_sheet(sid: str, year: int, sheet_map: dict, hand_redacted_dir: Path) -> tuple[Path | None, str]:
    plot = derive_plot(sid)
    for fname in sheet_map.get(sid, []):
        if f"_{year}_" in fname:
            p = hand_redacted_dir / plot / fname
            if p.exists():
                return p, "hand"
    return None, "missing"


def fallback_v1_sheet(sid: str, year: int, source_repo: Path) -> Path | None:
    p = source_repo / "data" / "assets" / "datasheets" / f"{sid}_{year}.png"
    return p if p.exists() else None


INSTRUCTION_TEMPLATE = """\
Read `/workspace/brief.md` for the full task statement, input inventory,
output schema, and scoring rules.

In short: this saguaro has been measured in two surveys (one 2023, one
2026). Hand-redacted volunteer field forms are in `/workspace/datasheets/`
under **opaque filenames** (sheet_A.png, sheet_B.png — you must read the
date header to determine which is which year). Field photos are in
`/workspace/photos/` under opaque filenames (photo_001.jpg, ...).

Produce the cleaned, cross-year-matched table as a JSON list at
`/workspace/submission.json`. Per-cell scoring with field-typed tolerances
(±1° on direction, ±0.011 m on A/B/C/D/E, word-set Jaccard ≥0.5 on note).

Difficulty: **{difficulty}** ({split} split).
"""


def write_task(sid: str, v1_record: dict, v2_record: dict, source_repo: Path,
               sheet_map: dict, hand_redacted_dir: Path) -> dict:
    task_dir = TASKS_DIR / sid
    if task_dir.exists():
        shutil.rmtree(task_dir)
    (task_dir / "assets" / "datasheets").mkdir(parents=True)
    (task_dir / "assets" / "photos").mkdir(parents=True)
    (task_dir / "grade").mkdir()
    (task_dir / "environment").mkdir()
    (task_dir / "tests").mkdir()

    # Deterministic per-saguaro seed so the opaque shuffle is reproducible.
    rng = random.Random(f"saguaro-bench-curation::{sid}")

    # ---- Datasheets (prefer hand-redacted v2, fall back to v1 auto) --------
    # Bundle both years' sheets but rename to opaque sheet_A.png / sheet_B.png.
    raw_sheets: list[tuple[int, Path, str]] = []
    redaction_status: dict[int, str] = {}
    for year in (2023, 2026):
        src, status = pick_sheet(sid, year, sheet_map, hand_redacted_dir)
        if src is None:
            fb = fallback_v1_sheet(sid, year, source_repo)
            if fb is None:
                raise SystemExit(f"FATAL: no sheet for {sid} {year}")
            src, status = fb, "auto"
        raw_sheets.append((year, src, status))
        redaction_status[year] = status

    # Shuffle so opaque label A/B doesn't correlate with year.
    shuffled = list(raw_sheets)
    rng.shuffle(shuffled)
    sheet_opaque_map: dict[str, dict] = {}
    for letter, (year, src, status) in zip(("A", "B"), shuffled):
        dest_name = f"sheet_{letter}.png"
        shutil.copyfile(src, task_dir / "assets" / "datasheets" / dest_name)
        sheet_opaque_map[dest_name] = {
            "true_year": year,
            "source_file": src.name,
            "redaction_status": status,
        }

    # ---- Photos: opaque interleaved naming --------------------------------
    raw_photos: list[Path] = []
    for year in (2023, 2026):
        for ph in v1_record["assets"].get(f"photos_{year}", []):
            src = source_repo / ph["path"]
            if not src.exists():
                src = Path(ph["path"])
            if not src.exists():
                raise SystemExit(f"FATAL: missing photo {ph['path']} for {sid}")
            raw_photos.append((year, src))
    rng.shuffle(raw_photos)
    photo_opaque_map: dict[str, dict] = {}
    for i, (year, src) in enumerate(raw_photos, start=1):
        dest_name = f"photo_{i:03d}.jpg"
        shutil.copyfile(src, task_dir / "assets" / "photos" / dest_name)
        photo_opaque_map[dest_name] = {
            "true_year": year,
            "source_file": src.name,
        }

    # ---- brief.md ----------------------------------------------------------
    # Compute per-year canonical arms (sorted) from v2 truth_rows, EXCLUDING
    # _excluded rows from the visible schedule (they're skipped by scoring
    # and the agent shouldn't know which arms are excluded).
    arms_by_year: dict[int, list[str]] = {}
    n_excluded = 0
    for tr in v2_record["truth_rows"]:
        if tr.get("_excluded"):
            n_excluded += 1
            continue
        arms_by_year.setdefault(int(tr["year"]), []).append(str(tr["arm"]))
    for y in arms_by_year:
        # Numeric-aware sort.
        def _k(a):
            try:
                return (0, int(a))
            except ValueError:
                return (1, a)
        arms_by_year[y].sort(key=_k)

    (task_dir / "brief.md").write_text(build_brief(
        saguaro_id=sid,
        n_sheets=2,
        n_photos=len(raw_photos),
        canonical_arms_per_year=arms_by_year,
        n_excluded=n_excluded,
    ))

    # ---- instruction.md ---------------------------------------------------
    diff = v1_record["ground_truth"].get("difficulty", "unknown")
    (task_dir / "instruction.md").write_text(INSTRUCTION_TEMPLATE.format(
        difficulty=diff,
        split=v1_record.get("split", "unknown"),
    ))

    # ---- grade/truth.json + grade/score.py --------------------------------
    truth = {
        "saguaro_id": sid,
        "scored_fields": SCORED_FIELDS,
        "tolerances": TOLERANCES,
        "truth_rows": v2_record["truth_rows"],
        # Opaque-map is kept here for audit (it's in /grade so the agent can't see it).
        "_opaque_sheet_map": sheet_opaque_map,
        "_opaque_photo_map": photo_opaque_map,
    }
    (task_dir / "grade" / "truth.json").write_text(json.dumps(truth, indent=2, default=str))
    shutil.copyfile(LIB_DIR / "score.py", task_dir / "grade" / "score.py")

    # ---- task.toml --------------------------------------------------------
    redaction_tag = "hand" if all(s == "hand" for s in redaction_status.values()) else (
        "mixed" if "hand" in redaction_status.values() else "auto"
    )
    n_rows_scored = sum(1 for tr in v2_record["truth_rows"] if not tr.get("_excluded"))
    n_notes_overridden = sum(
        1 for tr in v2_record["truth_rows"]
        if not tr.get("_excluded") and (
            isinstance(tr.get("note"), list) or (isinstance(tr.get("note"), str) and tr["note"])
        )
    )
    task_toml = f"""schema_version = "1.1"
artifacts = []

[task]
name = "saguarobench-curation/{sid}"
description = "Curate the full cross-year arm-measurement table for saguaro {sid} on plot 41B. Difficulty: {diff}."
authors = ["Ty Pham-Swann"]
keywords = ["multimodal", "vlm", "saguaro", "curation", "digitization", "saguaro-bench"]

[metadata]
ext_id = "saguarobench-curation-{sid}"
task_id = "{sid}"
display_title = "Curate {sid}"
display_description = "Read hand-redacted volunteer field forms + field photos for saguaro {sid} (2023 and 2026), match arms across years, produce the cleaned canonical-arm table. Difficulty: {diff}."
category = "multimodal-curation"
language = "english"
repository_url = "https://github.com/typhamswann/saguaro-bench"
plot = "{derive_plot(sid)}"
split = "{v1_record.get('split', 'unknown')}"
difficulty = "{diff}"
n_arms_2023 = {sum(1 for tr in v2_record['truth_rows'] if tr['year']==2023 and not tr.get('_excluded'))}
n_arms_2026 = {sum(1 for tr in v2_record['truth_rows'] if tr['year']==2026 and not tr.get('_excluded'))}
n_truth_rows_scored = {n_rows_scored}
n_truth_rows_excluded = {n_excluded}
n_notes_with_override = {n_notes_overridden}
n_photos = {len(raw_photos)}
redaction_status_2023 = "{redaction_status[2023]}"
redaction_status_2026 = "{redaction_status[2026]}"
redaction_status = "{redaction_tag}"

[verifier]
timeout_sec = 300.0
user = "root"

[verifier.env]

[agent]
timeout_sec = 1800.0
user = "agent"

[environment]
build_timeout_sec = 600.0
docker_image = "saguaro-bench-task:1.0"
os = "linux"
cpus = 1
memory_mb = 2048
storage_mb = 2048
gpus = 0
allow_internet = false
mcp_servers = []

[environment.env]

[solution]

[solution.env]
"""
    (task_dir / "task.toml").write_text(task_toml)

    # ---- environment/Dockerfile -------------------------------------------
    dockerfile = """FROM saguaro-bench-base:1.0

# Agent-visible workspace. Assets are baked in under OPAQUE filenames so the
# agent can read them with its standard file-read primitive but can't tell
# which sheet is which year (or which photo is which year) from the path.
RUN mkdir -p /workspace/datasheets /workspace/photos

COPY assets/datasheets/  /workspace/datasheets/
COPY assets/photos/      /workspace/photos/
COPY brief.md instruction.md /workspace/

# Verifier-only data: ground truth + scorer + opaque->true-year map.
# Root-owned, mode 0700 so the agent user cannot read it.
COPY grade/ /grade/
RUN chmod 700 /grade && chmod 600 /grade/truth.json && chmod 700 /grade/score.py

# Create the agent user and give it the workspace.
RUN useradd -m -s /bin/bash agent && chown -R agent:agent /workspace

USER agent
WORKDIR /workspace
CMD ["/bin/bash"]
"""
    (task_dir / "environment" / "Dockerfile").write_text(dockerfile)

    # ---- tests/test.sh ----------------------------------------------------
    test_sh = f"""#!/usr/bin/env bash
# Harbor verifier — runs as root (per task.toml [verifier].user).
# Reads the agent's /workspace/submission.json, scores per-cell against
# /grade/truth.json using field-typed tolerances, writes
# /logs/verifier/reward.{{json,txt}}.
#
# Always exit 0 — the reward is the signal, not the exit code (mirrors deep-swe
# and wanderbench).
set -euo pipefail

LOG_PFX="[verifier]"

mkdir -p /logs/verifier /logs/agent /logs/artifacts

echo "${{LOG_PFX}} scoring saguaro-bench (curation) task {sid}"

python3 /grade/score.py /workspace/submission.json /grade/truth.json \\
    > /logs/verifier/reward.json

jq -r '.cell_accuracy_reward' /logs/verifier/reward.json > /logs/verifier/reward.txt

REWARD=$(cat /logs/verifier/reward.txt)
F1=$(jq -r '.row_f1 // empty' /logs/verifier/reward.json)
MISSING=$(jq -r '.rows_missing // empty' /logs/verifier/reward.json)
EXTRA=$(jq -r '.rows_extra // empty' /logs/verifier/reward.json)
ERR=$(jq -r '.structural_error // empty' /logs/verifier/reward.json)

echo "${{LOG_PFX}} reward=${{REWARD}} row_f1=${{F1}} missing=${{MISSING}} extra=${{EXTRA}}${{ERR:+ structural_error=$ERR}}"

# Stash the submission (if present) into /logs/artifacts for the trajectory viewer.
if [[ -f /workspace/submission.json ]]; then
    cp /workspace/submission.json /logs/artifacts/submission.json
fi

exit 0
"""
    test_path = task_dir / "tests" / "test.sh"
    test_path.write_text(test_sh)
    test_path.chmod(0o755)

    return {
        "saguaro_id": sid,
        "split": v1_record.get("split"),
        "difficulty": diff,
        "n_truth_rows_scored": n_rows_scored,
        "n_truth_rows_excluded": n_excluded,
        "n_notes_with_override": n_notes_overridden,
        "n_photos": len(raw_photos),
        "redaction_status_2023": redaction_status[2023],
        "redaction_status_2026": redaction_status[2026],
    }


def main() -> int:
    args = parse_args()
    source_repo: Path = args.source_repo or find_default_source_repo()

    v1 = json.loads((source_repo / "data" / "dataset.json").read_text())
    v2 = json.loads((source_repo / "data" / "curation_dataset_v2.json").read_text())
    v2_idx = {r["saguaro_id"]: r for r in v2}
    sheet_map = json.loads(
        (source_repo / "data" / "curation_workdir_v2" / "saguaro_sheet_map.json").read_text()
    )
    hand_redacted_dir = source_repo / "data" / "assets" / "datasheets_v2_hand_redacted"

    if args.clean and TASKS_DIR.exists():
        shutil.rmtree(TASKS_DIR)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []
    wanted = args.only
    for v1_record in v1:
        sid = v1_record["saguaro_id"]
        if wanted is not None and sid not in wanted:
            continue
        v2_record = v2_idx.get(sid)
        if v2_record is None:
            print(f"  ✗ {sid}: missing in v2 dataset", file=sys.stderr)
            continue
        s = write_task(sid, v1_record, v2_record, source_repo, sheet_map, hand_redacted_dir)
        summary.append(s)
        print(f"  ✓ {sid:10s} split={s['split']:5s} diff={s['difficulty']:7s} "
              f"rows={s['n_truth_rows_scored']:>2}(+{s['n_truth_rows_excluded']} excl) "
              f"notes_overridden={s['n_notes_with_override']:>2} "
              f"photos={s['n_photos']:>2} "
              f"redaction={s['redaction_status_2023']}/{s['redaction_status_2026']}")

    (TASKS_DIR / "INDEX.json").write_text(json.dumps(summary, indent=2))
    n_hand = sum(1 for s in summary
                 if s["redaction_status_2023"] == "hand" and s["redaction_status_2026"] == "hand")
    print()
    print(f"  built {len(summary)} task(s) under {TASKS_DIR}")
    print(f"  fully-hand-redacted: {n_hand} / {len(summary)}")
    print(f"  total scored truth rows: {sum(s['n_truth_rows_scored'] for s in summary)}")
    print(f"  total notes overridden:  {sum(s['n_notes_with_override'] for s in summary)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
