# 🔀 llm-trunk

[![Version](https://img.shields.io/badge/ver.-0.1.0-1a1a2e)](https://github.com/pdudotdev/llm-trunk/releases/tag/v0.1.0)
[![License](https://img.shields.io/badge/license-GPLv3-1a1a2e)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/pdudotdev/llm-trunk?color=1a1a2e)](https://github.com/pdudotdev/llm-trunk/commits/master/)

Skill-tagged routing gateway built on [LiteLLM](https://docs.litellm.ai/). Routes each request to a model + effort level based on which skill was invoked — identified by the sha256 hash of that skill's `SKILL.md` body, not a prompt classifier.

▫️ **Same idea as an 802.1Q trunk port:**
- [x] **Tagged frame** → the VLAN ID picks the VLAN. Here: the skill hash picks that skill's model/effort lane
- [x] **Untagged frame** → goes to the native VLAN. Here: goes to `untagged` (Haiku, low effort)
- [x] **Allowed-VLAN list** → only listed VLANs get their own path. Here: `catalog.yaml` — a slash command not in it (e.g. `/compact`, a personal skill) rides `untagged`

▫️ **"VLAN hopping" protection:**
- [x] Can't fake a tag — a wrong hash for a skill is denied
- [x] Headers can't claim one — `x-skill-id`/`x-skill-hash` are only honored from a key with `metadata: {"trust_skill_headers": true}`; no key has it today
- [x] A forged body is handled per key, like per-port VLAN filtering — the hash isn't secret (anyone can read the skill files), so each key's `allowed_skills` metadata decides which lanes it can reach; anything else drops to `untagged`. The single `qa-usage` key is unrestricted on purpose (it needs every QA skill), so this becomes an active boundary once a narrower key exists

## 📖 **Table of Contents**
- 🔀 **llm-trunk**
  - [🔭 Overview](#-overview)
  - [🔀 How It Works](#-how-it-works)
  - [🧪 Example Session](#-example-session)
  - [🚀 Installation & Usage](#-installation--usage)
  - [📂 Project Files](#-project-files)
  - [⬆️ Planned Upgrades](#️-planned-upgrades)
  - [📄 Disclaimer](#-disclaimer)
  - [📜 License](#-license)
  - [📧 Hi](#-hi)

## 🔭 Overview

A local proxy between Claude Code and Anthropic on `127.0.0.1:4000`. LiteLLM does the proxying; this repo owns only the **routing policy** — which model and effort level a request gets, based on which QA skill (if any) was invoked.

▫️ **Key characteristics:**
- [x] **Skill-tagged routing** — the sha256 of a skill's `SKILL.md` body is the routing identity
- [x] **Bounded stickiness** — after a skill is invoked, later messages in the same Claude Code session keep its route until it expires: 10 minutes idle, or 30 minutes since the last invocation, whichever comes first. This stops "invoke a pricey skill once, then use it for everything". It can't stop someone re-typing the skill before every question — the key's budget cap is the backstop there
- [x] **Untagged fallback** — used when no skill is resolved: none invoked this turn and no live sticky route. Cheapest model, low effort
- [x] **Deny rules** — a stale hash, oversized input (per-lane `max_input`, counting system prompt + messages + tool definitions), or a vendor price cliff blocks the request before it reaches Anthropic
- [x] **Multi-vendor ready** — the price-cliff check runs on every request (a no-op for `vendor: anthropic`)
- [x] **Local lab, not production** — one Mac, Docker Compose, no auth beyond LiteLLM's own keys

▫️ **Model tiers:**
- [x] Opus 5 — test-plan creation
- [x] Sonnet 5 — test execution, bug logging
- [x] Haiku 4.5 — fix verification, untagged

## 🔀 How It Works

▫️ **One-time setup:**
- [x] Each skill's `SKILL.md` body (frontmatter stripped) is sha256-hashed — [`scripts/hash_skill.py`](scripts/hash_skill.py)
- [x] Hash, lane, effort and token caps go into [`catalog.yaml`](catalog.yaml) — the routing table
- [x] Docker Compose starts LiteLLM + Postgres, loading `litellm/config.yaml` (models, pricing, callback)
- [x] A budget-capped virtual key is minted via LiteLLM's admin API — the client never sees the real Anthropic key

▫️ **Every request:**
- [x] Claude Code sends the request to the proxy
- [x] The callback resolves the skill, in order: trusted headers → a skill invoked in the **newest user turn** (a typed `/skill`, or Claude calling its `Skill` tool) → sticky route → `untagged`. Older turns are ignored, since Claude Code resends the whole conversation every turn
- [x] The skill's hash is checked against `catalog.yaml`
- [x] **Allow** → model, effort and `max_tokens` are set to the lane's values, whatever the client asked for (Claude Code's own `/effort` is dropped). Mid-conversation `system` messages are moved into the user turn, since Haiku 4.5 rejects them
- [x] **Deny** → stale hash, oversized input or a vendor price cliff; the request never reaches Anthropic
- [x] After Anthropic responds, skill, lane, effort, tokens and cost are logged

## 🧪 Example Session

▫️ A real run of Claude Code in `company-client` through the gateway. The decision column is what the gateway logged.

| # | What the user does | Gateway decision |
|---|---|---|
| 1 | Asks a plain question in a new session | `untagged` → Haiku 4.5, low |
| 2 | Runs `/qa-test-plan-creation` | hash verified → Opus 5, high |
| 3 | Asks a follow-up, no skill | sticky → stays on Opus 5, high |
| 4 | Runs `/qa-bug-logging` in the same session | hash verified → switches to Sonnet 5, low |
| 5 | Runs `/qa-test-case-execution` | hash verified → Sonnet 5, medium |
| 6 | Runs `/qa-fix-verification` | hash verified → Haiku 4.5, low |
| 7 | Starts a new session, `@`-references a file | `untagged` — nothing carried over from the old session |
| 8 | Changes a word inside a skill, then runs it | **422** stale hash — never reaches Anthropic |
| 9 | Appends a line to a skill, then runs it | **422** stale hash — never reaches Anthropic |
| 10 | Pastes a ~200 KB log into an untagged session | **422** input cap (~89k estimated ≥ 64k) — never reaches Anthropic |

> ⚠️ **NOTE:** Switching lanes costs one uncached turn (~$0.13 vs ~$0.03 for a cached Sonnet/Opus turn here) — Anthropic's prompt cache doesn't carry across a model or effort change. Every deny is a 422 whose message Claude Code shows as-is, e.g. `API Error: 422 llm-trunk: ~89409 input tokens exceeds the untagged lane cap (64000) — run /compact or start a new session`.

## 🚀 Installation & Usage

▫️ **Prerequisites:**
- Docker (Compose v2)
- Python 3.11+
- API key(s)

▫️ **Step 1 - Clone & configure:**
```
git clone https://github.com/pdudotdev/llm-trunk
cd llm-trunk
cp .env.example .env
```
Fill in `.env`: `LITELLM_MASTER_KEY` (any strong string), `ANTHROPIC_API_KEY` (the **routed-client** workspace key), and `POSTGRES_PASSWORD` (`openssl rand -hex 24` — must be URL-safe, see `.env.example`).

▫️ **Step 2 - Start the stack:**
```
docker compose up -d
docker compose logs -f litellm    # confirm a clean boot
```

▫️ **Step 3 - Hash your skills:**
```
python3 scripts/hash_skill.py path/to/SKILL.md
```
Paste the printed `sha256`/`bytes` into `catalog.yaml` for each skill.

▫️ **Step 4 - Mint a virtual key:**
```
./scripts/create_qa_usage_key.sh
```
Copy [`client-settings.json.example`](client-settings.json.example) to `<your-client-repo>/.claude/settings.json` and replace the placeholder `ANTHROPIC_AUTH_TOKEN` with the returned `key`.

▫️ **Step 5 - Test:**
```
claude    # from your client repo — /your-skill-name to invoke a route
```
Watch the decisions:
```
docker compose logs -f litellm | grep --line-buffered "llm-trunk"
```

> ⚠️ **NOTE:** `catalog.yaml` is re-read on every request, so edits apply immediately. Changes under `policy/` or to `litellm/config.yaml` need `docker compose restart litellm`.

## 📂 Project Files

▫️ **Setup phase:**

| File | Role |
|---|---|
| [`scripts/hash_skill.py`](scripts/hash_skill.py) | Prints the `sha256`/`bytes` of a skill's body (frontmatter stripped) for `catalog.yaml`. Rerun whenever a `SKILL.md` changes. Refuses bodies using `$ARGUMENTS`, `$1`, `${CLAUDE_SKILL_DIR}`, `${CLAUDE_SESSION_ID}` or `` !`cmd` `` — Claude Code rewrites those when invoking, so the hash could never match. |
| [`catalog.yaml`](catalog.yaml) | The routing table — one row per skill (sha256, bytes, bucket, alias, vendor, effort, caps) plus `untagged`. `vendor` drives the price-cliff check. |
| [`docker-compose.yml`](docker-compose.yml) | Starts `postgres` (spend/key storage) and `litellm` (the proxy), mounting `litellm/config.yaml`, `policy/` and `catalog.yaml`. LiteLLM is pinned by digest (1.102.0), since the callback relies on version-specific internals. |
| [`litellm/config.yaml`](litellm/config.yaml) | LiteLLM's config: `model_list` (alias → real model + pricing), the `callbacks` entry for the routing policy, and `general_settings` (master key, database URL). |
| [`scripts/create_qa_usage_key.sh`](scripts/create_qa_usage_key.sh) | Mints the `qa-usage` virtual key with a budget cap (`max_budget`/`budget_duration`). No `models` restriction — LiteLLM checks it against the client's original model, before the callback rewrites it, so it would block every request. Per-skill access is `allowed_skills` in key metadata, omitted here since QA needs every QA skill. |
| [`client-settings.json.example`](client-settings.json.example) | Template for `<your-client-repo>/.claude/settings.json` — points Claude Code at the gateway. |

▫️ **Per-request phase:**

| File | Role |
|---|---|
| [`policy/litellm_callback.py`](policy/litellm_callback.py) | The LiteLLM callback run on every request. Resolves the skill (see [How It Works](#-how-it-works)); the body is hashed over exactly the catalog's `bytes` and must end there, so any edit — appended lines included — is denied as stale until re-hashed. Applies the key's `allowed_skills` (a malformed value restricts the key to `untagged`), estimates input size, calls `decide()`, then sets model/effort/`max_tokens`. Tracks sticky routes per Claude Code `session_id` with the 10/30-minute expiry. Logs the outcome, including estimated vs real input tokens. |
| [`policy/hash.py`](policy/hash.py) | `sha256_hex()`, used by the callback and `scripts/hash_skill.py`. `strip_frontmatter()` (BOM- and CRLF-aware, like Claude Code) and the substitution check, used by `scripts/hash_skill.py`. |
| [`policy/cliffs.py`](policy/cliffs.py) | Vendor price-cliff thresholds (Grok/Gemini 200k, OpenAI 272k). Checked by `decide()` on every request; a no-op for Anthropic. |
| [`policy/decide.py`](policy/decide.py) | The routing policy as a pure function: catalog + skill id/hash + sticky skill + estimated input size → allow/deny with lane, effort and reason. Checks the hash, price cliffs and `max_input` (plus unknown skill ids, reachable only via trusted headers). No LiteLLM or network dependency. |
| `policy/__init__.py` | Empty — makes `policy/` a Python package. |

## ⬆️ Planned Upgrades
- [ ] Two LiteLLM instances sharing sticky state via Redis
- [ ] Non-Anthropic vendors in `catalog.yaml`, exercising `policy/cliffs.py`
- [ ] Per-department virtual keys (`sk-dev-usage`), each scoped with `allowed_skills`

## 📄 Disclaimer
You're responsible for creating your own API keys, funding your credits, and validating `catalog.yaml` against your own skill files before routing real traffic through llm-trunk.

## 📜 License
Licensed under the [**GNU General Public License v3.0**](LICENSE).

## 📧 Hi
Wanna say hello? DM me on [**LinkedIn**](https://www.linkedin.com/in/tmihaicatalin/).
