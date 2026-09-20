# llm-trunk

Skill-tagged routing gateway for Claude Code, built on [LiteLLM](https://docs.litellm.ai/). Claude Code traffic is routed to a specific Anthropic model + effort level based on which QA skill was invoked — identified by the sha256 hash of that skill's `SKILL.md`, not by a prompt classifier.

## How it works

llm-trunk sits between Claude Code and Anthropic as a local reverse proxy on `127.0.0.1:4000`. It never re-implements the Anthropic API — LiteLLM does the actual proxying; this repo only owns the *routing policy* layered on top via a custom LiteLLM callback.

**One-time setup**, before any live traffic flows:

1. Each `SKILL.md` in the client repo gets sha256-hashed — specifically, the *body* (everything after the YAML frontmatter), since that's the only part Claude Code actually sends verbatim when a skill is invoked.
2. Those hashes, plus a bucket/alias/effort/token-cap per skill, are recorded in `catalog.yaml` — the single routing table.
3. The Docker Compose stack (LiteLLM + Postgres) starts up, loading `litellm/config.yaml`, which declares the real Anthropic model behind each alias, pricing, and the custom callback to run on every request.
4. A scoped virtual key is minted through LiteLLM's own admin API, so the client never sees the real Anthropic key.

**Every live request** then flows like this:

1. Claude Code POSTs a `/v1/messages` request to the proxy, invoking a skill (or not).
2. A custom LiteLLM callback intercepts the request *before* it reaches Anthropic. It figures out which skill (if any) was invoked — from request headers, from Claude Code's own `<command-name>` tag when a skill is invoked via `/skill-name` (Claude Code strips the YAML frontmatter client-side and sends a `Base directory for this skill: ...` line plus the body instead — the raw frontmatter is only a fallback, for content pasted or `@`-referenced directly), or from session memory if the skill was invoked on an earlier turn in the same conversation — and hashes the body.
3. That skill id + hash is looked up against `catalog.yaml`.
4. A routing decision is made: tagged skill with a matching hash and an input size under its cap → allow, routed to that skill's model/effort; no skill recognized → allow, routed to the cheap `untagged` fallback; an unrecognized skill id, a stale hash, oversized input, or a crossed vendor price cliff → deny, and the request never reaches Anthropic.
5. On allow, the client's own requested model, effort, and `max_tokens` are overridden with the resolved route's, and the skill is remembered for the rest of *that conversation*, so later turns don't need to resend `SKILL.md` to stay on the same route. A different conversation on the same key starts clean at `untagged`.
6. Once Anthropic responds, the callback logs which skill/alias/effort/tokens/cost were involved, for later routed-vs-unrouted bill comparisons.

## Lab design

This is a personal cost-routing lab, not a production deployment: one Mac, no auth beyond LiteLLM's own master/virtual keys, and it stops the moment the Mac sleeps.

### Two repos, one machine

- **llm-trunk** (this repo, public on GitHub) — the gateway itself: compose stack, LiteLLM config, the routing policy in `policy/`, and `catalog.yaml`.
- **company-client** (local-only, never pushed anywhere) — an emulated "employee checkout": four QA skills at `.claude/skills/<name>/SKILL.md` (test-plan creation, test-case execution, bug logging, fix verification — Claude Code only auto-discovers project skills from `.claude/skills/`, not a bare `skills/` at the repo root) plus a `.claude/settings.json` pointing `ANTHROPIC_BASE_URL` at the gateway. Exists purely to generate realistic Claude Code traffic for llm-trunk to route — never needs to leave this laptop.

### Three separate Anthropic workspaces

Kept apart so each produces its own clean, comparable bill:

- **Builder** — the Claude Code session that writes and maintains llm-trunk itself. Never goes through the gateway; no `ANTHROPIC_BASE_URL` override.
- **Routed-client** — the only Anthropic key llm-trunk ever holds (`ANTHROPIC_API_KEY` in `.env`). Every tagged and `untagged` request that makes it past `decide()` bills here.
- **Unrouted-baseline** — a separate Claude Code profile with no base-url override, running the identical QA prompts directly against Anthropic with no routing at all. Its bill is the "what if we hadn't routed" control group.

**Claude Code's own auth (subscription vs. API key) only matters for the Builder session.** The `company-client` session never authenticates with Anthropic at all — its `.claude/settings.json` redirects it entirely to the local gateway (`ANTHROPIC_BASE_URL=http://127.0.0.1:4000` + a LiteLLM virtual key as `ANTHROPIC_AUTH_TOKEN`), bypassing whatever auth Claude Code would otherwise use. The gateway itself is the only thing that ever calls Anthropic, always using the real `ANTHROPIC_API_KEY` in `.env` (hardcoded per-model in `litellm/config.yaml`) — regardless of how any Claude Code session, builder included, is authenticated. That key needs its own funded API credits: a Claude.ai/Pro/Max subscription is a separate billing product from the standalone Anthropic API and doesn't fund it.

### How this gets tested

The real test is a bill comparison, not a unit test: run the same QA tasks (test plan, test execution, bug log, fix verification) once **routed** through llm-trunk and once **unrouted** direct to Anthropic with no gateway, then compare the two bills. That's what shows whether skill-tagged routing actually saves money — the routing logic itself is already covered by `decide()`'s own checks.

## Commands

Run from this repo's root, with `.env` filled in.

**Start / stop**

```bash
docker compose up -d              # start postgres + litellm
docker compose ps                 # confirm both containers are up
docker compose logs -f litellm    # watch it boot; catches callback import errors
docker compose down                # stop everything
```

`catalog.yaml` is re-read on every request — edits apply immediately. Editing anything under `policy/` or `litellm/config.yaml` needs `docker compose restart litellm` to take effect (both are only loaded once, at startup).

**Set up your client repo** (once, after the stack is up)

```bash
./scripts/create_qa_usage_key.sh
```

Copy `client-settings.json.example` to `<your-client-repo>/.claude/settings.json`, and paste the `key` field from the response above in place of the placeholder `ANTHROPIC_AUTH_TOKEN`. Claude Code only picks this up from the directory it's launched in — see [Lab design](#lab-design) for why that has to be a separate repo/session from this one.

**Hash a skill** (whenever a `SKILL.md` changes)

```bash
python3 scripts/hash_skill.py path/to/SKILL.md
```

**Call the proxy directly**, without Claude Code — `model` is whatever the client asks for; llm-trunk overrides it based on routing, not the other way around:

```bash
curl http://127.0.0.1:4000/v1/messages \
  -H "Authorization: Bearer <virtual-key>" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model": "claude-opus-5", "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]}'
```

**Check what happened**

```bash
docker compose logs litellm | grep "llm-trunk"                         # every allow/deny decision this callback logged, so far
docker compose logs -f litellm | grep --line-buffered "llm-trunk"      # same, live, while you test
docker compose exec postgres psql -U litellm -d litellm                 # LiteLLM's own spend/key tables; \dt to list them
```

## Files, in the order they come into play

### Setup phase

| File | Role |
|---|---|
| `scripts/hash_skill.py` | CLI: `python3 scripts/hash_skill.py <path/to/SKILL.md>` strips the file's YAML frontmatter and prints the `sha256:`/`bytes:` of the body, ready to paste into `catalog.yaml`. Rerun it whenever a `SKILL.md` changes, or its route goes stale. |
| `catalog.yaml` | The routing table. One row per tagged skill (sha256, bytes, bucket, alias, vendor, effort, `max_input`/`max_output`) plus the `untagged` fallback row. `sha256`/`bytes` describe the skill's *body* (frontmatter stripped) — the only part Claude Code sends verbatim — used to isolate that exact region from surrounding prompt text before hashing. `vendor` is `anthropic` for every row today, but is what lets `decide()` apply the right vendor's price-cliff check once a non-Anthropic route is added. |
| `docker-compose.yml` | Brings up the two containers: `postgres` (spend/virtual-key storage) and `litellm` (the proxy itself), mounting `litellm/config.yaml`, `policy/`, and `catalog.yaml` into the LiteLLM container. |
| `litellm/config.yaml` | LiteLLM's own config, loaded on boot: the `model_list` mapping each catalog alias to a real Anthropic model id and its per-token pricing, the `callbacks` entry registering `policy.litellm_callback.proxy_handler_instance`, and `general_settings` (master key, database URL). |
| `scripts/create_qa_usage_key.sh` | Run once the stack is up: calls LiteLLM's `/key/generate` admin endpoint to mint the `qa-usage`-labeled virtual key. Deliberately has no `models` restriction — LiteLLM checks a key's model allow-list against the client's *original* requested model, before the callback ever rewrites it, and Claude Code never sends our alias names directly. Access control is `decide()`'s job. The secret it returns — not anything in this repo — is what the client actually authenticates with. |
| `client-settings.json.example` | Template for `<your-client-repo>/.claude/settings.json` — the `env` block that points a Claude Code session at the gateway. Not `company-client`'s own copy (that repo is local-only and never pushed), so this is what anyone else adopting llm-trunk actually has to work from. |

### Per-request phase

| File | Role |
|---|---|
| `policy/litellm_callback.py` | The custom `CustomLogger` LiteLLM invokes on every request. `async_pre_call_hook` resolves the skill — `x-skill-id` + `x-skill-hash` headers (only honoured together) → Claude Code's `<command-name>` tag + `Base directory for this skill:` marker (the real invocation path) → raw frontmatter (fallback, for pasted/`@`-referenced content) → sticky session → untagged — calls `decide()`, then either raises an HTTP error (deny) or pins the model, effort, and `max_tokens` and remembers the skill for that conversation. `async_log_success_event` logs the outcome once Anthropic responds. |
| `policy/hash.py` | `sha256_hex()` — the single hashing entry point, used by the callback above (for both the extracted body and the conversation fingerprint) and `scripts/hash_skill.py`. Also `strip_frontmatter()`, used only by `scripts/hash_skill.py` to remove a `SKILL.md`'s YAML header before hashing (the callback locates the body a different way — via a regex match's end position — since it's working inside a larger string, not a lone file). |
| `policy/cliffs.py` | Vendor input-size price-cliff thresholds (Grok/Gemini at 200k, OpenAI Astra-class at 272k). Called from `decide()` on every request via each catalog row's `vendor` field — a no-op today since every row is `anthropic` (no cliff), but live and ready for the moment a row's vendor changes. |
| `policy/decide.py` | The routing policy itself, as a pure function: given the catalog, a candidate skill id/hash, any sticky skill from earlier turns, and an estimated input size, returns an allow/deny `Decision` with the alias, effort, and reason — checking for an unrecognized skill id, a stale hash, vendor price cliffs, and the row's `max_input` cap. No LiteLLM or network dependency. |
| `policy/__init__.py` | Empty — makes `policy/` an importable Python package. |
