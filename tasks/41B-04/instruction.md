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

Difficulty: **hard** (test split).
