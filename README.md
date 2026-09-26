# 🔀 llm-trunk

[![Version](https://img.shields.io/badge/ver.-0.2.0-1a1a2e)](https://github.com/pdudotdev/llm-trunk/releases)
[![License](https://img.shields.io/badge/license-GPLv3-1a1a2e)](LICENSE)
[![Tests](https://github.com/pdudotdev/llm-trunk/actions/workflows/tests.yml/badge.svg)](https://github.com/pdudotdev/llm-trunk/actions/workflows/tests.yml)
[![Last Commit](https://img.shields.io/github/last-commit/pdudotdev/llm-trunk?color=1a1a2e)](https://github.com/pdudotdev/llm-trunk/commits/master/)

Tier-based routing gateway for Claude Code, built on [LiteLLM](https://docs.litellm.ai/) — and a way to **measure what the routing saves**. Each request goes to a model tier based on what it is: a registered skill (identified by the sha256 of its `SKILL.md`, not a prompt classifier), a subagent, compaction, Claude Code's own background calls, or plain chat. A live dashboard and a scripted scenario show the cost of every request next to what it would have cost on the model Claude Code asked for.

▫️ **Same idea as an 802.1Q trunk port:**
- [x] **Tagged frame** → the VLAN ID picks the VLAN. Here: a registered skill's hash picks its catalog tier (complex / moderate / light)
- [x] **Untagged frame** → goes to the native VLAN. Here: plain chat goes to the lowest tier (Haiku, low effort)
- [x] **Allowed-VLAN list** → only listed VLANs get their own path. Here: `catalog.yaml` — an unregistered skill gets no tier of its own and goes to the lowest

```
   registered skills      unregistered skills        subagents        compaction ·
    (catalog.yaml)          and plain chat                           background calls
__________▼_____________________▼______________________▼___________________▼__________
\                    │                     │                     │                    /
 \  hash verified →  │  lowest tier, or    │  one tier below     │  stay on the      /
  \  catalog tier    │  the sticky tier    │  the session        │  session's tier  /
   \_________________│_____________________│_____________________│_________________/
            │                   │                    │                   │
            └───────────────────┴─────────┬──────────┴───────────────────┘
                     ┌────────────────────┼────────────────────┐
                     ▼                    ▼                    ▼
                  complex              moderate              light
                 Opus 5.5 ·           Sonnet 5 ·          Haiku 4.5 ·
                   high                 medium                low
                     └────────────────────┼────────────────────┘
                                          ▼
                                      Anthropic
```

▫️ **"VLAN hopping" protection:**
- [x] Can't fake a tag — a registered skill with a wrong hash is denied
- [x] Headers can't claim one — `x-skill-id`/`x-skill-hash` are only honored from a key with `metadata: {"trust_skill_headers": true}`; no key has it today
- [x] A forged body is handled per key, like per-port VLAN filtering — the hash isn't secret (anyone can read the skill files), so each key's `allowed_skills` metadata decides which skills can lift it above the lowest tier; anything else is treated as unregistered

## 📖 **Table of Contents**
- 🔀 **llm-trunk**
  - [🔭 Overview](#-overview)
  - [🔀 How It Works](#-how-it-works)
  - [🧪 Example Session](#-example-session)
  - [🚀 Installation & Usage](#-installation--usage)
  - [📊 Measuring It](#-measuring-it)
  - [💡 Concepts 101](#-concepts-101)
  - [📂 Project Files](#-project-files)
  - [⬆️ Planned Upgrades](#️-planned-upgrades)
  - [📄 Disclaimer](#-disclaimer)
  - [📜 License](#-license)
  - [📧 Hi](#-hi)

## 🔭 Overview

A local proxy between Claude Code and Anthropic on `127.0.0.1:4000`. LiteLLM does the proxying; this repo owns the **routing policy** — which tier (model + effort) each request gets — and the tools that measure its cost.

▫️ **Tiers** (cheapest first; each tier's model is one line in `litellm/config.yaml`):
- [x] `light` — Haiku 4.5, low effort, 64k input cap
- [x] `moderate` — Sonnet 5, medium effort, 128k input cap
- [x] `complex` — Opus 5.5, high effort, 180k input cap

▫️ **Routing rules:**

| Request type | What it is | Tier |
|---|---|---|
| **normal** | Your own prompts | The session's sticky tier, else `light` |
| **skill** (registered) | A catalog skill, hash verified | Its catalog tier — and it becomes the session's sticky tier |
| **skill** (unregistered) | Any other skill | `light` — even mid-session, and follow-ups stay there |
| **subagent** | Requests from a subagent Claude Code spawns | One tier below the session (never below `light`); each subagent keeps its tier |
| **compaction** | `/compact` or auto-compaction | The session's tier — never capped, so it can always shrink an oversized session |
| **background** | Suggestions, away-summary recaps | The session's tier (they carry the whole conversation, so moving them would re-cache it) |
| | Session titles | `light` (~1k tokens, nothing cached) |
| | Auto mode's permission checks | Passed through untouched — Claude Code chooses that model on purpose |

▫️ **Key characteristics:**
- [x] **Bounded stickiness** — after a registered skill runs, later messages in the same Claude Code session keep its tier until it expires: 10 minutes idle, or 30 minutes since the last invocation, whichever comes first. Background calls, subagents and compaction never refresh it
- [x] **Deny rules** — a stale hash, oversized input (per-tier `max_input`, estimated over system prompt + messages + tool definitions), or a vendor price cliff returns a 422 with the reason, before anything reaches Anthropic
- [x] **Measured** — every request logs its type, tier, tokens (cache reads and writes), cost, the model that answered and the model Claude Code asked for
- [x] **Multi-vendor ready** — the price-cliff check runs on every request (a no-op for `vendor: anthropic`)
- [x] **Local lab, not production** — one Mac, Docker Compose, no auth beyond LiteLLM's own keys

## 🔀 How It Works

▫️ **One-time setup:**
- [x] Each skill's `SKILL.md` body (frontmatter stripped) is sha256-hashed — [`scripts/hash_skill.py`](scripts/hash_skill.py)
- [x] Hash, byte length and tier go into [`catalog.yaml`](catalog.yaml), next to the tiers' effort and token caps
- [x] Docker Compose starts LiteLLM + Postgres, loading `litellm/config.yaml` (one model per tier, pricing, callback)
- [x] A budget-capped virtual key is minted via LiteLLM's admin API — the client never sees the real Anthropic key

▫️ **Every request** — the callback classifies it, in this order (all signals checked on the wire):
- [x] **subagent** — it carries Claude Code's `x-claude-code-agent-id` header
- [x] **compaction** — the newest user turn ends with Claude Code's compaction instruction. Checked *before* skills: compaction resends the turn it compacts, which may itself be a skill invocation
- [x] **background** — session titles, suggestions and recaps (recognized by the fixed text Claude Code sends), and auto mode's permission checks
- [x] **skill** — a skill invoked in the **newest user turn** (a typed `/skill`, or Claude calling its `Skill` tool); older turns are ignored, since Claude Code resends the whole conversation. Registered = in the catalog, hash matching, allowed for the key. Built-in commands like `/model` carry no skill body and don't count
- [x] **normal** — everything else
- [x] **Allow** → model and effort are set to the tier's, whatever the client asked for (Claude Code's own `/effort` is dropped), and `max_tokens` is capped at the tier's `max_output`. Mid-conversation `system` messages are moved into the user turn, since Haiku 4.5 rejects them
- [x] **Deny** → stale hash, oversized input or a vendor price cliff: a 422 with the reason; the request never reaches Anthropic
- [x] After Anthropic responds, one JSON line is logged: request type, tier, session tier, skill, tokens (including cache reads and writes), cost, the model that answered and the one asked for — upstream failures (e.g. a 400 or 529) too

> ⚠️ **NOTE:** Background and compaction detection matches text Claude Code sends, so a future rewording would make those requests look like normal turns — routed to the session's tier and refreshing its timer, the fallback, never worse.

## 🧪 Example Session

▫️ What the gateway decides in a Claude Code session in the demo client repo — each row is also an automated test in [`tests/test_example_session.py`](tests/test_example_session.py).

| # | What the user does | Gateway decision |
|---|---|---|
| 1 | Asks a plain question in a new session | normal → `light` (Haiku 4.5, low) |
| 2 | Runs `/design-review` | hash verified → `complex` (Opus 5.5, high), sticky |
| 3 | Asks a follow-up, no skill | normal → stays on `complex` |
| 4 | Runs `/change-review` | hash verified → switches to `moderate` (Sonnet 5, medium) |
| 5 | Asks Claude to use a subagent | subagent → `light`, one tier below `moderate` |
| 6 | Runs `/personal-notes` (not in the catalog) | unregistered → `light`, and the follow-up stays there |
| 7 | Runs `/compact` in a `complex` session that's over the cap | compaction → stays on `complex`, not capped |
| 8 | Edits or appends to a skill, then runs it | **422** stale hash — never reaches Anthropic |
| 9 | Pastes a ~200 KB log into a `light` session | **422** input cap (≥ 64k) — never reaches Anthropic |
| 10 | Starts a new session | nothing carried over from the old one |

> ⚠️ **NOTE:** Switching to a tier with a different model or effort costs one uncached turn — Anthropic's prompt cache is per model and settings (see [Concepts 101](#-concepts-101)). Subagents are the cheapest place to downgrade: they start a fresh context anyway.

> ⚠️ **NOTE:** Claude Code shows a deny's message as-is, e.g. `API Error: 422 llm-trunk: ~89409 input tokens exceeds the light tier cap (64000) — run /compact or start a new session`.

## 🚀 Installation & Usage

▫️ **Prerequisites:**
- Docker (Compose v2)
- Python 3.11+ (`pip install -r requirements-dev.txt` for the dashboard, scenario runner and tests)
- API key(s)

▫️ **Step 1 - Clone & configure:**
```
git clone https://github.com/pdudotdev/llm-trunk
cd llm-trunk
cp .env.example .env
```
Fill in `.env`: `LITELLM_MASTER_KEY` (any strong string), `ANTHROPIC_API_KEY` (the key the gateway calls Anthropic with — a dedicated workspace makes its spend easy to track), and `POSTGRES_PASSWORD` (`openssl rand -hex 24` — must be URL-safe, see `.env.example`).

▫️ **Step 2 - Start the stack:**
```
docker compose up -d
docker compose logs -f litellm    # confirm a clean boot
```

▫️ **Step 3 - Register your skills:**
```
python3 scripts/hash_skill.py path/to/SKILL.md
```
Add the skill to `catalog.yaml` with the printed `sha256`/`bytes` and a `tier`. Unregistered skills still work — they just run on the lowest tier.

▫️ **Step 4 - Mint a virtual key:**
```
./scripts/create_qa_usage_key.sh
```
Copy [`client-settings.json.example`](client-settings.json.example) to `<your-client-repo>/.claude/settings.json` and replace the placeholder `ANTHROPIC_AUTH_TOKEN` with the returned `key`.

▫️ **Step 5 - Use it:**
```
claude    # from your client repo — /your-skill-name to invoke a tier
python3 scripts/watch.py              # live routing log (last 10 minutes, then live; --since 1h for more)
```

> ⚠️ **NOTE:** `catalog.yaml` is re-read on every request, so edits apply immediately — and a mistake in it breaks every request (the tests validate it). Changes under `policy/` or to `litellm/config.yaml` need `docker compose restart litellm`.

## 📊 Measuring It

▫️ **Live dashboard** — `python3 scripts/dashboard.py`: savings vs the models Claude Code asked for, spend by request type (flagging any type that costs more than asked), per-session prompt-cache countdowns with the price of the next message warm vs cold, and a live feed.

▫️ **Scripted scenario** — `python3 scenarios/run.py` drives a real, interactive Claude Code session through the gateway ([`scenarios/dev-day.yaml`](scenarios/dev-day.yaml): every request type on every tier, plus an idle gap past the cache). Each step is confirmed in Claude Code's own transcript, its answer is checked, its tier verified, and the whole session is run through the rule checker. `--quick` skips the idle gap; `--cold-start` first waits for the prompt cache to expire, so runs are comparable.

▫️ **Rule checker** — `python3 scripts/check_rules.py` checks every logged request against the routing rules above, including that the model that answered is the tier's. Exits non-zero on any violation.

▫️ **Cost report** — `python3 scripts/report.py` totals the gateway log per request type, tier, day and session.

▫️ **Tests** — `python3 -m pytest` (also run by CI on every push).

## 💡 Concepts 101

▫️ **Prompt caching — the `(c)` tag in the watcher**

Anthropic caches the start of each request: Claude Code marks the tool definitions, system prompt and conversation so far as cacheable. The cache doesn't need the whole request to match — it matches the **longest identical beginning**. A conversation only ever grows **at the end**, so every request starts with exactly what the previous one sent:

| Request | Contents | From cache | Processed fresh |
|---|---|---|---|
| Turn 1 | tools + system + **msg 1** | nothing (first time) | everything — and it's stored |
| Turn 2 | tools + system + msg 1 + reply 1 + **msg 2** | everything up to msg 1 | reply 1 + msg 2 — stored too |
| Turn 3 | … + msg 2 + reply 2 + **msg 3** | everything up to msg 2 | reply 2 + msg 3 |

Each turn reads everything earlier from cache and pays full price only for the small new tail — which gets stored for the next turn. In a real session, a `what severity?` turn had 55,427 input tokens, of which 55,173 came from cache: only 254 were new, so it cost $0.012 instead of ~$0.14.

- **Price:** a cache read costs **0.1×** the normal input price (0.05× on Opus 5.5); the first write costs **1.25×**
- **Expiry:** ~5 minutes without use (each hit resets it) — separate from llm-trunk's 10-minute sticky timer
- **What breaks it** (the next turn pays full price once): a change to anything *earlier* in the request (e.g. `/compact` rewriting history), a different model (each model has its own cache), or different thinking/effort settings — so switching tiers costs one uncached turn
- **Shared across sessions:** Claude Code's tools and system prompt are the same in every session, so a new session started within ~5 minutes of another reads them from cache

▫️ **Claude Code's own requests — the `(i)` tag in the watcher**

Besides your own turns, Claude Code sends requests of its own. llm-trunk recognizes these, identified from live traffic:

| Call | When it's sent | How it's recognized | Tier |
|---|---|---|---|
| **Session naming** | Once per session, after your first typed messages | No tools, Claude Code's `<session>` title prompt | `light` |
| **Next-prompt suggestion** | A few seconds after a reply | Last message starts with `[SUGGESTION MODE:` | session's |
| **Away-summary recap** | ~3 minutes after you leave the terminal window | Last message starts with *"The user stepped away and is coming back."* | session's |
| **Permission check** | Before actions, in auto mode | Claude Code's security-monitor system prompt, no tools | passed through |

None of them start or refresh a sticky timer — otherwise they'd keep a pricey tier alive with no one working. Recaps can be turned off per user in Claude Code's `/config`.

## 📂 Project Files

▫️ **Setup phase:**

| File | Role |
|---|---|
| [`scripts/hash_skill.py`](scripts/hash_skill.py) | Prints the `sha256`/`bytes` of a skill's body (frontmatter stripped) for `catalog.yaml`. Rerun whenever a `SKILL.md` changes. Refuses bodies using `$ARGUMENTS`, `$1`, `${CLAUDE_SKILL_DIR}`, `${CLAUDE_SESSION_ID}` or `` !`cmd` `` — Claude Code rewrites those when invoking, so the hash could never match. |
| [`catalog.yaml`](catalog.yaml) | The routing table: tier `order` (cheapest first, explicit so reordering the file can't invert routing), each tier's effort and caps (and `vendor` for the price-cliff check), and the registered skills with their hash and tier. |
| [`litellm/config.yaml`](litellm/config.yaml) | LiteLLM's config: one `model_list` entry per tier (the model it runs, pricing), the `callbacks` entry for the routing policy, and `general_settings` (master key, database URL). |
| [`pricing.yaml`](pricing.yaml) | Anthropic list prices per model, used to price "what it would have cost on the model asked for". |
| [`docker-compose.yml`](docker-compose.yml) | Starts `postgres` (spend/key storage) and `litellm` (the proxy), mounting `litellm/config.yaml`, `policy/` and `catalog.yaml`. LiteLLM is pinned by digest (1.102.0), since the callback relies on version-specific internals. |
| [`scripts/create_qa_usage_key.sh`](scripts/create_qa_usage_key.sh) | Mints the `qa-usage` virtual key with a budget cap (`max_budget`/`budget_duration`). No `models` restriction — LiteLLM checks it against the client's original model, before the callback rewrites it, so it would block every request. Per-skill access is `allowed_skills` in key metadata. |
| [`client-settings.json.example`](client-settings.json.example) | Template for `<your-client-repo>/.claude/settings.json` — points Claude Code at the gateway. |

▫️ **Per-request phase:**

| File | Role |
|---|---|
| [`policy/litellm_callback.py`](policy/litellm_callback.py) | The LiteLLM callback run on every request. Classifies it (see [How It Works](#-how-it-works)); a skill body is hashed over exactly the catalog's `bytes` and must end there, so any edit — appended lines included — is denied as stale until re-hashed. Applies the key's `allowed_skills`, estimates input size, calls `decide()`, then sets model/effort/`max_tokens`. Tracks sticky tiers per Claude Code session (10/30-minute expiry) and each subagent's tier. Logs each outcome as one JSON line — `spend`, `deny`, `expired` or `failed`. |
| [`policy/decide.py`](policy/decide.py) | The routing policy as a pure function: catalog + request type + session tier (+ skill id/hash, subagent tier) + estimated input size → allow/deny with tier, effort and reason. No LiteLLM or network dependency. |
| [`policy/hash.py`](policy/hash.py) | `sha256_hex()`, used by the callback and `scripts/hash_skill.py`. `strip_frontmatter()` (BOM- and CRLF-aware, like Claude Code) and the substitution check. |
| [`policy/cliffs.py`](policy/cliffs.py) | Vendor price-cliff thresholds (Grok/Gemini 200k, OpenAI 272k). A no-op for Anthropic. |

▫️ **Monitoring and measuring:**

| File | Role |
|---|---|
| [`scripts/watch.py`](scripts/watch.py) | Live, color-coded log of every routing decision — request type, tier, skill, model, effort, sticky time left, input tokens (`(c)` when served from cache) and cost. Stdlib-only; `NO_COLOR=1` disables colors. |
| [`scripts/dashboard.py`](scripts/dashboard.py) | Live dashboard (needs `rich`): savings vs requested models, spend by request type, prompt-cache clocks per session, live feed. |
| [`scripts/report.py`](scripts/report.py) | Cost totals per request type, tier, day and session, plus estimate accuracy. |
| [`scripts/check_rules.py`](scripts/check_rules.py) | Checks logged requests against the routing rules; non-zero exit on a violation. |
| [`scenarios/run.py`](scenarios/run.py) · [`scenarios/dev-day.yaml`](scenarios/dev-day.yaml) | Scripted Claude Code session through the gateway, with per-step cost, answer checks, tier checks and the rule check. Results go to `scenarios/results/`. |
| [`tests/`](tests/) | Unit and contract tests for the policy, the callback, the catalog and every tool above. |

## ⬆️ Planned Upgrades
- [ ] Benchmark table: the same scenario routed vs pass-through, repeated, from a cold cache
- [ ] Per-agent exceptions for subagents (e.g. a planning agent keeping its parent's tier)
- [ ] Cache-aware switching: only drop to a cheaper tier when the savings repay the re-cache
- [ ] Per-department virtual keys, each scoped with `allowed_skills`, and showback per key
- [ ] Non-Anthropic vendors in `catalog.yaml`, exercising `policy/cliffs.py`

## 📄 Disclaimer
You're responsible for creating your own API keys, funding your credits, and validating `catalog.yaml` against your own skill files before routing real traffic through llm-trunk.

## 📜 License
Licensed under the [**GNU General Public License v3.0**](LICENSE).

## 📧 Hi
Wanna say hello? DM me on [**LinkedIn**](https://www.linkedin.com/in/tmihaicatalin/).
