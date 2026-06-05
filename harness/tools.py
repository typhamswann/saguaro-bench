"""Tool definitions + dispatch for the SaguaroBench OpenRouter loop.

The model emits one tool call per turn as a JSON object:

    {"tool": "<name>", "args": {...}}

We parse, dispatch, and return either:
- a plain string (becomes a `text` content part in the next user message)
- a dict with {"text": ..., "image_b64": ..., "image_mime": ...} (becomes
  a text + image_url content list)

This matches the wanderbench harbor_driver pattern (one tool call per
turn, JSON-only assistant output) — universally portable across every
chat-completions-style provider OpenRouter routes to, no provider-specific
function-calling format required.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

ALLOWED = {"list_dir", "read_text", "view_image", "write_submission"}

# Sandbox: the agent's "workspace" is a host directory we set up per task.
# Everything else is off-limits — paths must resolve under this root.


def _safe_join(root: Path, path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        # Strip a leading "/workspace/" if present so the agent's mental model
        # ("files live in /workspace") still works when we run on the host.
        try:
            rel = p.relative_to("/workspace")
            return (root / rel).resolve()
        except ValueError:
            return (root / p.relative_to(p.anchor)).resolve()
    return (root / p).resolve()


def _under(root: Path, p: Path) -> bool:
    """Resolve both sides before comparison so /tmp ↔ /private/tmp symlinks
    on macOS don't trip the sandbox check."""
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _rel(root: Path, p: Path) -> str:
    """Pretty path for display: always rooted at /workspace/ in the agent's
    mental model, regardless of where the host root actually lives."""
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(p)


def _err(msg: str) -> str:
    return f"ERROR: {msg}"


def list_dir(args: dict, root: Path, state: dict) -> str:
    path = args.get("path", ".")
    p = _safe_join(root, path)
    if not _under(root, p):
        return _err(f"path {path!r} resolves outside the workspace")
    if not p.exists():
        return _err(f"path {path!r} does not exist")
    if not p.is_dir():
        return _err(f"path {path!r} is not a directory")
    entries = []
    for child in sorted(p.iterdir()):
        kind = "dir" if child.is_dir() else "file"
        size = child.stat().st_size if child.is_file() else "-"
        entries.append(f"  {kind:4s}  {size!s:>10s}  {_rel(root, child)}")
    return f"# /workspace/{_rel(root, p)}\n" + "\n".join(entries)


def read_text(args: dict, root: Path, state: dict) -> str:
    path = args.get("path")
    if not isinstance(path, str):
        return _err("read_text: missing 'path' string argument")
    p = _safe_join(root, path)
    if not _under(root, p):
        return _err(f"path {path!r} resolves outside the workspace")
    if not p.exists() or not p.is_file():
        return _err(f"no such file: {path!r}")
    LIMIT = 50_000
    text = p.read_text(errors="replace")
    if len(text) > LIMIT:
        text = text[:LIMIT] + f"\n... [truncated, total {len(text)} chars]"
    return text


_IMG_MIME = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
}


def view_image(args: dict, root: Path, state: dict):
    path = args.get("path")
    if not isinstance(path, str):
        return _err("view_image: missing 'path' string argument")
    p = _safe_join(root, path)
    if not _under(root, p):
        return _err(f"path {path!r} resolves outside the workspace")
    if not p.exists() or not p.is_file():
        return _err(f"no such file: {path!r}")
    mime = _IMG_MIME.get(p.suffix.lower())
    if mime is None:
        return _err(f"unsupported image extension: {p.suffix!r}")
    b = p.read_bytes()
    rel = _rel(root, p)
    state.setdefault("images_viewed", []).append(rel)
    return {
        "text": f"Image at /workspace/{rel} ({len(b)} bytes).",
        "image_b64": base64.b64encode(b).decode("ascii"),
        "image_mime": mime,
    }


def write_submission(args: dict, root: Path, state: dict) -> str:
    content = args.get("content")
    if content is None:
        return _err("write_submission: missing 'content' argument")
    if isinstance(content, dict):
        content = json.dumps(content)
    if not isinstance(content, str):
        return _err("write_submission: 'content' must be a JSON string or object")
    # Soft validate: must at least be JSON-parseable.
    try:
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return _err("write_submission: content must decode to a JSON object")
    except Exception as e:
        return _err(f"write_submission: content is not valid JSON: {e}")
    sub_path = root / "submission.json"
    sub_path.write_text(content)
    state["submission_path"] = str(sub_path)
    state["done"] = True
    return f"Submission written to /workspace/submission.json ({len(content)} chars). The task will end and be scored."


DISPATCH = {
    "list_dir":          list_dir,
    "read_text":         read_text,
    "view_image":        view_image,
    "write_submission":  write_submission,
}


# -----------------------------------------------------------------------------
# Parsing the assistant's reply -> (tool, args) | None
# -----------------------------------------------------------------------------

_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def parse_tool_call(text: str):
    """Extract one tool call from the assistant's reply.

    Accepts:
        {"tool": "name", "args": {...}}
        ```json {"tool": "...", "args": {...}} ```
        plain prose with a JSON object somewhere inside
    """
    if not text:
        return None
    candidates: list[str] = []
    candidates.append(text.strip())
    for m in _FENCE_RE.finditer(text):
        candidates.append(m.group(1).strip())
    m = _JSON_OBJ_RE.search(text)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        tool = obj.get("tool") or obj.get("name") or obj.get("action")
        args = obj.get("args") or obj.get("arguments") or obj.get("params") or {}
        if isinstance(tool, str) and tool in ALLOWED and isinstance(args, dict):
            return tool, args
    return None
