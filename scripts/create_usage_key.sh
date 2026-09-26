#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
source .env
set +a

# Deliberately no "models" restriction: LiteLLM checks a key's model
# allow-list against the CLIENT'S requested model, before our callback ever
# runs to rewrite it -- and Claude Code always sends its own default model
# (e.g. claude-opus-5-5), never one of the tier names. Restricting this key
# to the tier names blocks every real request at the gate. To limit which
# skills can lift a key above the lowest tier, add
# "allowed_skills": [...] to its metadata (checked in litellm_callback.py's
# _allowed_skills()); this key omits it, so every registered skill works.
#
# max_budget/budget_duration/tpm_limit/rpm_limit are an orthogonal backstop,
# independent of routing/decide() -- pure LiteLLM key settings, no code
# changes needed elsewhere. Bounds total damage across ALL tiers combined,
# not per-tier: LiteLLM's per-model equivalents (model_max_budget,
# model_rpm_limit/model_tpm_limit) don't help here -- model_max_budget is
# gated behind a paid Enterprise license (confirmed: /key/generate rejects
# it without one), and model_rpm_limit/model_tpm_limit are silently
# swallowed into inert metadata by this LiteLLM version instead of being
# enforced (confirmed via /key/info -- no error, but no effect either).
# --fail-with-body: an HTTP error (bad master key, DB down) exits non-zero
# and still prints the reason; -S reports a gateway that isn't running.
curl -sS --fail-with-body -X POST "http://127.0.0.1:4000/key/generate" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": {"name": "llm-trunk-usage"},
    "max_budget": 50,
    "budget_duration": "30d",
    "rpm_limit": 20,
    "tpm_limit": 200000
  }'
