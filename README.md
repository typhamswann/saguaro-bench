# [SaguaroBench](https://github.com/typhamswann/saguaro-bench)

SaguaroBench is a benchmark for measuring multimodal language models on a
real-world citizen-science **curation** task: reading hand-written volunteer
field forms and field photos, then producing the cleaned, cross-year-matched
arm-measurement table that the human curator would have produced.

Volunteers measure saguaro cacti every few years on the same plot. They
record per-arm measurements (direction, base height, tip height, arm
length, recorder notes) on paper field-forms, numbering each saguaro's
arms independently each visit. A curator then takes both years' raw
sheets + photos and produces one cleaned spreadsheet: matched arms
across years, canonical arm numbers, every measurement and note
re-keyed into the canonical schema.

This benchmark turns that curator workflow into a 25-task evaluation.
Each task is one saguaro from plot 41B: the agent gets two hand-redacted
volunteer field forms and a handful of field photos with **opaque
filenames** (the agent doesn't know which sheet is 2023 vs 2026 from the
path — it has to read the date header), and must produce the full
canonical-arm table as `submission.json`. **Scoring is per-cell with
field-typed tolerances**, so every measurement and every recorder-note
contributes independently to the reward.

## Task format

SaguaroBench tasks use the [Harbor](https://www.harborframework.com/docs/tasks)
task format. The agent sees a normal Unix workspace and writes a JSON
file to a known path — there is no benchmark-specific CLI to learn:

```text
task.toml          Metadata: saguaro_id, plot, split, difficulty,
                   redaction status, row/photo counts, resource limits
instruction.md     Short pointer at brief.md
brief.md           Task statement + opaque asset inventory + output
                   schema + per-year canonical-arm count
assets/            Bundled into /workspace/ at build time, with OPAQUE names
  datasheets/      sheet_A.png, sheet_B.png — one is 2023, one is 2026
  photos/          photo_001.jpg, photo_002.jpg, ... — mixed years
grade/             Verifier-only — root-owned + mode 0700 inside the image
  truth.json       Ground truth rows (with v2 note overrides + _excluded
                   rows) + scoring schema (scored_fields, tolerances)
  score.py         Stdlib-only per-cell scorer
environment/       Dockerfile that bakes assets into /workspace, grade/
                   into /grade, and gives the workspace to an `agent` user
tests/test.sh      Verifier entry point — runs `python3 /grade/score.py …`
                   and writes /logs/verifier/reward.{json,txt}
```

### The agent's contract

Inside the container the agent works in `/workspace/`:

```
/workspace/
├── instruction.md              short pointer to brief.md
├── brief.md                    full task statement + I/O contract
├── datasheets/
│   ├── sheet_A.png             hand-redacted volunteer field form (year unknown from filename)
│   └── sheet_B.png             hand-redacted volunteer field form (year unknown from filename)
├── photos/
│   ├── photo_001.jpg           field photo, year unknown
│   ├── photo_002.jpg           ...
│   └── photo_NNN.jpg
└── submission.json             ← agent writes its full cleaned table here
```

The agent uses whatever file-read primitive it already has (Claude Code's
`Read`, Codex's image-aware `read`, etc.) to look at the PNG/JPG assets,
and whatever write primitive (`Write`, `apply_patch`, `bash: echo ... >
submission.json`) to produce `submission.json`. There is no custom CLI,
no view buffer, no tool-call indirection. This matches
[deep-swe](https://github.com/datacurve-ai/deep-swe)'s shape: workspace
+ instruction + write your answer to a known path.

`submission.json` must be a JSON list of row objects. Each row:

```json
{
  "saguaro_id": "41B-13",
  "year": 2023,
  "arm": "1",
  "direction": 360,
  "A": 1.89,
  "B": 0.98,
  "C": 2.04,
  "D": 0.98,
  "E": 0.2,
  "note": ""
}
```

Output one row per `(year, canonical_arm)`. The brief lists which
canonical arm numbers exist per year for this saguaro — but it does NOT
say which paper-arm in either year corresponds to which canonical arm.
That's what the agent has to figure out by matching arm direction +
measurements + photos across the two sheets.

## Scoring

Per-cell match against ground truth, keyed by `(saguaro_id, year, arm)`,
with field-typed tolerances:

| field | match rule |
|---|---|
| `direction` | numeric, ±1.0° |
| `A`, `B`, `C`, `D`, `E` | numeric, ±0.011 m |
| `note` | **list-of-acceptable** OR Jaccard word-set ≥0.5 (empty=empty) |
| `saguaro_id` | normalized string equality |

Missing rows score 0 across all their cells. **Extra (hallucinated)
rows** incur a 5% penalty each, capped at 50%. **Excluded rows** (truth
rows where the paper is genuinely ambiguous or known-wrong) are skipped
entirely by scoring — neither the truth cells nor an agent submission at
that key count.

Final reward: `cell_accuracy_reward = max(0, correct/total - extra_penalty)` in `[0, 1]`.

Per-task `reward.json`:

```json
{
  "cell_accuracy_reward":  0.952,
  "base_cell_accuracy":    0.967,
  "extra_row_penalty":     0.015,
  "row_f1":                0.978,
  "rows_truth":            15,
  "rows_pred_scored":      15,
  "rows_matched":          15,
  "rows_missing":          0,
  "rows_extra":            0,
  "rows_excluded":         0,
  "per_field_accuracy": {
    "saguaro_id": 1.0, "direction": 1.0,
    "A": 1.0, "B": 1.0, "C": 0.933, "D": 1.0, "E": 1.0,
    "note": 0.867
  },
  "saguaro_id": "41B-13"
}
```

`row_f1` and `per_field_accuracy` are diagnostics; `cell_accuracy_reward`
is the primary metric.

### Why notes are list-of-acceptable

During curation QA the user surfaced cases where the recorder note is
inherently ambiguous: e.g. "5 nubbins" vs "5 nubbins!" (same content,
the recorder wrote the second on one sheet and the first on another),
"DOUBLE / JOINED @ THE BASE BUT SEPARATED" written across two arm rows
where it could be attributed to either, "Don checked on 2/9/26 + this
should be 3.045" QA-overlay that survived hand-redaction. The truth
file stores all defensible note phrasings as a list; a match against
any list member counts the cell as correct. See the upstream paper-
faithful overrides in `data/curation_dataset_v2.json` for the full
audit trail.

## Quickstart

### Path A — Harbor / Pier (canonical)

Any [Harbor](https://www.harborframework.com/)-compatible runtime works.
Build the base image once (it's just `python:3.11-slim` + `jq`), then run:

```bash
git clone https://github.com/typhamswann/saguaro-bench
cd saguaro-bench
docker build -t saguaro-bench-base:1.0 base/        # build once
harbor run -p tasks --agent <agent> --model <model>
# or, using pier (DeepSWE's runner):
pier run -p tasks --agent claude-code --model anthropic/claude-opus-4-7
```

Each task image is `FROM saguaro-bench-base:1.0` and bakes in its own
assets + ground truth. The verifier emits `cell_accuracy_reward ∈ [0,
1]` per task; Harbor / Pier collate per-task rewards into a leaderboard.

### Path B — OpenRouter harness (six models pre-wired)

For quick iteration across arbitrary OpenRouter models — no Docker
required at runtime. See `harness/README.md` for full docs.

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
python harness/run.py --models all --max-turns 30 --cost-cap 20
```

Results land at `runs/<timestamp>/<model_tag>.json` with per-task
`cell_accuracy_reward`, `row_f1`, per-field accuracy breakdown, cost in
USD, served provider, and the list of images the agent chose to look at.

### Subsets and single tasks

```bash
harbor run -p saguaro-bench/tasks --agent <agent>             # all 25
harbor run -p saguaro-bench/tasks --agent <agent> --n-tasks 4 # first 4
harbor run -p saguaro-bench/tasks/41B-13 --agent <agent>      # one task
```

### Sanity-check a single task without an agent

```bash
docker build -t sab-task -f tasks/41B-13/environment/Dockerfile tasks/41B-13
docker run --rm sab-task bash -c '
  cat instruction.md brief.md
  ls datasheets photos
'
# Then grade a submission (root mode since /grade is locked):
docker run --rm --user root \
  -v "$PWD/tasks/41B-13/tests:/tests:ro" sab-task \
  bash -c '
    echo "[]" > /workspace/submission.json   # placeholder
    bash /tests/test.sh
    cat /logs/verifier/reward.json
  '
```

The image's `WORKDIR` is `/workspace` and the default user is `agent`.
`/grade/` is root-owned mode 0700 — only the verifier can read the
truth.

## Dataset

- **Plot:** 41B (Saguaro National Park, Arizona). The same plot was
  re-measured by volunteers in 2023 and 2026.
- **Saguaros:** 25, each appearing in both years. **237 scored truth
  rows** + **2 excluded rows** (genuinely ambiguous arm-4 geometry on
  saguaro 41B-06, both years — flagged in `data/curation_dataset_v2.json`).
- **Splits** (stratified by per-saguaro difficulty): 17 train / 4 val / 4
  test. Preserved in each `task.toml` under `metadata.split`.
- **Difficulty distribution:** 1 easy / 17 medium / 7 hard.
- **Sheets:** 50 paper data-sheet scans (2 per saguaro), hand-redacted
  to remove the curator's marginal canonical-arm renumberings,
  "Yes/No" stamps, photo annotations, and arrow overlays. 24/25
  saguaros are fully hand-redacted on both years; 41B-22's 2026 sheet
  falls back to an auto-redacted version (flagged in its `task.toml`
  as `metadata.redaction_status_2026 = "auto"`).
- **Photos:** 209 field photos (0–13 per saguaro). 2 saguaros have no
  photos (volunteer didn't take any); the brief flags this.
- **Note overrides:** 14 truth rows have paper-faithful note overrides
  surfaced during curation QA (list-of-acceptable phrasings to handle
  recorder variation, ambiguous placement, etc.).

Ground truth is bundled in each task's `grade/truth.json` and is unreadable
by the agent user inside the container.

### Why hand-redacted?

An earlier auto-redacted version of this benchmark saw scores at the
ceiling for several frontier models on the matching task. Inspection of
the sheets revealed that auto-redaction sometimes left visible fragments
of the curator's canonical-arm renumbering, giving a partial shortcut.
The hand-redacted set used here is a careful pass over each sheet by
the curator, removing every marginal canonical number and stamp that
leaked the answer. The curation task can only be solved by actually
reading the volunteer's handwriting.

### Why opaque filenames?

The real curator workflow is: a pile of scanned sheets and photos lands
in the queue with no year tagging, and the curator has to read each one
to figure out what's in it. Opaque names preserve that. They also
prevent a benchmark shortcut where an agent infers which sheet is 2026
from the filename instead of from the sheet's date header.

## Repo layout

```
saguaro-bench/
├── base/
│   └── Dockerfile        saguaro-bench-base:1.0 — python:3.11-slim + jq
├── scripts/
│   ├── build_tasks.py    Regenerate tasks/ from saguaro_arm_matching_env
│   └── lib/
│       ├── brief.py      build_brief — renders brief.md per task
│       └── score.py      Canonical per-cell scorer (copied verbatim per task)
└── tasks/
    ├── INDEX.json        Summary across all tasks
    └── <saguaro_id>/     One per saguaro (25 total)
        ├── instruction.md
        ├── brief.md
        ├── task.toml
        ├── assets/{datasheets,photos}/   opaque filenames
        ├── grade/{truth.json, score.py}  root-locked
        ├── environment/Dockerfile
        └── tests/test.sh
```

## Version history

- **v0.3.0** (current) — Full curation task. Agent reads opaque-named
  hand-redacted sheets + opaque-named photos, produces the cleaned
  cross-year arm-measurement table. Per-cell scoring with field-typed
  tolerances (direction ±1°, A/B/C/D/E ±0.011 m, note list-of-acceptable
  OR Jaccard ≥0.5). Truth pulled from `data/curation_dataset_v2.json`
  with all paper-faithful note overrides and excluded rows.
- **v0.2.0** — DeepSWE-style files-on-disk contract for the
  **arm-matching** task. Agent submits a JSON mapping of 2026 arms to
  2023 arms or "new". Tagged at
  [v0.2.0](https://github.com/typhamswann/saguaro-bench/tree/v0.2.0).
- **v0.1.0** — WanderBench-style narrow-tool surface (`sab harbor-step`
  CLI), arm-matching task. Tagged at
  [v0.1.0](https://github.com/typhamswann/saguaro-bench/tree/v0.1.0).

## Local development (no Docker required)

The scorer is stdlib-only Python and tasks are just files, so you can
test the scoring path against any task without rebuilding the image:

```bash
# Build a perfect submission from the truth (sanity check):
python3 - <<'PY'
import json
t = json.load(open('tasks/41B-13/grade/truth.json'))
rows = []
for r in t['truth_rows']:
    if r.get('_excluded'): continue
    rr = {k: r[k] for k in ('saguaro_id','year','arm','direction','A','B','C','D','E')}
    n = r.get('note', '')
    if isinstance(n, list): n = next((x for x in n if x), '')
    rr['note'] = n
    rows.append(rr)
open('/tmp/sub.json','w').write(json.dumps(rows))
PY
python3 tasks/41B-13/grade/score.py /tmp/sub.json tasks/41B-13/grade/truth.json
# -> {"cell_accuracy_reward": 1.0, ..., "row_f1": 1.0, ...}
```

## Full curation pipeline

This 25-task slice is the frozen benchmark. The full saguaro-curation
RL environment covers all 7 plots and 217 saguaros with the same Harbor
+ DeepSWE-style packaging. Available under separate terms.

For access, contact phamswannty@gmail.com.

## License

MIT.
