#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/soo/code/Agent4OLAP"
MODEL="${MODEL:-gpt-5.6-terra}"
REASONING_EFFORT="${REASONING_EFFORT:-high}"
SERVICE_TIER="${SERVICE_TIER:-flex}"
MAX_STEPS="${MAX_STEPS:-6}"
TIMEOUT_SEC="${TIMEOUT_SEC:-240}"
LIMIT="${LIMIT:-0}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
TRACE_ROOT="experiments/logs/terra_high_paired"

cd "$ROOT"

uv run python experiments/src/run_query_batch.py \
  --mode dashboard \
  --queries-dir experiments/queries \
  --model "$MODEL" \
  --dashboard-id 13 \
  --username abc \
  --password abc \
  --login-mode dom \
  --max-steps "$MAX_STEPS" \
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

uv run python experiments/src/run_query_batch.py \
  --mode streamlit \
  --queries-dir experiments/queries \
  --model "$MODEL" \
  --dashboard-id 13 \
  --username abc \
  --password abc \
  --max-steps "$MAX_STEPS" \
  --per-query-timeout-sec "$TIMEOUT_SEC" \
  --limit "$LIMIT" \
  --trace-root "$TRACE_ROOT" \
  --batch-id "$RUN_ID/state_aware" \
  --extra-arg=--service-tier \
  --extra-arg="$SERVICE_TIER" \
  --extra-arg=--reasoning-effort \
  --extra-arg="$REASONING_EFFORT" \
  --extra-arg=--chat-wait-timeout \
  --extra-arg=180

uv run python experiments/src/score_e2e_batch.py \
  --batch-dir "$TRACE_ROOT/$RUN_ID/state_aware" \
  --output "$TRACE_ROOT/$RUN_ID/state_aware/scored_report.json"

printf 'Paired run complete: %s\n' "$TRACE_ROOT/$RUN_ID"
