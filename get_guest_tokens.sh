#!/usr/bin/env bash
set -euo pipefail

# usage:
# ./get_guest_token.sh http://localhost:8088 admin admin 12

SUPERSET_URL="${1:-http://localhost:8088}"
USERNAME="${2:-admin}"
PASSWORD="${3:-admin}"
DASHBOARD_ID="${4:-12}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: need $1"; exit 1; }; }
need curl
need python

echo "Superset: $SUPERSET_URL"
echo "User: $USERNAME"
echo "Dashboard ID: $DASHBOARD_ID"
echo

curl_json() {
  # prints: body\nHTTP_STATUS
  curl -sS -o - -w $'\n%{http_code}' "$@"
}

parse_json() {
  # $1: json string, $2: python statement that prints value (j is dict)
  local json="$1"
  local expr="$2"
  printf '%s' "$json" | python3 - <<PY
import sys, json
s = sys.stdin.read()
if not s.strip():
  print("")
  raise SystemExit(0)

try:
  j = json.loads(s)
except Exception:
  # show nothing, caller will print raw response
  print("")
  raise SystemExit(0)

try:
  $expr
except Exception:
  print("")
PY
}


# 1) login -> access token
LOGIN_OUT="$(curl_json -X POST "${SUPERSET_URL}/api/v1/security/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${USERNAME}\",\"password\":\"${PASSWORD}\",\"provider\":\"db\",\"refresh\":false}")"

LOGIN_BODY="$(printf '%s' "$LOGIN_OUT" | sed '$d')"
LOGIN_CODE="$(printf '%s' "$LOGIN_OUT" | tail -n 1)"

if [[ "$LOGIN_CODE" != "200" ]]; then
  echo "ERROR: login failed (HTTP $LOGIN_CODE). Response:"
  echo "$LOGIN_BODY"
  exit 1
fi

ACCESS_TOKEN="$(printf '%s' "$LOGIN_BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token","").strip())')"
if [[ -z "$ACCESS_TOKEN" ]]; then
  echo "ERROR: failed to parse access_token. Raw response:"
  echo "$LOGIN_BODY"
  exit 1    
fi

# 2) csrf token
COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT
CSRF_OUT="$(curl_json "${SUPERSET_URL}/api/v1/security/csrf_token/" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -c "$COOKIE_JAR")"

CSRF_BODY="$(printf '%s' "$CSRF_OUT" | sed '$d')"
CSRF_CODE="$(printf '%s' "$CSRF_OUT" | tail -n 1)"

if [[ "$CSRF_CODE" != "200" ]]; then
  echo "ERROR: csrf fetch failed (HTTP $CSRF_CODE). Response:"
  echo "$CSRF_BODY"
  exit 1
fi

CSRF_TOKEN="$(printf '%s' "$CSRF_BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("result","").strip())')"


# 3) dashboard uuid (resource id로 필요)
DASH_OUT="$(curl_json "${SUPERSET_URL}/api/v1/dashboard/${DASHBOARD_ID}" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Accept: application/json")"
DASH_BODY="$(printf '%s' "$DASH_OUT" | sed '$d')"
DASH_CODE="$(printf '%s' "$DASH_OUT" | tail -n 1)"

if [[ "$DASH_CODE" != "200" ]]; then
  echo "ERROR: dashboard fetch failed (HTTP $DASH_CODE). Response:"
  echo "$DASH_BODY"
  exit 1
fi

DASHBOARD_UUID="$(printf '%s' "$DASH_BODY" | python3 -c 'import json,sys; j=json.load(sys.stdin); print((j.get("result") or {}).get("uuid","").strip())')"
if [[ -z "$DASHBOARD_UUID" ]]; then
  echo "ERROR: failed to parse DASHBOARD_UUID. Raw response:"
  echo "$DASH_BODY"
  exit 1
fi

# 4) embed uuid도 같이 확인(옵션)
EMB_OUT="$(curl_json "${SUPERSET_URL}/api/v1/dashboard/${DASHBOARD_ID}/embedded" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Accept: application/json")"
EMB_BODY="$(printf '%s' "$EMB_OUT" | sed '$d')"
EMB_CODE="$(printf '%s' "$EMB_OUT" | tail -n 1)"

EMBED_UUID=""
if [[ "$EMB_CODE" == "200" ]]; then
  EMBED_UUID="$(printf '%s' "$EMB_BODY" | python3 -c 'import json,sys; j=json.load(sys.stdin); print((j.get("result") or {}).get("uuid","").strip())')"
fi

# 5) guest token
# Prefer embedded UUID if available; fall back to numeric dashboard id.
RESOURCE_ID="${EMBED_UUID:-$DASHBOARD_ID}"
echo "RESOURCE_ID=${RESOURCE_ID}"
GUEST_PAYLOAD="$(python - <<PY
import json
print(json.dumps({
  "user": {"username": "streamlit-guest"},
  "resources": [{"type": "dashboard", "id": "${RESOURCE_ID}"}],
  "rls": []
}))
PY
)"

GUEST_OUT="$(curl_json -X POST "${SUPERSET_URL}/api/v1/security/guest_token/" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "X-CSRFToken: ${CSRF_TOKEN}" \
  -H "X-CSRF-Token: ${CSRF_TOKEN}" \
  -H "Referer: ${SUPERSET_URL}/" \
  -H "Content-Type: application/json" \
  -b "$COOKIE_JAR" \
  -d "${GUEST_PAYLOAD}")"

GUEST_BODY="$(printf '%s' "$GUEST_OUT" | sed '$d')"
GUEST_CODE="$(printf '%s' "$GUEST_OUT" | tail -n 1)"

if [[ "$GUEST_CODE" != "200" ]]; then
  echo "ERROR: guest_token failed (HTTP $GUEST_CODE). Response:"
  echo "$GUEST_BODY"
  exit 1
fi

GUEST_TOKEN="$(printf '%s' "$GUEST_BODY" | python3 -c 'import json,sys; j=json.load(sys.stdin); print(j.get("token","").strip())')"
if [[ -z "$GUEST_TOKEN" ]]; then
  echo "ERROR: failed to parse guest token. Raw response:"
  echo "$GUEST_BODY"
  exit 1
fi

echo "ACCESS_TOKEN=${ACCESS_TOKEN}"
echo "DASHBOARD_UUID=${DASHBOARD_UUID}"
if [[ -n "$EMBED_UUID" ]]; then
  echo "EMBED_UUID=${EMBED_UUID}"
fi
echo "GUEST_TOKEN=${GUEST_TOKEN}"
python3 - <<PY
import base64, json
token = "${GUEST_TOKEN}"
payload = token.split(".")[1]
payload += "=" * (-len(payload) % 4)
data = base64.urlsafe_b64decode(payload.encode("utf-8"))
print("GUEST_TOKEN_PAYLOAD=" + data.decode("utf-8"))
PY
