#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
source .env
set +a

# Deliberately no "models" restriction: LiteLLM checks a key's model
# allow-list against the CLIENT'S requested model, before our callback ever
# runs to rewrite it -- and Claude Code always sends its own default model
# (e.g. claude-sonnet-5), never one of our alias names. Restricting this key
# to the alias names blocks every real request at the gate. Access control
# is decide()'s job, not this key's.
curl -s -X POST "http://127.0.0.1:4000/key/generate" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": {"name": "qa-usage"}
  }'
