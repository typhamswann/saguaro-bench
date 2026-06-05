"""OpenRouter harness for SaguaroBench — runs one or more models against
one or more tasks, scoring via each task's stdlib-only grade/score.py.

Designed to mirror the wanderbench harbor_driver.py shape so apples-to-
apples cross-benchmark comparisons are easy:

- JSON-only tool-call protocol (one tool per turn), so every model OpenRouter
  routes to is supported without provider-specific function-calling glue.
- Sliding image-window cap so old screenshots don't bloat the context.
- Per-model USD cost cap as a backstop.
- Results written to runs/<run_id>/<model_tag>.json incrementally so a kill
  mid-run leaves resumable state.

Workspace contract per task (matches DeepSWE-style v0.2 task images):

    /workspace/
        instruction.md
        brief.md
        datasheets/{2023,2026}.png
        photos/{2023,2026}/photo_<N>.jpg
        submission.json    ← the agent writes this; score.py reads it

We don't build the docker images at runtime — the assets already live on
host at tasks/<sid>/assets/, and grade/score.py is stdlib-only. The host
runs the loop and shells out to score.py for grading.

Usage:
    export OPENROUTER_API_KEY=sk-or-v1-...
    python harness/run.py --models gemini35_flash,qwen37_plus --max-turns 12
    python harness/run.py --models all --tasks 41B-01,41B-13 --max-turns 12 \
                          --cost-cap 8.00 --run-id pilot_2026-06
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from openrouter import OpenRouterClient
from tools import DISPATCH, parse_tool_call, ALLOWED


# -----------------------------------------------------------------------------
# System prompt — tool list + protocol. The model sees this once.
# -----------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are matching saguaro cactus arm measurements across two citizen-science survey years (2023 and 2026) on the same plant.

Volunteers numbered the arms independently each year, so the 2026 arm numbers do NOT necessarily correspond to the 2023 ones. For each 2026 arm, decide which 2023 arm is the same physical arm, or "new" if the arm appeared since 2023.

Measurement columns (per arm, per year):
- direction: compass bearing from saguaro center out to the arm, in degrees (0=N, 90=E, 180=S, 270=W).
- A: height in meters from the ground to where the arm emerges from the main stem.
- B: height in meters from the ground to a 1 m datum mark on the stem near where A was measured.
- C: height from the ground to the tip of the arm.
- D: height from the ground to a 1 m datum mark on the stem near where C was measured.
- E: horizontal distance in meters from the main stem to the arm tip.

Biological constraints: saguaro arms grow slowly. They rarely shrink. New arms emerge between surveys; existing arms only rarely disappear.

# Your environment

Your workspace is /workspace/. The relevant files are:
- /workspace/instruction.md         the task statement
- /workspace/brief.md               digitized arm rows + photo inventory
- /workspace/datasheets/2023.png    hand-redacted volunteer field form
- /workspace/datasheets/2026.png    hand-redacted volunteer field form
- /workspace/photos/2023/photo_<N>.jpg
- /workspace/photos/2026/photo_<N>.jpg

# Protocol

Emit EXACTLY ONE tool call per turn as JSON and nothing else:

    {"tool": "<name>", "args": {...}}

Tools:
- list_dir({"path": "<dir>"})
    Return a directory listing.
- read_text({"path": "<file>"})
    Return the contents of a text file (e.g. instruction.md, brief.md).
- view_image({"path": "<file>"})
    Stage an image so it appears (base64-attached) in the NEXT user message.
- write_submission({"content": "<json string>"})
    Write your final mapping as a JSON STRING to /workspace/submission.json
    and end the task. `content` must be a JSON object mapping every 2026 arm
    number to a 2023 arm number (as strings) or the literal "new". The
    mapping must be a function — no two 2026 arms may map to the same
    non-"new" 2023 arm.

Recommended workflow:
1. read_text /workspace/instruction.md
2. read_text /workspace/brief.md
3. view_image /workspace/datasheets/2026.png
4. view_image /workspace/datasheets/2023.png
5. Examine photos as needed via view_image (the brief lists how many are
   available; use 1-based indexing).
6. write_submission(...) to finish.

Output ONLY the JSON tool call. No prose before or after."""


INITIAL_USER = """\
Start matching saguaro {sid}. The full task statement is at
/workspace/instruction.md and the per-task brief (arm rows + photo
inventory) is at /workspace/brief.md. Emit your first tool call."""


# -----------------------------------------------------------------------------
# Per-task driver
# -----------------------------------------------------------------------------

def setup_workspace(task_dir: Path) -> Path:
    """Copy a task's bundled assets into a temp host dir that becomes
    /workspace from the agent's perspective. Returns the host workspace
    root.
    """
    ws = Path(tempfile.mkdtemp(prefix="sb_ws_"))
    # Mirror what environment/Dockerfile does at build time.
    shutil.copyfile(task_dir / "instruction.md", ws / "instruction.md")
    shutil.copyfile(task_dir / "brief.md",       ws / "brief.md")
    (ws / "datasheets").mkdir()
    shutil.copyfile(task_dir / "assets" / "datasheets" / "2023.png", ws / "datasheets" / "2023.png")
    shutil.copyfile(task_dir / "assets" / "datasheets" / "2026.png", ws / "datasheets" / "2026.png")
    (ws / "photos" / "2023").mkdir(parents=True)
    (ws / "photos" / "2026").mkdir(parents=True)
    for year in (2023, 2026):
        src = task_dir / "assets" / "photos" / str(year)
        if not src.exists():
            continue
        for f in sorted(src.iterdir()):
            shutil.copyfile(f, ws / "photos" / str(year) / f.name)
    return ws


def strip_old_images(messages: list[dict], window: int) -> None:
    """Keep image attachments only on the most recent `window` user messages.
    Older ones get their image_url parts replaced with a placeholder text.
    """
    user_idx = [i for i, m in enumerate(messages) if m["role"] == "user"]
    keep = set(user_idx[-window:])
    for i in user_idx:
        if i in keep:
            continue
        m = messages[i]
        if isinstance(m["content"], list):
            m["content"] = [
                {"type": "text", "text": "[earlier image elided to fit context]"}
                if p.get("type") == "image_url" else p
                for p in m["content"]
            ]


def run_task(
    client: OpenRouterClient,
    model_tag: str,
    model_slug: str,
    provider: dict | None,
    task_dir: Path,
    *,
    max_turns: int,
    image_window: int,
    log_path: Path,
) -> dict:
    """Run one (model, task) rollout. Returns a result record."""
    sid = task_dir.name
    ws = setup_workspace(task_dir)
    state: dict = {"images_viewed": [], "done": False, "submission_path": None}

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": INITIAL_USER.format(sid=sid)},
    ]

    started = time.time()
    stop = "max_turns"
    last_error_streak = 0
    MAX_ERRORS = 5

    def _log(line: str) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} [{model_tag}] {sid} {line}\n")

    turn = 0
    for turn in range(1, max_turns + 1):
        try:
            reply = client.chat(
                model=model_slug,
                messages=messages,
                provider=provider,
            )
        except Exception as e:
            stop = f"api_error:{str(e)[:80]}"
            _log(f"turn={turn} {stop}")
            break

        messages.append({"role": "assistant", "content": reply})
        call = parse_tool_call(reply)
        if call is None:
            last_error_streak += 1
            _log(f"turn={turn} no_tool_call (streak={last_error_streak})")
            if last_error_streak >= MAX_ERRORS:
                stop = "no_tool_call_x5"
                break
            messages.append({"role": "user",
                             "content": 'Your last reply was not a valid tool call. Reply with ONLY one JSON object: {"tool":"<name>","args":{...}}.'})
            continue
        last_error_streak = 0
        tool, args = call

        fn = DISPATCH[tool]
        result = fn(args, ws, state)

        # Build the next user message from the tool result.
        if isinstance(result, dict):
            parts = []
            if result.get("text"):
                parts.append({"type": "text", "text": result["text"]})
            if result.get("image_b64"):
                parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{result['image_mime']};base64,{result['image_b64']}"
                    },
                })
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": [{"type": "text", "text": str(result)}]})

        _log(f"turn={turn} tool={tool} ok cost_usd={client.cost_usd:.4f}")

        if state.get("done"):
            stop = "write_submission"
            break

        strip_old_images(messages, image_window)

    # Score
    sub_path = ws / "submission.json"
    truth_path = task_dir / "grade" / "truth.json"
    score_py = task_dir / "grade" / "score.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(score_py), str(sub_path), str(truth_path)],
            capture_output=True, text=True, timeout=60,
        )
        reward_json = json.loads(proc.stdout)
    except Exception as e:
        reward_json = {
            "exact_mapping_reward": 0.0,
            "arm_pair_f1": 0.0,
            "structural_error": f"scoring_subprocess_error: {e}",
        }

    rec = {
        "saguaro_id": sid,
        "model_tag": model_tag,
        "model_slug": model_slug,
        "exact_mapping_reward": reward_json.get("exact_mapping_reward", 0.0),
        "arm_pair_f1": reward_json.get("arm_pair_f1", 0.0),
        "structural_error": reward_json.get("structural_error"),
        "stop": stop,
        "turns_taken": turn,
        "max_turns": max_turns,
        "images_viewed": state.get("images_viewed", []),
        "cost_usd_running": round(client.cost_usd, 4),
        "wall_time_sec": round(time.time() - started, 1),
    }
    # Best-effort cleanup
    try:
        shutil.rmtree(ws)
    except Exception:
        pass
    return rec


# -----------------------------------------------------------------------------
# Top-level run
# -----------------------------------------------------------------------------

def _expand_models(models_arg: str, registry: dict) -> list[tuple[str, str, dict | None]]:
    """Resolve a comma-separated model arg into [(tag, slug, provider), ...]."""
    if models_arg == "all":
        tags = list(registry["models"].keys())
    else:
        tags = [m.strip() for m in models_arg.split(",") if m.strip()]
    out = []
    for tag in tags:
        if tag not in registry["models"]:
            raise SystemExit(f"unknown model tag: {tag!r}. known: {sorted(registry['models'])}")
        rec = registry["models"][tag]
        out.append((tag, rec["slug"], rec.get("provider")))
    return out


def _expand_tasks(tasks_arg: str | None) -> list[Path]:
    tasks_dir = REPO / "tasks"
    if tasks_arg is None or tasks_arg == "all":
        return sorted([p for p in tasks_dir.iterdir() if p.is_dir() and (p / "task.toml").exists()])
    out = []
    for sid in tasks_arg.split(","):
        sid = sid.strip()
        if not sid:
            continue
        p = tasks_dir / sid
        if not p.exists():
            raise SystemExit(f"no such task: {sid!r}")
        out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default="all",
                    help="comma-separated model tags (see harness/models.json) or 'all'")
    ap.add_argument("--tasks", default=None,
                    help="comma-separated saguaro_ids or 'all' (default: all 25)")
    ap.add_argument("--max-turns", type=int, default=14)
    ap.add_argument("--image-window", type=int, default=6,
                    help="keep image attachments only on the most recent N user messages")
    ap.add_argument("--cost-cap", type=float, default=None,
                    help="abort a model once its running OpenRouter cost exceeds this (USD)")
    ap.add_argument("--run-id", default=None,
                    help="run identifier (default: timestamp). results land at runs/<run-id>/")
    ap.add_argument("--registry", default=str(HERE / "models.json"))
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        # Convenience: try ~/.openrouter_key like wanderbench's pattern.
        key_file = Path.home() / ".openrouter_key"
        if key_file.exists():
            api_key = key_file.read_text().strip()
    if not api_key:
        raise SystemExit("Set OPENROUTER_API_KEY or write the key to ~/.openrouter_key")

    registry = json.loads(Path(args.registry).read_text())
    models = _expand_models(args.models, registry)
    tasks = _expand_tasks(args.tasks)

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    runs_dir = REPO / "runs" / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "config.json").write_text(json.dumps({
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "models": [{"tag": t, "slug": s, "provider": p} for (t, s, p) in models],
        "tasks": [t.name for t in tasks],
        "max_turns": args.max_turns,
        "image_window": args.image_window,
        "cost_cap": args.cost_cap,
    }, indent=2))

    print(f"[run] id={run_id}  models={[m[0] for m in models]}  n_tasks={len(tasks)}", flush=True)

    for (tag, slug, provider) in models:
        client = OpenRouterClient(api_key=api_key)
        results: list[dict] = []
        capped = False
        out_path = runs_dir / f"{tag}.json"
        log_path = runs_dir / f"{tag}.log"

        for i, task_dir in enumerate(tasks, 1):
            if args.cost_cap and client.cost_usd >= args.cost_cap:
                print(f"[{tag}] cost cap ${args.cost_cap:.2f} reached at "
                      f"${client.cost_usd:.4f} — skipping remaining {len(tasks)-i+1} tasks",
                      flush=True)
                capped = True
                break
            print(f"[{tag}] task {i}/{len(tasks)}: {task_dir.name}  "
                  f"[cost ${client.cost_usd:.3f}]", flush=True)
            try:
                rec = run_task(
                    client, tag, slug, provider, task_dir,
                    max_turns=args.max_turns,
                    image_window=args.image_window,
                    log_path=log_path,
                )
            except Exception as e:
                rec = {
                    "saguaro_id": task_dir.name,
                    "model_tag": tag,
                    "model_slug": slug,
                    "error": str(e)[:200],
                    "exact_mapping_reward": 0.0,
                    "arm_pair_f1": 0.0,
                }
            results.append(rec)
            print(f"    -> reward={rec.get('exact_mapping_reward')}  f1={rec.get('arm_pair_f1', 0):.3f}  "
                  f"turns={rec.get('turns_taken')}  stop={rec.get('stop')}  "
                  f"err={rec.get('structural_error')}", flush=True)
            # Incremental write so a SIGINT doesn't lose results
            out_path.write_text(json.dumps({
                "model_tag": tag,
                "model_slug": slug,
                "provider": provider,
                "served_providers": sorted(client.served_providers),
                "cost_usd": round(client.cost_usd, 4),
                "calls": client.calls,
                "capped_cost": capped,
                "results": results,
            }, indent=2))

        n = len(results)
        mean_exact = sum(r.get("exact_mapping_reward", 0) for r in results) / max(1, n)
        mean_f1 = sum(r.get("arm_pair_f1", 0) for r in results) / max(1, n)
        print(f"[{tag}] done. mean exact={mean_exact:.3f}  mean f1={mean_f1:.3f}  "
              f"cost=${client.cost_usd:.3f}  providers={sorted(client.served_providers)}"
              f"{'  CAPPED' if capped else ''}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
