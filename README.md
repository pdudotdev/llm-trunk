# 🔀 llm-trunk

[![Version](https://img.shields.io/badge/ver.-0.1.0-1a1a2e)](https://github.com/pdudotdev/llm-trunk/releases/tag/v0.1.0)
[![License](https://img.shields.io/badge/license-GPLv3-1a1a2e)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/pdudotdev/llm-trunk?color=1a1a2e)](https://github.com/pdudotdev/llm-trunk/commits/master/)

Skill-tagged routing gateway for Claude Code, built on [LiteLLM](https://docs.litellm.ai/). Routes traffic to a specific Anthropic model + effort level based on which QA skill was invoked — identified by the sha256 hash of that skill's `SKILL.md` body, not a prompt classifier.

## 📖 **Table of Contents**
- 🔀 **llm-trunk**
  - [🔭 Overview](#-overview)
  - [🔀 How It Works](#-how-it-works)
  - [🧪 Lab Design](#-lab-design)
  - [🚀 Installation & Usage](#-installation--usage)
  - [📂 Project Files](#-project-files)
  - [⬆️ Planned Upgrades](#️-planned-upgrades)
  - [📄 Disclaimer](#-disclaimer)
  - [📜 License](#-license)
  - [📧 Hi](#-hi)

## 🔭 Overview

A local reverse proxy sitting between Claude Code and Anthropic on `127.0.0.1:4000`. LiteLLM does the actual proxying; this repo owns only the **routing policy** — which model and effort level a request gets, based on which QA skill (if any) was invoked.

▫️ **Key characteristics:**
- [x] **Skill-tagged routing** — sha256 of a skill's `SKILL.md` body is the routing identity, not a prompt classifier
- [x] **Session stickiness** — a tagged route persists for the rest of that conversation, no re-invoking every turn
- [x] **Untagged fallback** — anything unrecognized routes to the cheapest model, low effort
- [x] **Deny levers** — stale hash, unrecognized skill id, oversized input, or a vendor price cliff all block the request before it reaches Anthropic
- [x] **Multi-vendor ready** — the price-cliff check runs on every decision today (`vendor: anthropic` = no-op), ready for a non-Anthropic route
- [x] **Local lab, not production** — one Mac, Docker Compose, no auth beyond LiteLLM's own keys

▫️ **Model tiers:**
- [x] Opus 5 — test-plan creation
- [x] Sonnet 5 — test execution, bug logging
- [x] Haiku 4.5 — fix verification, untagged fallback

## 🔀 How It Works

▫️ **One-time setup:**
- [x] Each skill's `SKILL.md` body (frontmatter stripped) gets sha256-hashed — [`scripts/hash_skill.py`](scripts/hash_skill.py)
- [x] Hash + bucket + alias + effort + token caps recorded per skill in [`catalog.yaml`](catalog.yaml) — the single routing table
- [x] Docker Compose brings up LiteLLM + Postgres, loading `litellm/config.yaml` (model list, pricing, callback registration)
- [x] A scoped virtual key is minted via LiteLLM's own admin API — the client never sees the real Anthropic key

▫️ **Every live request:**
- [x] Claude Code POSTs to the proxy, invoking a skill or not
- [x] A custom LiteLLM callback resolves the skill — headers → Claude Code's `<command-name>` tag (the real invocation path) → raw frontmatter (fallback, for pasted/`@`-referenced content) → sticky session → untagged
- [x] The resolved skill id + hash is checked against `catalog.yaml`
- [x] **Allow** → model, effort, and `max_tokens` are overridden to the resolved route, regardless of what the client asked for
- [x] **Deny** → unrecognized skill id, stale hash, oversized input, or a crossed vendor price cliff — the request never reaches Anthropic
- [x] Once Anthropic responds, skill/alias/effort/tokens/cost get logged, for routed-vs-unrouted bill comparisons

## 🧪 Lab Design

Personal cost-routing lab, not a production deployment — one Mac, no auth beyond LiteLLM's own keys, stops the moment the Mac sleeps.

▫️ **Two repos, one machine:**
- [x] **llm-trunk** (this repo) — the gateway: compose stack, LiteLLM config, `policy/`, `catalog.yaml`
- [x] **company-client** (local-only, never pushed) — an emulated "employee checkout": four QA skills at `.claude/skills/<name>/SKILL.md` + a `.claude/settings.json` pointing at the gateway

▫️ **Three separate Anthropic workspaces**, so each produces a clean, comparable bill:
- [x] **Builder** — this Claude Code session; never goes through the gateway
- [x] **Routed-client** — the only Anthropic key llm-trunk holds (`.env`); every tagged/untagged request bills here
- [x] **Unrouted-baseline** — a separate Claude Code profile, same prompts, no gateway — the "what if we hadn't routed" control group

▫️ **Claude Code's own auth only matters for Builder** — `company-client` bypasses it entirely via `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`. Only the gateway's own `ANTHROPIC_API_KEY` in `.env` ever calls Anthropic, and it needs real funded API credits — a Claude.ai subscription doesn't cover it.

▫️ **How this gets tested:** run the same QA tasks once routed through llm-trunk, once unrouted direct to Anthropic, then compare the two bills. `decide()`'s own checks already cover routing correctness — the bill comparison is what proves the savings.

## 🚀 Installation & Usage

▫️ **Prerequisites:**
- Docker (Compose v2)
- Python 3.11+
- An Anthropic API key with funded credits

▫️ **Step 1 - Clone & configure:**
```
git clone https://github.com/pdudotdev/llm-trunk
cd llm-trunk
cp .env.example .env
```
Fill in `.env`: `LITELLM_MASTER_KEY` (any strong string), `ANTHROPIC_API_KEY` (the **routed-client** workspace key), and `POSTGRES_PASSWORD` (`openssl rand -hex 24` — must be URL-safe, see the comment in `.env.example`).

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
Copy [`client-settings.json.example`](client-settings.json.example) to `<your-client-repo>/.claude/settings.json`, and paste the returned `key` in place of the placeholder `ANTHROPIC_AUTH_TOKEN`.

▫️ **Step 5 - Test:**
```
claude    # from your client repo — /your-skill-name to invoke a route
```
Watch it land:
```
docker compose logs -f litellm | grep --line-buffered "llm-trunk"
```

> ⚠️ **NOTE:** `catalog.yaml` is re-read on every request — edits apply immediately. Editing anything under `policy/` or `litellm/config.yaml` needs `docker compose restart litellm` (both are only loaded once, at startup).

## 📂 Project Files

▫️ **Setup phase:**

| File | Role |
|---|---|
| [`scripts/hash_skill.py`](scripts/hash_skill.py) | Strips frontmatter, prints the `sha256`/`bytes` of a skill's body for `catalog.yaml`. Rerun whenever a `SKILL.md` changes. |
| [`catalog.yaml`](catalog.yaml) | The routing table — one row per skill (sha256, bytes, bucket, alias, vendor, effort, caps) plus `untagged`. `sha256`/`bytes` describe the *body* (frontmatter stripped), the only part Claude Code sends verbatim. `vendor` drives `decide()`'s price-cliff check (a no-op until a non-Anthropic route exists). |
| [`docker-compose.yml`](docker-compose.yml) | Brings up `postgres` (spend/key storage) and `litellm` (the proxy), mounting `litellm/config.yaml`, `policy/`, and `catalog.yaml` into the container. |
| [`litellm/config.yaml`](litellm/config.yaml) | LiteLLM's own config: `model_list` (alias → real model + pricing), the `callbacks` entry registering our routing policy, and `general_settings` (master key, database URL). |
| [`scripts/create_qa_usage_key.sh`](scripts/create_qa_usage_key.sh) | Mints the `qa-usage` virtual key via LiteLLM's `/key/generate`. Deliberately unrestricted — LiteLLM checks a key's model allow-list against the client's *original* model, before the callback rewrites it, so restricting it blocks every real request. `decide()` is the actual access control. |
| [`client-settings.json.example`](client-settings.json.example) | Template for `<your-client-repo>/.claude/settings.json` — the `env` block pointing Claude Code at the gateway. `company-client`'s own copy is local-only, so this is what any other adopter works from. |

▫️ **Per-request phase:**

| File | Role |
|---|---|
| [`policy/litellm_callback.py`](policy/litellm_callback.py) | The `CustomLogger` LiteLLM invokes on every request. Resolves the skill — headers → `<command-name>` tag + `Base directory` marker (real path) → raw frontmatter (fallback) → sticky session → untagged — calls `decide()`, then denies or pins model/effort/`max_tokens` and remembers the skill for that conversation. Logs the outcome once Anthropic responds. |
| [`policy/hash.py`](policy/hash.py) | `sha256_hex()` — the single hashing entry point, used by the callback (body hash + conversation fingerprint) and `scripts/hash_skill.py`. `strip_frontmatter()`, used only by `scripts/hash_skill.py` (the callback locates the body via a regex match's end position instead). |
| [`policy/cliffs.py`](policy/cliffs.py) | Vendor input-size price-cliff thresholds (Grok/Gemini 200k, OpenAI Astra-class 272k). Called from `decide()` on every request — a no-op today since every row is `anthropic`, but live and ready for a non-Anthropic vendor. |
| [`policy/decide.py`](policy/decide.py) | The routing policy, as a pure function: catalog + candidate skill id/hash + sticky skill + estimated input size → an allow/deny `Decision` with alias, effort, reason — checking for an unrecognized skill id, a stale hash, price cliffs, and the `max_input` cap. No LiteLLM or network dependency. |
| `policy/__init__.py` | Empty — makes `policy/` an importable Python package. |

## ⬆️ Planned Upgrades
- [ ] Live unrouted-baseline comparison run, with a documented $ delta
- [ ] A real non-Anthropic vendor added to `catalog.yaml` (first live exercise of `policy/cliffs.py`)
- [ ] Per-department virtual keys (`sk-dev-usage`, `sk-hr-usage`, ...) if a second real consumer shows up

## 📄 Disclaimer
You're responsible for funding your own Anthropic API credits, keeping the three workspace keys separate, and validating `catalog.yaml` against your own skill files before routing real traffic through this.

## 📜 License
Licensed under the [**GNU General Public License v3.0**](LICENSE).

## 📧 Hi
Wanna say hello? Send me a DM at [**LinkedIn**](https://www.linkedin.com/in/tmihaicatalin/).
