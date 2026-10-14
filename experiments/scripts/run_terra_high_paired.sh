#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MODEL="${MODEL:-gpt-5.6-terra}"
REASONING_EFFORT="${REASONING_EFFORT:-high}"
SERVICE_TIER="${SERVICE_TIER:-flex}"
MAX_STEPS="${MAX_STEPS:-30}"
DASHBOARD_ACTION_BUDGET="${DASHBOARD_ACTION_BUDGET:-6}"
TIMEOUT_SEC="${TIMEOUT_SEC:-0}"
LIMIT="${LIMIT:-0}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
TRACE_ROOT="experiments/logs/terra_high_paired"

cd "$ROOT"

uv run python experiments/src/run_query_batch.py \
  --mode streamlit \
  --queries-dir experiments/queries \
  --model "$MODEL" \
  --dashboard-id 13 \
  --username abc \
  --password abc \
  --max-steps "$MAX_STEPS" \
  --extra-arg=--dashboard-action-budget \
  --extra-arg="$DASHBOARD_ACTION_BUDGET" \
  --per-query-timeout-sec "$TIMEOUT_SEC" \
  --limit "$LIMIT" \
  --trace-root "$TRACE_ROOT" \
  --batch-id "$RUN_ID/state_aware" \
  --restart-container streamlit_app \
  --extra-arg=--service-tier \
  --extra-arg="$SERVICE_TIER" \
  --extra-arg=--reasoning-effort \
  --extra-arg="$REASONING_EFFORT" \
  --extra-arg=--chat-wait-timeout \
  --extra-arg=0

uv run python experiments/src/score_e2e_batch.py \
  --batch-dir "$TRACE_ROOT/$RUN_ID/state_aware" \
  --output "$TRACE_ROOT/$RUN_ID/state_aware/scored_report.json"

uv run python experiments/src/run_query_batch.py \
  --mode dashboard \
  --queries-dir experiments/queries \
  --model "$MODEL" \
  --dashboard-id 13 \
  --username abc \
  --password abc \
  --login-mode dom \
  --max-steps "$MAX_STEPS" \
  --extra-arg=--dashboard-action-budget \
  --extra-arg="$DASHBOARD_ACTION_BUDGET" \
  --per-query-timeout-sec "$TIMEOUT_SEC" \
  --limit "$LIMIT" \
  --trace-root "$TRACE_ROOT" \
  --batch-id "$RUN_ID/dashboard" \
  --extra-arg=--service-tier \
  --extra-arg="$SERVICE_TIER" \
  --extra-arg=--reasoning-effort \
  --extra-arg="$REASONING_EFFORT"

uv run python experiments/src/score_e2e_batch.py \
  --batch-dir "$TRACE_ROOT/$RUN_ID/dashboard" \
  --output "$TRACE_ROOT/$RUN_ID/dashboard/scored_report.json"

printf 'Paired run complete: %s\n' "$TRACE_ROOT/$RUN_ID"
