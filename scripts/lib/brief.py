"""Render per-task brief.md for the curation task.

The brief gives the agent the saguaro id, the opaque asset inventory, the
output schema, and the per-year canonical-arm count target — but NOT the
cross-year arm mapping or the per-arm measurements. That's what the agent
has to figure out from the sheets + photos.
"""
from __future__ import annotations


def build_brief(
    saguaro_id: str,
    n_sheets: int,
    n_photos: int,
    canonical_arms_per_year: dict,
    n_excluded: int = 0,
) -> str:
    """canonical_arms_per_year: {year_int: [arm_str, ...]} in ascending order."""
    lines: list[str] = []
    lines.append(f"# Saguaro {saguaro_id} — full curation")
    lines.append("")
    lines.append(
        "Two volunteers measured this saguaro on plot 41B: one in 2023, one "
        "in 2026. Each produced a handwritten field-form recording the arm "
        "measurements. A human curator then matches arms across years (same "
        "physical arm = same canonical arm number) and digitizes the cleaned "
        "table."
    )
    lines.append("")
    lines.append("Your job is to produce that cleaned table.")
    lines.append("")
    lines.append("## Inputs")
    lines.append("")
    lines.append(f"`/workspace/datasheets/` — {n_sheets} hand-redacted volunteer field forms,")
    lines.append("    with **opaque filenames** (`sheet_A.png`, `sheet_B.png`). One sheet covers")
    lines.append("    each year; read the date header to determine which is 2023 vs 2026. The")
    lines.append("    curator's marginal canonical-arm renumberings have been blacked out, so")
    lines.append("    the only arm numbers visible are the volunteer's paper-arm numbers — which")
    lines.append("    DIFFER between years (the volunteer re-counted from \"north-most then")
    lines.append("    clockwise\" each time).")
    lines.append("")
    if n_photos:
        lines.append(f"`/workspace/photos/` — {n_photos} field photo(s), **opaque filenames**")
        lines.append("    (`photo_001.jpg`, `photo_002.jpg`, ...). Years are mixed and not")
        lines.append("    annotated. Photos help disambiguate arm matching when two arms are at")
        lines.append("    similar directions or when the digitized measurements are inconclusive")
        lines.append("    (e.g., the saguaro's identifying whiteboard is visible in some photos).")
    else:
        lines.append("`/workspace/photos/` — empty (no field photos available for this saguaro).")
    lines.append("")
    lines.append("## Output")
    lines.append("")
    lines.append("Write your cleaned table to `/workspace/submission.json` as a JSON list of")
    lines.append("row objects. Each row has these fields:")
    lines.append("")
    lines.append("```")
    lines.append("saguaro_id   string  — always \"" + saguaro_id + "\" for this task")
    lines.append("year         int     — 2023 or 2026")
    lines.append("arm          string  — canonical arm number (\"1\", \"2\", ...)")
    lines.append("direction    number  — compass bearing from main stem, degrees (0–360)")
    lines.append("A            number  — height where arm emerges from main stem, meters")
    lines.append("B            number  — datum-mark height near A, meters")
    lines.append("C            number  — arm-tip height, meters")
    lines.append("D            number  — datum-mark height near C, meters")
    lines.append("E            number  — horizontal distance from main stem to arm tip, meters")
    lines.append("note         string  — recorder note (use \"\" if none)")
    lines.append("```")
    lines.append("")
    lines.append("Example row:")
    lines.append("")
    lines.append("```json")
    lines.append('{"saguaro_id": "' + saguaro_id + '", "year": 2023, "arm": "1",')
    lines.append(' "direction": 360, "A": 1.89, "B": 0.98, "C": 2.04,')
    lines.append(' "D": 0.98, "E": 0.2, "note": ""}')
    lines.append("```")
    lines.append("")
    lines.append("## Canonical arm numbering")
    lines.append("")
    lines.append("Canonical arm numbers identify the SAME physical arm across years. Arm `\"3\"`")
    lines.append("in 2023 and arm `\"3\"` in 2026 must be the same physical arm. Arms that emerged")
    lines.append("after the 2023 survey get canonical numbers continuing from the 2023 count")
    lines.append("(if 2023 has 5 arms and 2026 has 8, the 3 new 2026-only arms become canonical")
    lines.append("6, 7, 8 — pick the assignment that's consistent with arm direction so a")
    lines.append("re-survey would give the same numbering).")
    lines.append("")
    lines.append("The volunteer's paper-arm numbers on the sheets do NOT match the canonical")
    lines.append("numbering. You must derive the canonical numbering yourself by matching arms")
    lines.append("across years using direction, A/E measurements, and photos.")
    lines.append("")
    lines.append("## Row schedule (target)")
    lines.append("")
    for year in sorted(canonical_arms_per_year):
        arms = canonical_arms_per_year[year]
        lines.append(f"- **{year}**: {len(arms)} arm(s), canonical numbers `{arms}`")
    lines.append("")
    if n_excluded:
        lines.append(
            f"Note: {n_excluded} truth row(s) for this saguaro are marked **excluded** "
            "(genuinely ambiguous on the paper or known-wrong) and are skipped by the "
            "scorer — neither their presence nor absence in your submission counts. "
            "You don't know which arms these are; submit your best effort for every "
            "canonical (year, arm) above."
        )
        lines.append("")
    lines.append("## Scoring")
    lines.append("")
    lines.append("Per-cell match against ground truth, keyed by `(saguaro_id, year, arm)`:")
    lines.append("")
    lines.append("- `direction`: ±1°")
    lines.append("- `A`, `B`, `C`, `D`, `E`: ±0.011 m")
    lines.append("- `note`: word-set Jaccard ≥0.5 OR any-of-acceptable list match (empty=empty)")
    lines.append("- `saguaro_id`: normalized string equality")
    lines.append("")
    lines.append("Missing rows score 0 across all their cells. Extra (hallucinated) rows incur")
    lines.append("a 5% penalty each, capped at 50%. Reward is in [0, 1].")
    return "\n".join(lines) + "\n"
