# SaguaroBench OpenRouter harness

Drop-in driver for running SaguaroBench against any model OpenRouter
exposes — no Docker required at runtime. Mirrors the
[WanderBench harbor-driver shape](https://github.com/typhamswann/wanderbench-benchmark)
so cross-benchmark comparisons against those same six models are
apples-to-apples.

The agent works in a host-side `/workspace` (a temp dir per task) that
the harness sets up from `tasks/<sid>/assets/`. Scoring shells out to the
task's stdlib-only `grade/score.py` — no container build, no model SDK.

## Quickstart

```bash
# 1. Set your OpenRouter API key.
export OPENROUTER_API_KEY=sk-or-v1-...
# (or write the bare key to ~/.openrouter_key — same convention as wanderbench)

# 2. Run the six pre-configured models against all 25 tasks.
python harness/run.py --models all --max-turns 14 --cost-cap 10

# 3. Inspect the results.
ls runs/<timestamp>/
cat runs/<timestamp>/gemini35_flash.json | jq '.results[] | {sid: .saguaro_id, reward: .exact_mapping_reward}'
```

## Args

| Flag | Default | What it does |
|---|---|---|
| `--models` | `all` | Comma-sep model tags (see `harness/models.json`) or `all` |
| `--tasks` | `all` | Comma-sep saguaro IDs (e.g. `41B-01,41B-13`) or `all` |
| `--max-turns` | `14` | Max tool calls per task before forced termination |
| `--image-window` | `6` | Keep image attachments only on the most recent N user messages |
| `--cost-cap` | `None` | Abort a model once its running OpenRouter cost (USD) exceeds this |
| `--run-id` | timestamp | Run identifier; results land at `runs/<run-id>/` |
| `--registry` | `harness/models.json` | Model registry path |

## Protocol

Each task is one rollout per model. The harness installs a system prompt
describing the workspace + the four available tools, then loops:

1. Call OpenRouter `/chat/completions` with the current message history.
2. Parse the assistant reply as JSON `{"tool": "<name>", "args": {...}}`.
3. Dispatch to the host-side handler:
   - `list_dir` — directory listing under `/workspace`
   - `read_text` — read a text file (instruction.md, brief.md)
   - `view_image` — base64-encode an image; attached to the next user message
   - `write_submission` — write `/workspace/submission.json` and end the task
4. Append the tool result as a user message (text + optional image).
5. If `write_submission` was called, exit; otherwise, repeat.

When the loop ends (`write_submission`, `--max-turns` reached, 5 consecutive
parse failures, or an API error), the harness shells out to the task's
`grade/score.py` and records the result.

## Result format

`runs/<run-id>/<model_tag>.json`:

```json
{
  "model_tag": "gemini35_flash",
  "model_slug": "google/gemini-3.5-flash",
  "provider": null,
  "served_providers": ["Google"],
  "cost_usd": 0.3142,
  "calls": 247,
  "capped_cost": false,
  "results": [
    {
      "saguaro_id":            "41B-01",
      "model_tag":             "gemini35_flash",
      "model_slug":            "google/gemini-3.5-flash",
      "exact_mapping_reward":  1.0,
      "arm_pair_f1":           1.0,
      "structural_error":      null,
      "stop":                  "write_submission",
      "turns_taken":           7,
      "max_turns":             14,
      "images_viewed":         ["datasheets/2026.png", "datasheets/2023.png", "photos/2026/photo_1.jpg"],
      "cost_usd_running":      0.0149,
      "wall_time_sec":         18.4
    },
    ...
  ]
}
```

Results are flushed to disk after every task, so a SIGINT mid-run
doesn't lose work.

## Model registry

`harness/models.json` holds the OpenRouter slug + provider routing for
each tag. The six pre-configured models match the WanderBench run:

| Tag | OpenRouter slug | Provider pin |
|---|---|---|
| `gemini35_flash` | `google/gemini-3.5-flash` | (let OR pick) |
| `qwen37_plus` | `qwen/qwen3.7-plus` | (let OR pick) |
| `qwen35_397b` | `qwen/qwen3.5-397b-a17b` | fp8 |
| `glm5v_turbo` | `z-ai/glm-5v-turbo` | fp8 |
| `kimi_k26` | `moonshotai/kimi-k2.6` | fp8 |
| `gemma4_26b` | `google/gemma-4-26b-a4b` | (let OR pick) |

**Verify slugs** at https://openrouter.ai/models before running —
provider naming drifts between releases. The `gemma4_26b` slug
specifically is a guess; cross-check it.

## Notes / caveats

- The tool-call protocol is JSON-only (one tool per turn) rather than
  provider-native function calling, so weaker/less-tuned models are
  more likely to drop into prose. The harness gives the model 5
  consecutive prose replies before giving up (`stop = no_tool_call_x5`).
- Images are sent as base64 data URLs in the user message — that's the
  most portable across OpenRouter providers. The `--image-window` flag
  caps how many images stay attached at once.
- This is intentionally NOT using the Harbor / Pier runner. Those are
  the canonical evaluators for the published leaderboard. This harness
  is for fast iteration + arbitrary-model exploration on a host
  machine; the results are directly comparable in absolute terms but
  use a slightly different loop shape (host-side tools vs. container-
  side `harbor-step`).
