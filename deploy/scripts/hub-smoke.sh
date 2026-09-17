#!/usr/bin/env bash
# QwenPaw Hub canary smoke gate (EP-2-10, A7): the checks a canary
# deployment must pass before the service cutover. Exit 0 = promote,
# non-zero = roll the canary away.
#
# Usage:
#   hub-smoke.sh <base-url> <admin-username> <admin-password>
set -euo pipefail

BASE="${1:?usage: hub-smoke.sh <base-url> <admin-user> <admin-password>}"
USER_NAME="${2:?usage: hub-smoke.sh <base-url> <admin-user> <admin-password>}"
PASSWORD="${3:?usage: hub-smoke.sh <base-url> <admin-user> <admin-password>}"

fail() {
    echo "SMOKE-FAIL: $*" >&2
    exit 1
}

echo "== smoke against $BASE =="

# 1) unauthenticated request must be refused (auth plane alive)
code=$(curl -s -o /dev/null -w '%{http_code}' \
    --max-time 10 "$BASE/api/hub/healthz")
[ "$code" = "401" ] || [ "$code" = "403" ] \
    || fail "healthz without auth returned $code (expected 401/403)"

# 2) admin login mints a bearer token
token=$(curl -s --max-time 10 -X POST "$BASE/api/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$USER_NAME\",\"password\":\"$PASSWORD\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))')
[ -n "$token" ] || fail "login did not return a token"

# 3) authenticated healthz answers
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -H "Authorization: Bearer $token" "$BASE/api/hub/healthz")
[ "$code" = "200" ] || fail "authenticated healthz returned $code"

# 4) permissions plane answers (role/denied contract)
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -H "Authorization: Bearer $token" "$BASE/api/hub/me/permissions")
[ "$code" = "200" ] || fail "me/permissions returned $code"

# 5) runtime registry answers (control-plane DB reachable)
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -H "Authorization: Bearer $token" "$BASE/api/hub/runtimes")
[ "$code" = "200" ] || fail "runtimes listing returned $code"

echo "SMOKE-PASS"
