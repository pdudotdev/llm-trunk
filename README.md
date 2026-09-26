# 🔀 llm-trunk

[![Version](https://img.shields.io/badge/ver.-0.2.0-1a1a2e)](https://github.com/pdudotdev/llm-trunk/releases)
[![License](https://img.shields.io/badge/license-GPLv3-1a1a2e)](LICENSE)
[![Tests](https://github.com/pdudotdev/llm-trunk/actions/workflows/tests.yml/badge.svg)](https://github.com/pdudotdev/llm-trunk/actions/workflows/tests.yml)
[![Last Commit](https://img.shields.io/github/last-commit/pdudotdev/llm-trunk?color=1a1a2e)](https://github.com/pdudotdev/llm-trunk/commits/master/)

A routing gateway for Claude Code, built on [LiteLLM](https://docs.litellm.ai/). It sends each request to a cheaper or stronger model tier based on what the request is, and measures what that saves.

A request can be a registered skill (recognized by the SHA-256 hash of its `SKILL.md`, not by guessing from the prompt), a subagent, compaction, one of Claude Code's own background calls, or plain chat. A live dashboard and a scripted scenario show each request's cost next to what it would have cost on the model Claude Code asked for.

▫️ **Same idea as an 802.1Q trunk port:**
- [x] **Tagged frame** → the VLAN ID picks the VLAN. Here: a registered skill's hash picks its tier (complex / moderate / light)
- [x] **Untagged frame** → goes to the native VLAN. Here: plain chat goes to the lowest tier (Haiku, low effort)
- [x] **Allowed-VLAN list** → only listed VLANs get their own path. Here: `catalog.yaml`. A skill that isn't listed goes to the lowest tier

```
  registered skills    plain chat      unregistered       subagents      compaction,       permission
   (catalog.yaml)                     skills, titles                    suggestions,         checks
                                                                           recaps
__________▼_________________▼________________▼________________▼_______________▼_________________▼__________
\   hash verified   │ sticky tier,  │   lowest tier  │ one tier below │ the session's │   tier running    /
 \ → catalog tier   │  else lowest  │                │   the session  │     tier      │     the model    /
  \                 │               │                │   (then kept)  │               │     asked for   /
   \________________│_______________│________________│________________│_______________│________________/
          │                 │                │                │               │                 │
          └─────────────────┴────────────────┴───────┬────────┴───────────────┴─────────────────┘
                                ┌────────────────────┼────────────────────┐
                                ▼                    ▼                    ▼
                             complex              moderate              light
                            Opus 5.5 ·           Sonnet 5 ·          Haiku 4.5 ·
                               high                medium                low
                                └────────────────────┼────────────────────┘
                                                     ▼
                                                 Anthropic
```

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

llm-trunk runs as a local proxy on `127.0.0.1:4000`, between Claude Code and Anthropic. LiteLLM does the proxying. This repo adds the **routing policy** (which tier each request gets) and the tools that measure the cost.

▫️ **Tiers** (cheapest first; set in [`catalog.yaml`](catalog.yaml), each tier's model in [`litellm/config.yaml`](litellm/config.yaml)):

| Tier | Model | Effort | Max input | Max output |
|---|---|---|---|---|
| `light` | Haiku 4.5 | low | 64k tokens | 4,000 tokens |
| `moderate` | Sonnet 5 | medium | 128k tokens | 4,000 tokens |
| `complex` | Opus 5.5 | high | 180k tokens | 8,000 tokens |

▫️ **Routing rules:**

| Request type | What it is | Tier |
|---|---|---|
| **normal** | Your own prompts | The session's sticky tier, else `light` |
| **skill** (registered) | A skill listed in the catalog, with a matching hash | Its catalog tier. It also becomes the session's sticky tier |
| **skill** (unregistered) | Any other skill | `light`, and the session's sticky tier ends |
| **subagent** | Requests from a subagent Claude Code starts | One tier below the session (never below `light`). Each subagent keeps its first tier |
| **compaction** | `/compact` or auto-compaction | The session's tier. No input cap, so an oversized session can always be shrunk |
| **background** | Next-prompt suggestions, away-summary recaps | The session's tier. They resend the whole conversation, so another model would have to re-cache it |
| | Session titles | `light` (about 1k tokens, nothing cached) |
| | Auto mode's permission checks | The tier running the model Claude Code asked for, so the safety check isn't weakened. Never above what the key could reach with a skill |

▫️ **Key characteristics:**
- [x] **Bounded stickiness:** after a registered skill runs, your later messages in that session stay on its tier. This ends after 10 minutes idle or 30 minutes after the skill was last invoked, whichever comes first. Claude Code's own calls, subagents and compaction never extend it
- [x] **Deny rules:** a skill edited since it was hashed, or input over the tier's cap, gets an HTTP 400 with the reason. The request never reaches Anthropic
- [x] **Measured:** every request is logged with its type, tier, tokens (including cache reads and writes), cost, the model that answered and the model Claude Code asked for
- [x] **A local lab, not a production service:** one machine (macOS or Ubuntu), Docker Compose, and LiteLLM's own keys for access

## 🔀 How It Works

▫️ **One-time setup:**
- [x] Each skill's `SKILL.md` body (without its frontmatter) is hashed with [`scripts/hash_skill.py`](scripts/hash_skill.py)
- [x] The hash, the body's length in bytes and a tier go into [`catalog.yaml`](catalog.yaml)
- [x] Docker Compose starts LiteLLM and Postgres. LiteLLM loads `litellm/config.yaml`: one model per tier, its prices, and the routing callback
- [x] A budget-capped virtual key is created through LiteLLM. Claude Code uses that key and never sees the real Anthropic key

▫️ **Every request:** the callback decides what the request is, checking in this order:
1. **permission check:** Claude Code's security-monitor system prompt, with no tools. Checked first, so a check made for a subagent isn't moved down a tier with it
2. **subagent:** the request carries Claude Code's `x-claude-code-agent-id` header
3. **compaction:** the newest user message ends with Claude Code's compaction instruction. Checked before skills, because compaction resends the message it compacts, which may be a skill call
4. **background:** session titles, suggestions and recaps, recognized by the fixed text Claude Code sends
5. **skill:** a skill invoked in the **newest user message** (a typed `/skill`, or Claude calling its `Skill` tool). Older messages don't count, since Claude Code resends the whole conversation every time. It's registered if it's in the catalog, its hash matches, and the key is allowed to use it. Built-in commands like `/model` aren't skills
6. **normal:** everything else

▫️ **Then:**
- [x] **Allow:** the model and effort are set to the tier's, whatever Claude Code asked for, and `max_tokens` is capped at the tier's max output. Exceptions: titles run without thinking, and permission checks keep Claude Code's own effort, with `max_tokens` up to 8,192. `system` messages in the middle of a conversation are moved into the user message, because Haiku 4.5 rejects them
- [x] **Deny:** an edited skill or oversized input gets a 400 with the reason, and nothing is sent to Anthropic
- [x] **Log:** after Anthropic answers, one JSON line goes to the gateway's log. Failed requests (e.g. a 529 overload) are logged too

> ⚠️ **NOTE:** Background, compaction and permission-check requests are recognized by the exact text Claude Code sends. If a Claude Code update rewords that text, those requests will be treated as normal messages: they'll go to the session's tier and keep its sticky timer alive. Compaction would also be subject to the input cap again. The automated tests won't catch this (they use saved copies of the text), and neither will `scripts/check_rules.py` (it trusts the logged request type). After updating Claude Code, run the [manual sanity suite](tests/sanity/manual.py) and check that the 🔒, 🧹 and `(i)` labels still appear.

▫️ **Who can reach which tier (keys):**
- [x] **A skill's hash isn't a secret.** Anyone can read the skill files, so any client can send a registered skill's exact text and get its tier. To limit this, set `allowed_skills` in a key's metadata: only those skills can lift that key above the lowest tier, and any other skill counts as unregistered (like per-port VLAN filtering). A key without `allowed_skills` can reach every tier
- [x] **Headers are ignored by default.** The `x-skill-id` and `x-skill-hash` headers are only trusted from a key with `metadata: {"trust_skill_headers": true}`. No key has this today
- [x] **Permission checks stay within the key's reach.** A faked one never goes above the highest tier the key could reach with a skill, and it keeps that tier's input cap. It does keep Claude Code's own effort and up to 8,192 output tokens, because real permission checks need them

## 🧪 Example Session

▫️ What the gateway decides during a Claude Code session in a client repo that has the catalog's skills. Each row is also an automated test in [`tests/test_example_session.py`](tests/test_example_session.py).

| # | What you do | Gateway decision |
|---|---|---|
| 1 | Ask a plain question in a new session | normal → `light` (Haiku 4.5, low) |
| 2 | Run `/design-review` | hash verified → `complex` (Opus 5.5, high), sticky |
| 3 | Ask a follow-up | normal → stays on `complex` |
| 4 | Run `/change-review` | hash verified → switches to `moderate` (Sonnet 5, medium) |
| 5 | Ask Claude to use a subagent | subagent → `light`, one tier below `moderate` |
| 6 | Run `/personal-notes` (not in the catalog) | unregistered → `light`, and the next message stays there |
| 7 | Run `/compact` in a `complex` session that's over the cap | compaction → stays on `complex`, no cap |
| 8 | Edit a skill file, then run the skill | **400** stale hash, never reaches Anthropic |
| 9 | Paste a ~200 KB log into a `light` session | **400** input over the 64k cap, never reaches Anthropic |
| 10 | Start a new session | starts on `light`; nothing carries over |

> ⚠️ **NOTE:** Moving to a tier with a different model or effort makes the next message uncached, because Anthropic's prompt cache is per model and settings (see [Concepts 101](#-concepts-101)). Subagents are the cheapest place to use a lower tier, since they start with a fresh context anyway.

> ⚠️ **NOTE:** Claude Code shows a denial's reason as-is, e.g. `API Error: 400 llm-trunk: ~89409 input tokens exceeds the light tier cap (64000) — run /compact or start a new session`.

## 🚀 Installation & Usage

▫️ **Prerequisites:**

| | macOS | Ubuntu |
|---|---|---|
| Docker with Compose v2 | [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) | [Docker Engine + Compose plugin](https://docs.docker.com/engine/install/ubuntu/), then `sudo usermod -aG docker $USER` and log in again |
| Python 3.11+ | `brew install python` | `sudo apt install python3 python3-venv` |
| `curl`, `openssl` | preinstalled | `sudo apt install curl openssl` |
| [Claude Code](https://docs.claude.com/en/docs/claude-code/setup) | ✓ | ✓ |
| An Anthropic API key | ✓ | ✓ |

The monitoring scripts read the gateway's log with `docker compose logs`, so run them as a user who can use Docker without `sudo`, on the machine running the gateway.

▫️ **Step 1 - Clone and configure:**
```
git clone https://github.com/pdudotdev/llm-trunk
cd llm-trunk
cp .env.example .env
```
Edit `.env` and fill in:
- `LITELLM_MASTER_KEY`: the gateway's admin key. It must start with `sk-`, e.g. the output of `echo "sk-$(openssl rand -hex 24)"`
- `ANTHROPIC_API_KEY`: the key the gateway uses to call Anthropic. A separate Anthropic workspace for it makes its spend easy to track
- `POSTGRES_PASSWORD`: use `openssl rand -hex 24`. It must be URL-safe, so don't use `-base64`. `POSTGRES_USER` and `POSTGRES_DB` can stay `litellm`

▫️ **Step 2 - Install the Python tools:**
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```
This installs what the dashboard, scripts and tests need. Run `source .venv/bin/activate` again in each new terminal.

▫️ **Step 3 - Start the gateway:**
```
docker compose up -d
curl http://127.0.0.1:4000/health/liveliness    # prints "I'm alive!" once it's up
```
If it doesn't come up, check `docker compose logs litellm`.

▫️ **Step 4 - Register your skills:**
```
python3 scripts/hash_skill.py path/to/.claude/skills/my-skill/SKILL.md
```
It prints the skill's `sha256` and `bytes`. Add them to `catalog.yaml` under `skills`, with a tier:
```yaml
  my-skill:
    tier: moderate
    sha256: "…"
    bytes: 424
```
Skills you don't register still work; they run on the lowest tier. Rerun `hash_skill.py` and update the entry whenever a `SKILL.md` changes, or that skill will be denied.

▫️ **Step 5 - Create a key and point Claude Code at the gateway:**
```
./scripts/create_usage_key.sh
cp client-settings.json.example path/to/your-client-repo/.claude/settings.json
```
The script prints JSON with a `key` field (`sk-…`). Put that value in place of `ANTHROPIC_AUTH_TOKEN`'s placeholder in the copied `settings.json`. The key has a $50 budget per 30 days; edit the script to change that.

▫️ **Step 6 - Use it:**
```
cd path/to/your-client-repo && claude       # terminal 1: work as usual; /my-skill runs on its tier
python3 scripts/dashboard.py                # terminal 2, in llm-trunk: live costs and routing
```

> ⚠️ **NOTE:** `catalog.yaml` is re-read on every request, so changes apply immediately. A mistake in it breaks every request; `python3 -m pytest` validates it. Changes under `policy/` or to `litellm/config.yaml` need `docker compose restart litellm`.

> ⚠️ **NOTE:** The routing history is the gateway container's log. `docker compose restart` keeps it, but `docker compose down` or recreating the container (e.g. after changing `docker-compose.yml`) clears it.

## 📊 Measuring It

Run these on the gateway's machine, from the `llm-trunk` folder, with the virtual environment active.

▫️ **Live dashboard:** `python3 scripts/dashboard.py`. Shows savings compared with the models Claude Code asked for, spend per request type, a prompt-cache countdown per session (with the price of the next message while the cache is warm vs cold), and a live feed. Options: `--since` (history to load, default 1h), `--ttl` (cache lifetime, `5m` or `60m`), `--window` (how recent a session must be to show).

▫️ **Live log:** `python3 scripts/watch.py`. One line per routing decision, from the last 10 minutes onward (`--since 1h` for more). Needs only the Python standard library; `NO_COLOR=1` turns off colors.

▫️ **Scripted scenario:** `python3 scenarios/run.py` drives a real Claude Code session through the gateway, following [`scenarios/dev-day.yaml`](scenarios/dev-day.yaml): every request type on every tier, plus an idle gap long enough for the cache to expire. For each step it confirms the message reached Claude Code, checks the answer and the tier, then runs the rule checker over the session. It costs real tokens, about $1–2 per run on the gateway's key.
- It runs in a client repo that has the catalog's skills and a `settings.json` pointing at the gateway. The default is `../company-client`; set another with `--client path/to/repo`
- `--quick` skips the idle gap. `--cold-start` first waits for the cache to expire, so runs are comparable. `--dry-run` lists the steps without sending anything
- If Claude Code stops accepting input, the run prints Claude Code's screen and stops. Results are saved in `scenarios/results/`

▫️ **Rule checker:** `python3 scripts/check_rules.py` (`--since 1h` to limit it, or `--results scenarios/results/<run>.json` to check a saved scenario run). Checks every logged request against the routing rules, including that the model that answered is the tier's model. Exits with 1 if any rule was broken. It only sees what the gateway logs: a request LiteLLM rejects before the routing callback runs (e.g. a bad or missing key) isn't logged.

▫️ **Cost report:** `python3 scripts/report.py` (`--since 24h` to limit it). Totals per request type, tier, day and session, and how close the gateway's input-size estimates were.

▫️ **Automated tests:** `python3 -m pytest`, also run by CI on every push. The tests that compare `catalog.yaml` with the real skill files are skipped unless those files are at `../company-client/.claude/skills` or at the path in `LLM_TRUNK_SKILLS_DIR`.

▫️ **Manual sanity suite:** [`tests/sanity/manual.py`](tests/sanity/manual.py) lists 10 checks to run by hand in Claude Code with the dashboard open: tiers, stickiness and its expiry, subagents, unregistered and edited skills, the input cap, compaction and permission checks. Each has pass criteria and the reason for them. `python3 tests/sanity/manual.py` prints it.

## 💡 Concepts 101

▫️ **Prompt caching (the `(c)` tag in the watcher)**

Anthropic caches the beginning of each request. Claude Code marks the tool definitions, the system prompt and the conversation so far as cacheable. A later request reuses the cache for the **longest part that starts identically**. A conversation only grows **at the end**, so each request starts with exactly what the previous one sent:

| Request | Contents | From cache | Processed fresh |
|---|---|---|---|
| Turn 1 | tools + system + **msg 1** | nothing (first time) | everything, and it's stored |
| Turn 2 | tools + system + msg 1 + reply 1 + **msg 2** | everything up to msg 1 | reply 1 + msg 2, also stored |
| Turn 3 | … + msg 2 + reply 2 + **msg 3** | everything up to msg 2 | reply 2 + msg 3 |

So each turn pays full price only for the new part at the end. In a real session, one turn had 55,427 input tokens, of which 55,173 came from the cache. Only 254 were new, so it cost $0.012 instead of about $0.14.

- **Price:** reading from the cache costs **0.1×** the normal input price (0.05× on Opus 5.5). Writing to it costs **1.25×**. That makes a cached token cost the same on Opus 5.5 and Sonnet 5 ($0.20 per million), so moving heavily cached traffic between them saves little. The difference is in new input, cache writes and output
- **Expiry:** about 5 minutes without use; each use resets it. This is separate from llm-trunk's 10-minute sticky timer
- **What resets it** (the next message pays full price once): any change to an earlier part of the request (e.g. `/compact` rewriting the history), a different model (each model has its own cache), or different thinking or effort settings. That's why changing tiers costs one uncached message
- **Shared across sessions:** Claude Code's tools and system prompt are the same in every session, so a session started within about 5 minutes of another reads them from the cache

▫️ **Claude Code's own requests (the `(i)` tag in the watcher)**

Besides your messages, Claude Code sends requests of its own. llm-trunk recognizes these (identified from live traffic):

| Request | When it's sent | How it's recognized | Tier |
|---|---|---|---|
| **Session title** | Once per session, after your first messages | No tools, and Claude Code's `<session>` title prompt | `light` |
| **Next-prompt suggestion** | A few seconds after a reply | Last message starts with `[SUGGESTION MODE:` | the session's |
| **Away summary** | About 3 minutes after you leave the terminal | Last message starts with *"The user stepped away and is coming back."* | the session's |
| **Permission check** | Before actions, in auto mode | Claude Code's security-monitor system prompt, no tools | the one running the model asked for |

None of these start or extend a sticky timer; otherwise they would keep an expensive tier alive while nobody is working. Away summaries can be turned off in Claude Code's `/config`.

## 📂 Project Files

▫️ **Setup:**

| File | Role |
|---|---|
| [`scripts/hash_skill.py`](scripts/hash_skill.py) | Prints the `sha256` and `bytes` of a skill's body (without frontmatter) for `catalog.yaml`. Rejects skills that use `$ARGUMENTS`, `$1`, `${CLAUDE_SKILL_DIR}`, `${CLAUDE_SESSION_ID}` or `` !`cmd` ``: Claude Code fills those in when it runs the skill, so the hash could never match. |
| [`catalog.yaml`](catalog.yaml) | The routing table: the tier `order` (cheapest first), each tier's effort and token caps, and the registered skills with their hash and tier. |
| [`litellm/config.yaml`](litellm/config.yaml) | LiteLLM's config: one entry per tier (its model and prices), the routing callback, and the master key and database settings. |
| [`pricing.yaml`](pricing.yaml) | Anthropic's list prices per model, used to work out what a request would have cost on the model Claude Code asked for. |
| [`docker-compose.yml`](docker-compose.yml) | Starts `postgres` (keys and spend) and `litellm` (the proxy), both reachable only from this machine. LiteLLM is pinned to one exact version (1.102.0), because the callback depends on its internals. |
| [`scripts/create_usage_key.sh`](scripts/create_usage_key.sh) | Creates a virtual key named `llm-trunk-usage` with a budget and rate limits. It deliberately has no model restriction: LiteLLM would check that against the model Claude Code asks for, before the callback changes it, and block every request. Limit skills with `allowed_skills` in the key's metadata instead. |
| [`client-settings.json.example`](client-settings.json.example) | Template for your client repo's `.claude/settings.json`, pointing Claude Code at the gateway. |
| [`.env.example`](.env.example) | Template for `.env`: the master key, the Anthropic key and the Postgres settings. |

▫️ **Every request:**

| File | Role |
|---|---|
| [`policy/litellm_callback.py`](policy/litellm_callback.py) | The LiteLLM callback that runs on every request. It works out the request type (see [How It Works](#-how-it-works)) and checks a skill's hash over exactly the catalog's `bytes`, so any edit, including an added line, is denied until the skill is re-hashed. It applies the key's `allowed_skills`, estimates the input size, gets a decision from `decide()` (or picks the permission check's tier), then sets the model, effort and `max_tokens`. It remembers each session's sticky tier and each subagent's tier, and logs every outcome as one JSON line: `spend`, `deny`, `expired` or `failed`. |
| [`policy/decide.py`](policy/decide.py) | The routing rules as one function, with no LiteLLM or network code: catalog, request type, session tier, skill and input size in; allow or deny, with tier, effort and reason, out. |
| [`policy/hash.py`](policy/hash.py) | Hashing and frontmatter stripping, done the same way Claude Code does it (handles a byte-order mark and Windows line endings). Shared by the callback and `hash_skill.py`. |
| [`policy/models.py`](policy/models.py) | `model_key()`: turns any spelling of a Claude model id (dated, `[1m]`, Bedrock-style) into one name, so the callback and the scripts compare models the same way. |

▫️ **Monitoring and testing:**

| File | Role |
|---|---|
| [`scripts/dashboard.py`](scripts/dashboard.py) | Live dashboard: savings, spend per request type, cache countdowns per session, live feed. |
| [`scripts/watch.py`](scripts/watch.py) | Live log, one color-coded line per request: type, tier, skill, model, effort, sticky time left, input tokens (`(c)` when mostly cached) and cost. |
| [`scripts/report.py`](scripts/report.py) | Cost totals per request type, tier, day and session, plus estimate accuracy. |
| [`scripts/check_rules.py`](scripts/check_rules.py) | Checks logged requests against the routing rules; exits with 1 on a violation. |
| [`scenarios/run.py`](scenarios/run.py) · [`scenarios/dev-day.yaml`](scenarios/dev-day.yaml) | The scripted Claude Code session and its steps. |
| [`tests/`](tests/) | Automated tests for the routing rules, the callback (with LiteLLM stubbed out), the catalog, the log format and every script. [`tests/sanity/manual.py`](tests/sanity/manual.py) is the manual suite; pytest doesn't run it. |

## ⬆️ Planned Upgrades
- [ ] Benchmark table: the same scenario with and without routing, repeated, from a cold cache
- [ ] Per-agent exceptions for subagents (e.g. a planning agent keeping its parent's tier)
- [ ] Cache-aware switching: only move to a cheaper tier when the savings outweigh re-caching
- [ ] Per-department virtual keys, each limited with `allowed_skills`, with spend shown per key
- [ ] Log requests LiteLLM rejects before the routing callback runs, so the rule checker sees them too

## 📄 Disclaimer
You're responsible for creating your own API keys, paying for your usage, and checking `catalog.yaml` against your own skill files before routing real work through llm-trunk.

## 📜 License
Licensed under the [**GNU General Public License v3.0**](LICENSE).

## 📧 Hi
Wanna say hello? DM me on [**LinkedIn**](https://www.linkedin.com/in/tmihaicatalin/).
