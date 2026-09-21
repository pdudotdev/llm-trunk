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
# to the alias names blocks every real request at the gate. Per-skill access
# is metadata.allowed_skills instead (checked in litellm_callback.py's
# _allowed_skills()); this key omits it on purpose, since the QA team
# legitimately needs every QA skill.
#
# max_budget/budget_duration/tpm_limit/rpm_limit are an orthogonal backstop,
# independent of routing/decide() -- pure LiteLLM key settings, no code
# changes needed elsewhere. Bounds total damage across ALL lanes combined,
# not per-lane: LiteLLM's per-model equivalents (model_max_budget,
# model_rpm_limit/model_tpm_limit) don't help here -- model_max_budget is
# gated behind a paid Enterprise license (confirmed: /key/generate rejects
# it without one), and model_rpm_limit/model_tpm_limit are silently
# swallowed into inert metadata by this LiteLLM version instead of being
# enforced (confirmed via /key/info -- no error, but no effect either).
curl -s -X POST "http://127.0.0.1:4000/key/generate" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": {"name": "qa-usage"},
    "max_budget": 50,
    "budget_duration": "30d",
    "rpm_limit": 20,
    "tpm_limit": 200000
  }'
