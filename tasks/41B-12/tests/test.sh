#!/usr/bin/env bash
# Harbor verifier — runs as root (per task.toml [verifier].user).
# Reads the agent's /workspace/submission.json, scores per-cell against
# /grade/truth.json using field-typed tolerances, writes
# /logs/verifier/reward.{json,txt}.
#
# Always exit 0 — the reward is the signal, not the exit code (mirrors deep-swe
# and wanderbench).
set -euo pipefail

LOG_PFX="[verifier]"

mkdir -p /logs/verifier /logs/agent /logs/artifacts

echo "${LOG_PFX} scoring saguaro-bench (curation) task 41B-12"

python3 /grade/score.py /workspace/submission.json /grade/truth.json \
    > /logs/verifier/reward.json

jq -r '.cell_accuracy_reward' /logs/verifier/reward.json > /logs/verifier/reward.txt

REWARD=$(cat /logs/verifier/reward.txt)
F1=$(jq -r '.row_f1 // empty' /logs/verifier/reward.json)
MISSING=$(jq -r '.rows_missing // empty' /logs/verifier/reward.json)
EXTRA=$(jq -r '.rows_extra // empty' /logs/verifier/reward.json)
ERR=$(jq -r '.structural_error // empty' /logs/verifier/reward.json)

echo "${LOG_PFX} reward=${REWARD} row_f1=${F1} missing=${MISSING} extra=${EXTRA}${ERR:+ structural_error=$ERR}"

# Stash the submission (if present) into /logs/artifacts for the trajectory viewer.
if [[ -f /workspace/submission.json ]]; then
    cp /workspace/submission.json /logs/artifacts/submission.json
fi

exit 0
