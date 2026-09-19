#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
source .env
set +a

curl -s -X POST "http://127.0.0.1:4000/key/generate" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "models": ["qa-test-plan-creation", "qa-test-case-execution", "qa-bug-logging", "qa-fix-verification", "untagged"],
    "metadata": {"name": "qa-usage"}
  }'
