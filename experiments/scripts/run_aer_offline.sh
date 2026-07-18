#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${ROOT}/experiments/retrieval/runs/${TIMESTAMP}"
SESSIONS="${RUN_DIR}/query_sessions.jsonl"
REPORT="${RUN_DIR}/report.json"
VALIDATION_REPORT="${RUN_DIR}/gold_validation.json"

mkdir -p "${RUN_DIR}"
python3 "${ROOT}/experiments/src/validate_aer_gold.py" \
  --strict \
  --output "${VALIDATION_REPORT}"
python3 "${ROOT}/experiments/src/build_aer_benchmark.py" --output "${SESSIONS}"
python3 "${ROOT}/experiments/src/evaluate_aer_retrieval.py" \
  --sessions "${SESSIONS}" \
  --output "${REPORT}"

printf '\nAER offline evaluation completed.\nValidation: %s\nReport: %s\n' \
  "${VALIDATION_REPORT}" "${REPORT}"
