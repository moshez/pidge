#!/usr/bin/env bash
# Smoke-test a built Lambda image: SQLite FTS5 is present, the handler
# imports, and the Lambda runtime (through the base image's runtime interface
# emulator) serves the OAuth metadata and rejects an unauthenticated MCP call.
#
# Usage: infra/scripts/smoke_image.sh pidge:candidate
set -euo pipefail

image="${1:?usage: smoke_image.sh IMAGE}"
name="pidge-smoke-$$"
port="${SMOKE_PORT:-9000}"
url="http://127.0.0.1:${port}/2015-03-31/functions/function/invocations"
host="abc.lambda-url.us-east-2.on.aws"

docker run --rm --entrypoint python3 "$image" -c '
import sqlite3
sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(a)")
import pidge.lambda_main
print("imports ok, sqlite", sqlite3.sqlite_version)
'

docker run -d --name "$name" -p "127.0.0.1:${port}:8080" \
  -e AWS_REGION=us-east-2 -e AWS_DEFAULT_REGION=us-east-2 \
  -e AWS_ACCESS_KEY_ID=smoke -e AWS_SECRET_ACCESS_KEY=smoke \
  -e MAIL_DOMAIN=example.org -e FROM_ADDRESS=mail@example.org \
  -e OWNER_ADDRESS=owner@example.net -e CONFIGURATION_SET=pidge \
  -e MESSAGE_ID_DOMAIN=us-east-2.amazonses.com -e BUCKET=smoke \
  -e ISSUER=https://cognito-idp.us-east-2.amazonaws.com/us-east-2_smoke \
  -e USER_POOL_ID=us-east-2_smoke \
  -e SESSION_KEY_PARAMETER=/pidge/session-signing-key \
  -e SIGNER_KEY_ID=smoke -e SIGNER_SECRET_PARAMETER=/pidge/url-signer-secret \
  -e PIDGE_BUILD="${GITHUB_SHA:-smoke}" \
  "$image" >/dev/null
trap 'docker logs "$name" 2>&1 | tail -20; docker rm -f "$name" >/dev/null' EXIT

invoke() {
  curl -sS --noproxy '*' --retry 10 --retry-connrefused --retry-delay 1 \
    "$url" -d "$1"
}

expect() {
  local label="$1" pattern="$2" response="$3"
  if [[ "$response" != *"$pattern"* ]]; then
    echo "FAIL ($label): $response" >&2
    exit 1
  fi
  echo "ok: $label"
}

metadata=$(invoke '{"rawPath":"/.well-known/oauth-protected-resource/mcp","headers":{},
  "requestContext":{"domainName":"'"$host"'","http":{"method":"GET"}}}')
expect "protected-resource metadata" '"statusCode": 200' "$metadata"
expect "resource URL" "https://${host}/mcp" "$metadata"

mcp=$(invoke '{"rawPath":"/mcp","headers":{},"body":"{}",
  "requestContext":{"domainName":"'"$host"'","http":{"method":"POST"}}}')
expect "unauthenticated MCP call is refused" '"statusCode": 401' "$mcp"
expect "WWW-Authenticate points at the metadata" "resource_metadata=" "$mcp"

trap 'docker rm -f "$name" >/dev/null' EXIT
echo "smoke test passed"
