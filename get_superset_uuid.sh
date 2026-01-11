#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./get_superset_uuid.sh http://localhost:8088 admin admin 12

SUPERSET_URL="${1:-${SUPERSET_URL:-http://localhost:8088}}"
USERNAME="${2:-${SUPERSET_USERNAME:-admin}}"
PASSWORD="${3:-${SUPERSET_PASSWORD:-admin}}"
DASHBOARD_ID="${4:-${DASHBOARD_ID:-}}"

if [[ -z "${DASHBOARD_ID}" ]]; then
  echo "ERROR: DASHBOARD_ID (numeric) is required. Example: 12" >&2
  exit 1
fi

LOGIN_BODY="$(mktemp)"
DASH_BODY="$(mktemp)"
cleanup() { rm -f "$LOGIN_BODY" "$DASH_BODY"; }
trap cleanup EXIT

echo "Superset: ${SUPERSET_URL}"
echo "User: ${USERNAME}"
echo "Dashboard ID: ${DASHBOARD_ID}"
echo

# 1) Login -> access token
LOGIN_CODE="$(curl -sS -o "$LOGIN_BODY" -w "%{http_code}" \
  -X POST "${SUPERSET_URL}/api/v1/security/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${USERNAME}\",\"password\":\"${PASSWORD}\",\"provider\":\"db\",\"refresh\":true}")"

if [[ "$LOGIN_CODE" != "200" ]]; then
  echo "ERROR: login failed (HTTP $LOGIN_CODE). Response:" >&2
  sed -n '1,200p' "$LOGIN_BODY" >&2
  exit 1
fi

ACCESS_TOKEN="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token","").strip())' < "$LOGIN_BODY")"

if [[ -z "${ACCESS_TOKEN}" ]]; then
  echo "ERROR: access_token is empty. Login response:" >&2
  sed -n '1,200p' "$LOGIN_BODY" >&2
  exit 1
fi

# 2) Dashboard detail -> uuid
DASH_CODE="$(curl -sS -o "$DASH_BODY" -w "%{http_code}" \
  -X GET "${SUPERSET_URL}/api/v1/dashboard/${DASHBOARD_ID}" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}")"

if [[ "$DASH_CODE" != "200" ]]; then
  echo "ERROR: dashboard fetch failed (HTTP $DASH_CODE). Response:" >&2
  sed -n '1,200p' "$DASH_BODY" >&2
  exit 1
fi

DASH_UUID="$(python3 -c 'import json,sys; d=json.load(sys.stdin); r=d.get("result", d); print((r.get("uuid") or "").strip())' < "$DASH_BODY")"

if [[ -z "${DASH_UUID}" ]]; then
  echo "ERROR: SUPERSET_DASHBOARD_UUID not found in dashboard response:" >&2
  sed -n '1,200p' "$DASH_BODY" >&2
  exit 1
fi

echo "ACCESS_TOKEN=${ACCESS_TOKEN}"
echo "SUPERSET_DASHBOARD_UUID=${DASH_UUID}"
echo "${DASH_UUID}"

