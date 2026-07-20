#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${ROOT}/experiments/retrieval/runs/${TIMESTAMP}"
SESSIONS="${RUN_DIR}/query_sessions.jsonl"
REPORT="${RUN_DIR}/report.json"
VALIDATION_REPORT="${RUN_DIR}/gold_validation.json"
QUERY_RESULT_AERS="${RUN_DIR}/query_result_aers.jsonl"
COVERAGE_REPORT="${RUN_DIR}/extended_coverage.json"
QUERY_RESULT_REPLAY_REPORT="${RUN_DIR}/query_result_replay.json"

mkdir -p "${RUN_DIR}"
python3 "${ROOT}/experiments/src/validate_aer_gold.py" \
  --strict \
  --output "${VALIDATION_REPORT}"
python3 "${ROOT}/experiments/src/materialize_query_result_aers.py" \
  --output "${QUERY_RESULT_AERS}"
python3 "${ROOT}/experiments/src/validate_extended_aer_coverage.py" \
  --query-result-aers "${QUERY_RESULT_AERS}" \
  --output "${COVERAGE_REPORT}"
uv run python "${ROOT}/experiments/src/replay_query_result_aers.py" \
  --output "${QUERY_RESULT_REPLAY_REPORT}"
python3 "${ROOT}/experiments/src/build_aer_benchmark.py" --output "${SESSIONS}"
python3 "${ROOT}/experiments/src/evaluate_aer_retrieval.py" \
  --sessions "${SESSIONS}" \
  --rankings-output "${RUN_DIR}/rankings.jsonl" \
  --output "${REPORT}"

printf '\nAER offline evaluation completed.\nValidation: %s\nCoverage: %s\nQuery-result replay: %s\nReport: %s\n' \
  "${VALIDATION_REPORT}" "${COVERAGE_REPORT}" "${QUERY_RESULT_REPLAY_REPORT}" "${REPORT}"
