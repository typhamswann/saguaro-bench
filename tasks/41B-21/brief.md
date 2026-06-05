# Saguaro 41B-21 — full curation

Two volunteers measured this saguaro on plot 41B: one in 2023, one in 2026. Each produced a handwritten field-form recording the arm measurements. A human curator then matches arms across years (same physical arm = same canonical arm number) and digitizes the cleaned table.

Your job is to produce that cleaned table.

## Inputs

`/workspace/datasheets/` — 2 hand-redacted volunteer field forms,
    with **opaque filenames** (`sheet_A.png`, `sheet_B.png`). One sheet covers
    each year; read the date header to determine which is 2023 vs 2026. The
    curator's marginal canonical-arm renumberings have been blacked out, so
    the only arm numbers visible are the volunteer's paper-arm numbers — which
    DIFFER between years (the volunteer re-counted from "north-most then
    clockwise" each time).

`/workspace/photos/` — 8 field photo(s), **opaque filenames**
    (`photo_001.jpg`, `photo_002.jpg`, ...). Years are mixed and not
    annotated. Photos help disambiguate arm matching when two arms are at
    similar directions or when the digitized measurements are inconclusive
    (e.g., the saguaro's identifying whiteboard is visible in some photos).

## Output

Write your cleaned table to `/workspace/submission.json` as a JSON list of
row objects. Each row has these fields:

```
saguaro_id   string  — always "41B-21" for this task
year         int     — 2023 or 2026
arm          string  — canonical arm number ("1", "2", ...)
direction    number  — compass bearing from main stem, degrees (0–360)
A            number  — height where arm emerges from main stem, meters
B            number  — datum-mark height near A, meters
C            number  — arm-tip height, meters
D            number  — datum-mark height near C, meters
E            number  — horizontal distance from main stem to arm tip, meters
note         string  — recorder note (use "" if none)
```

Example row:

```json
{"saguaro_id": "41B-21", "year": 2023, "arm": "1",
 "direction": 360, "A": 1.89, "B": 0.98, "C": 2.04,
 "D": 0.98, "E": 0.2, "note": ""}
```

## Canonical arm numbering

Canonical arm numbers identify the SAME physical arm across years. Arm `"3"`
in 2023 and arm `"3"` in 2026 must be the same physical arm. Arms that emerged
after the 2023 survey get canonical numbers continuing from the 2023 count
(if 2023 has 5 arms and 2026 has 8, the 3 new 2026-only arms become canonical
6, 7, 8 — pick the assignment that's consistent with arm direction so a
re-survey would give the same numbering).

The volunteer's paper-arm numbers on the sheets do NOT match the canonical
numbering. You must derive the canonical numbering yourself by matching arms
across years using direction, A/E measurements, and photos.

## Row schedule (target)

- **2023**: 7 arm(s), canonical numbers `['1', '2', '3', '4', '5', '6', '7']`
- **2026**: 7 arm(s), canonical numbers `['1', '2', '3', '4', '5', '6', '7']`

## Scoring

Per-cell match against ground truth, keyed by `(saguaro_id, year, arm)`:

- `direction`: ±1°
- `A`, `B`, `C`, `D`, `E`: ±0.011 m
- `note`: word-set Jaccard ≥0.5 OR any-of-acceptable list match (empty=empty)
- `saguaro_id`: normalized string equality

Missing rows score 0 across all their cells. Extra (hallucinated) rows incur
a 5% penalty each, capped at 50%. Reward is in [0, 1].
