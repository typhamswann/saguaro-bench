# [SaguaroBench](https://github.com/typhamswann/saguaro-bench)

SaguaroBench is a benchmark for measuring multimodal language models on a
real-world citizen-science data-cleaning task: matching saguaro cactus
arm measurements across two survey years (2023 and 2026) on the same
plant.

Volunteers measure plots of saguaros every few years, numbering each
saguaro's arms independently each visit. Arm 3 on a saguaro in 2026 is
NOT necessarily the same physical arm as arm 3 in 2023 — the volunteer
re-counted from "north-most, then clockwise" and arms grow, split, die,
or appear in between. A human curator then hand-matches arms across
years so the team can compute per-arm growth.

This benchmark turns that matching task into a 25-task evaluation. Each
task is one saguaro from plot 41B: the agent receives the digitized arm
rows for both years and decides, for every 2026 arm, which 2023 arm is
the same physical arm — or `"new"` if the arm emerged since 2023. The
agent inspects hand-redacted paper datasheets (volunteer field forms
with the curator's marginal arm-number renumberings blacked out) and
field photos from each survey.

## Task format

SaguaroBench tasks use the [Harbor](https://www.harborframework.com/docs/tasks)
task format. The agent sees a normal Unix workspace and writes a JSON
file to a known path — there is no benchmark-specific CLI to learn:

```text
task.toml          Metadata: saguaro_id, plot, split, difficulty,
                   redaction status, resource limits
instruction.md     The prompt the agent sees (a copy is at /workspace/)
brief.md           Digitized arm rows + photo inventory (also at /workspace/)
assets/            Bundled into /workspace/ at build time
  datasheets/2023.png, 2026.png      hand-redacted volunteer field forms
  photos/{2023,2026}/photo_<N>.jpg
grade/             Verifier-only — root-owned + mode 0700 inside the image
  truth.json       Ground truth mapping + valid arm sets
  score.py         Stdlib-only scorer (structural validation + exact match)
environment/       Dockerfile that bakes assets into /workspace, grade/
                   into /grade, and gives the workspace to an `agent` user
tests/test.sh      Verifier entry point — runs `python3 /grade/score.py …`
                   and writes /logs/verifier/reward.{json,txt}
```

### The agent's contract

Inside the container the agent works in `/workspace/`:

```
/workspace/
├── instruction.md              the prompt
├── brief.md                    digitized arm rows + photo inventory
├── datasheets/
│   ├── 2023.png                hand-redacted volunteer field form
│   └── 2026.png
├── photos/
│   ├── 2023/photo_<N>.jpg      4–9 per saguaro per year
│   └── 2026/photo_<N>.jpg
└── submission.json             ← agent writes its final mapping here
```

The agent uses whatever file-read primitive it already has (Claude Code's
`Read`, Codex's image-attached `read`, etc.) to look at the PNG/JPG
assets, and whatever write primitive (`Write`, `apply_patch`, `bash:
echo ... > submission.json`) to produce `submission.json`. There is no
custom CLI, no view buffer, no tool-call indirection. This matches
[deep-swe](https://github.com/datacurve-ai/deep-swe)'s shape: workspace
+ instruction + write your answer to a known path.

`submission.json` must be a JSON object mapping every 2026 arm number
(string) to either a 2023 arm number (string) or the literal `"new"`.
The mapping must be a function — no two 2026 arms may map to the same
non-`"new"` 2023 arm.

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

### Path B — OpenRouter harness (six models pre-wired)

For quick iteration across arbitrary OpenRouter models — no Docker
required at runtime. See `harness/README.md` for full docs.

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
python harness/run.py --models all --max-turns 14 --cost-cap 10
```

The harness ships with six models pre-configured in
`harness/models.json` (Gemini 3.5 Flash, Qwen 3.7 Plus,
Qwen 3.5-397B-A17B, GLM-5V Turbo, Kimi K2.6, Gemma 4 26B-A4B) — the
same set used in the WanderBench OpenRouter run, with matching
quantization pins, so cross-benchmark comparisons are apples-to-apples.

Results land at `runs/<timestamp>/<model_tag>.json` with per-task
`exact_mapping_reward`, `arm_pair_f1`, cost in USD, served provider,
and the list of images the agent chose to look at.

Each task image is `FROM saguaro-bench-base:1.0` and bakes in its own
assets + ground truth. The verifier emits `exact_mapping_reward ∈ {0.0,
1.0}` per task; Harbor / Pier collate per-task rewards into a
leaderboard summary.

To sanity-check a single task without a Harbor agent:

```bash
docker build -t sab-task -f tasks/41B-13/environment/Dockerfile tasks/41B-13
docker run --rm sab-task bash -c '
  cat instruction.md brief.md
  ls datasheets photos/2023 photos/2026
  # simulate an agent submission:
  echo "{\"10\":\"1\",\"1\":\"2\",\"6\":\"3\",\"7\":\"4\",\"8\":\"5\",\"2\":\"new\",\"3\":\"new\",\"4\":\"new\",\"5\":\"new\",\"9\":\"new\"}" \
    > submission.json
'

# Then grade (in a separate root-mode docker run since /grade is locked):
docker run --rm --user root -v "$PWD/tasks/41B-13/tests:/tests:ro" sab-task bash /tests/test.sh
cat /logs/verifier/reward.json  # inside the container
```

The image's `WORKDIR` is `/workspace` and the default user is `agent`,
so the agent's process can read every PNG/JPG immediately at startup.
`/grade/` is root-owned mode 0700 so the agent process cannot read the
truth — only the verifier (running as root) can.

### Subsets and single tasks

```bash
harbor run -p saguaro-bench/tasks --agent <agent>             # all 25
harbor run -p saguaro-bench/tasks --agent <agent> --n-tasks 4 # first 4
harbor run -p saguaro-bench/tasks/41B-13 --agent <agent>      # one task
```

## Scoring

A submission scores **1.0** only if it passes structural validation AND
exactly matches the curator's ground-truth mapping. A structurally-
broken submission scores **0.0** with `structural_error` populated so it
can be triaged separately from a structurally-valid wrong answer.

Per-task `reward.json`:

```json
{
  "exact_mapping_reward":  1.0,
  "arm_pair_f1":           0.95,
  "saguaro_id":            "41B-13"
}
```

`arm_pair_f1` is a continuous diagnostic over the SET of matched
`(2026_arm, 2023_arm)` pairs (treating `"new"` entries as no-match).
Useful for ranking partial-credit answers but NOT the primary reward.

### Structural checks

- Keys exactly match the 2026 arms in the brief.
- Values are either `"new"` or a 2023 arm id present in the brief.
- The mapping is a function — no two 2026 arms map to the same non-`"new"` 2023 arm.

The scorer also accepts a wrapped form `{"submission": "<json-string>"}`
in case an agent wraps its output that way.

## Dataset

- **Plot:** 41B (Saguaro National Park, Arizona). The same plot was
  re-measured by volunteers in 2023 and 2026.
- **Saguaros:** 25, each appearing in both years. 129 total
  arm-mapping decisions.
- **Splits** (stratified by per-saguaro difficulty): 17 train / 4 val / 4
  test. The split is preserved in each `task.toml` under
  `metadata.split` so leaderboards can report sub-scores per split.
- **Difficulty distribution:** 1 easy / 17 medium / 7 hard.
- **Sheets:** 50 paper data-sheet scans (2 per saguaro), hand-redacted
  to remove the curator's marginal canonical-arm renumberings. 24/25
  saguaros are fully hand-redacted on both years; 41B-22's 2026 sheet
  falls back to the auto-redacted version (flagged in its `task.toml`
  as `metadata.redaction_status_2026 = "auto"`).
- **Photos:** 209 field photos (4–9 per saguaro per year). 2 saguaros
  have no photos (volunteer didn't take any); the brief flags this.

Ground truth is bundled in each task's `grade/truth.json` and is unreadable
by the agent user inside the container.

### Why hand-redacted?

An earlier auto-redacted version of this benchmark saw scores at the
ceiling for several frontier models. Inspection of the sheets revealed
that auto-redaction sometimes left visible fragments of the curator's
canonical-arm renumbering, giving a partial shortcut around the matching
task. The hand-redacted set used here is a careful pass over each sheet
by the curator, removing every marginal canonical number, "Yes/No"
stamps, photo annotations, and arrow overlays that leaked the answer.

## Repo layout

```
saguaro-bench/
├── base/
│   └── Dockerfile        saguaro-bench-base:1.0 — python:3.11-slim + jq
├── scripts/
│   ├── build_tasks.py    Regenerate tasks/ from saguaro_arm_matching_env
│   └── lib/
│       ├── brief.py      build_brief — renders brief.md from a record
│       └── score.py      Canonical scorer (copied verbatim per task)
├── harness/
│   ├── run.py            OpenRouter driver: model loop + per-task scoring
│   ├── openrouter.py     Stdlib-only chat-completions client with cost tracking
│   ├── tools.py          list_dir / read_text / view_image / write_submission
│   ├── models.json       Six pre-wired OpenRouter models
│   └── README.md
└── tasks/
    ├── INDEX.json        Summary across all tasks
    └── <saguaro_id>/     One per saguaro (25 total)
        ├── instruction.md
        ├── brief.md
        ├── task.toml
        ├── assets/{datasheets,photos}/
        ├── grade/{truth.json, score.py}
        ├── environment/Dockerfile
        └── tests/test.sh
```

## Version history

- **v0.2.0** (current) — DeepSWE-style. Agent sees `/workspace/` as a
  normal Unix dir, writes its mapping to `submission.json`. No custom
  CLI. Drop-in for Claude Code / Codex / Gemini CLI / OpenHands /
  mini-swe-agent without any agent-side changes.
- **v0.1.0** — [tag](https://github.com/typhamswann/saguaro-bench/tree/v0.1.0)
  WanderBench-style. Narrow `sab harbor-step --tool ...` surface,
  vendored Python runtime under `base/pkg`, view buffers managed by the
  runtime. Recoverable for benchmarks that want the structured
  tool-call audit trail.

## Local development (no Docker required)

The scorer is stdlib-only Python and tasks are just files, so you can
test the scoring path against any task without rebuilding the image:

```bash
echo '{"10":"1","1":"2","6":"3","7":"4","8":"5","2":"new","3":"new","4":"new","5":"new","9":"new"}' \
  > /tmp/sub.json
python3 tasks/41B-13/grade/score.py /tmp/sub.json tasks/41B-13/grade/truth.json
# -> {"exact_mapping_reward": 1.0, "arm_pair_f1": 1.0, "saguaro_id": "41B-13"}
```

## Full curation pipeline

This repo is the frozen 25-task arm-matching benchmark slice. The full
saguaro-curation RL environment covers the rest of the citizen-science
workflow — digitizing the paper sheets, sanity-checking volunteer entries
against photos, and producing the matched cross-year table — across 7
plots and 217 saguaros with Harbor-compatible packaging. Available under
separate terms.

For access, contact phamswannty@gmail.com.

## License

MIT.
