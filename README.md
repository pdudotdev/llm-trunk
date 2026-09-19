# llm-trunk

Skill-tagged routing gateway for Claude Code, built on [LiteLLM](https://docs.litellm.ai/). Claude Code traffic is routed to a specific Anthropic model + effort level based on which QA skill was invoked — identified by the sha256 hash of that skill's `SKILL.md`, not by a prompt classifier.

See [`LLM-TRUNK.md`](./LLM-TRUNK.md) for the full design spec this repo implements.

## How it works

llm-trunk sits between Claude Code and Anthropic as a local reverse proxy on `127.0.0.1:4000`. It never re-implements the Anthropic API — LiteLLM does the actual proxying; this repo only owns the *routing policy* layered on top via a custom LiteLLM callback.

**One-time setup**, before any live traffic flows:

1. Each `SKILL.md` in the client repo gets sha256-hashed.
2. Those hashes, plus a bucket/alias/effort/token-cap per skill, are recorded in `catalog.yaml` — the single routing table.
3. The Docker Compose stack (LiteLLM + Postgres) starts up, loading `litellm/config.yaml`, which declares the real Anthropic model behind each alias, pricing, and the custom callback to run on every request.
4. A scoped virtual key is minted through LiteLLM's own admin API, so the client never sees the real Anthropic key.

**Every live request** then flows like this:

1. Claude Code POSTs a `/v1/messages` request to the proxy, invoking a skill (or not).
2. A custom LiteLLM callback intercepts the request *before* it reaches Anthropic. It figures out which skill (if any) was invoked — from request headers, from a `SKILL.md` embedded in the request, or from session memory if the skill was invoked on an earlier turn in the same conversation — and hashes it.
3. That skill id + hash is looked up against `catalog.yaml`.
4. A routing decision is made: tagged skill with a matching hash and an input size under its cap → allow, routed to that skill's model/effort; no skill recognized → allow, routed to the cheap `untagged` fallback; stale hash, oversized input, or a crossed vendor price cliff → deny, and the request never reaches Anthropic.
5. On allow, the client's own requested model is overridden with the resolved one, and the skill is remembered for the rest of the conversation, so later turns don't need to resend `SKILL.md` to stay on the same route.
6. Once Anthropic responds, the callback logs which skill/alias/effort/tokens/cost were involved, for later routed-vs-unrouted bill comparisons.

## Files, in the order they come into play

### Setup phase

| File | Role |
|---|---|
| `scripts/hash_skill.py` | CLI: `python3 scripts/hash_skill.py <path/to/SKILL.md>` prints the sha256 hex digest of a skill file's bytes — the value that goes into `catalog.yaml`. |
| `catalog.yaml` | The routing table. One row per tagged skill (sha256, bucket, alias, vendor, effort, `max_input`/`max_output`) plus the `untagged` fallback row. `vendor` is `anthropic` for every row today, but is what lets `decide()` apply the right vendor's price-cliff check once a non-Anthropic route is added. Source of truth for every routing decision, both when authored here and when read live on each request. |
| `docker-compose.yml` | Brings up the two containers: `postgres` (spend/virtual-key storage) and `litellm` (the proxy itself), mounting `litellm/config.yaml`, `policy/`, and `catalog.yaml` into the LiteLLM container. |
| `litellm/config.yaml` | LiteLLM's own config, loaded on boot: the `model_list` mapping each catalog alias to a real Anthropic model id and its per-token pricing, the `callbacks` entry registering `policy.litellm_callback`, and `general_settings` (master key, database URL). |
| `scripts/create_qa_usage_key.sh` | Run once the stack is up: calls LiteLLM's `/key/generate` admin endpoint to mint the `qa-usage`-labeled virtual key, scoped to all four skill aliases plus `untagged`. The secret it returns — not anything in this repo — is what the client actually authenticates with. |

### Per-request phase

| File | Role |
|---|---|
| `policy/litellm_callback.py` | The custom `CustomLogger` LiteLLM invokes on every request. `async_pre_call_hook` resolves the skill (headers → embedded `SKILL.md` → sticky session → untagged), calls `decide()`, then either raises an HTTP error (deny) or overrides the model/effort and remembers the skill for the session. `async_log_success_event` logs the outcome once Anthropic responds. |
| `policy/hash.py` | One function, `hash_skill_md()` — sha256-hashes the bytes of a `SKILL.md` extracted from a live request. Used by both the callback above and `scripts/hash_skill.py`. |
| `policy/cliffs.py` | Vendor input-size price-cliff thresholds (Grok/Gemini at 200k, OpenAI Astra-class at 272k). Called from `decide()` on every request via each catalog row's `vendor` field — a no-op today since every row is `anthropic` (no cliff), but live and ready for the moment a row's vendor changes. |
| `policy/decide.py` | The routing policy itself, as a pure function: given the catalog, a candidate skill id/hash, any sticky skill from earlier turns, and an estimated input size, returns an allow/deny `Decision` with the alias, effort, and reason — checking stale hash, vendor price cliffs, and the row's `max_input` cap. No LiteLLM or network dependency. |

### Reference

| File | Role |
|---|---|
| `LLM-TRUNK.md` | The design spec this repo implements — routing rules, the three-Anthropic-workspace key model, catalog schema, and the full build order. |
| `policy/__init__.py` | Empty — makes `policy/` an importable Python package. |
