# 🔀 llm-trunk

[![Version](https://img.shields.io/badge/ver.-0.2.0-1a1a2e)](https://github.com/pdudotdev/llm-trunk/releases)
[![License](https://img.shields.io/badge/license-GPLv3-1a1a2e)](LICENSE)
[![Tests](https://github.com/pdudotdev/llm-trunk/actions/workflows/tests.yml/badge.svg)](https://github.com/pdudotdev/llm-trunk/actions/workflows/tests.yml)
[![Last Commit](https://img.shields.io/github/last-commit/pdudotdev/llm-trunk?color=1a1a2e)](https://github.com/pdudotdev/llm-trunk/commits/master/)

A routing gateway for Claude Code, built on [LiteLLM](https://docs.litellm.ai/). It sends each request to a cheaper or stronger model tier based on what the request is, and measures what that saves.

Skills are recognized by the SHA-256 hash of their `SKILL.md`, not by guessing from the prompt. A live dashboard shows each request's cost next to what it would have cost on the model Claude Code asked for.

▫️ **Same idea as an 802.1Q trunk port:**
- [x] **Tagged frame** → the VLAN ID picks the VLAN. Here: a registered skill's hash picks its tier
- [x] **Untagged frame** → goes to the native VLAN. Here: plain chat goes to the lowest tier
- [x] **Allowed-VLAN list** → only listed VLANs get their own path. Here: a skill missing from `catalog.yaml` goes to the lowest tier

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
  - [⚠️ Limitations](#️-limitations)
  - [💡 Concepts 101](#-concepts-101)
  - [📂 Project Files](#-project-files)
  - [⬆️ Planned Upgrades](#️-planned-upgrades)
  - [📄 Disclaimer](#-disclaimer)
  - [📜 License](#-license)
  - [📧 Hi](#-hi)

## 🔭 Overview

llm-trunk is a local proxy on `127.0.0.1:4000`, between Claude Code and Anthropic. LiteLLM does the proxying; this repo adds the routing policy and the tools that measure its cost.

▫️ **Tiers** (set in [`catalog.yaml`](catalog.yaml); models in [`litellm/config.yaml`](litellm/config.yaml)):

| Tier | Model | Effort | Max input | Max output |
|---|---|---|---|---|
| `light` | Haiku 4.5 | low | 64k tokens | 4,000 tokens |
| `moderate` | Sonnet 5 | medium | 128k tokens | 4,000 tokens |
| `complex` | Opus 5.5 | high | 180k tokens | 8,000 tokens |

▫️ **Routing rules:**

| Request | Tier |
|---|---|
| Your own messages | The session's sticky tier, else `light` |
| A registered skill | Its catalog tier, which becomes the session's sticky tier |
| An unregistered skill | `light`, and the sticky tier ends |
| Subagents | One tier below the session (never below `light`); each keeps its first tier |
| Compaction (`/compact`) | The session's tier, with no input cap |
| Suggestions and away summaries | The session's tier, so the prompt cache isn't lost |
| Session titles | `light` |
| Auto mode's permission checks | The tier running the model Claude Code asked for, so the safety check isn't weakened |

▫️ **Key characteristics:**
- [x] **Bounded stickiness:** after a registered skill runs, your next messages stay on its tier. This ends after 10 minutes idle, or 30 minutes after the skill was last run
- [x] **Deny rules:** an edited skill, or input over the tier's cap, is rejected with the reason before it reaches Anthropic
- [x] **Measured:** every request is logged with its type, tier, tokens, cost, and the model that answered
- [x] **A local lab:** one machine (macOS or Ubuntu) with Docker Compose, not a production service

## 🔀 How It Works

▫️ **Setup:**
- [x] Each skill's `SKILL.md` is hashed and listed in [`catalog.yaml`](catalog.yaml) with a tier
- [x] Docker Compose starts LiteLLM and Postgres, with llm-trunk's routing callback loaded
- [x] Claude Code gets a budget-capped LiteLLM key, never the real Anthropic key

▫️ **Every request:**
1. The callback works out what the request is: a permission check, a subagent, compaction, a background call, a skill, or a normal message
2. A skill counts as registered only if it's in the catalog, its hash matches, and the key is allowed to use it
3. The request is allowed or denied. If allowed, its model, effort and output limit are set to the tier's, whatever Claude Code asked for
4. After Anthropic answers, one line is written to the gateway's log

> ⚠️ **NOTE:** Background calls, compaction and permission checks are recognized by the exact text Claude Code sends. If a Claude Code update rewords that text, they'll be routed as normal messages. After updating Claude Code, run the [manual sanity suite](tests/sanity/manual.py).

▫️ **Access per key:**
- [x] A skill's hash isn't a secret: anyone who can read the skill files can send a registered skill and get its tier
- [x] To limit that, set `allowed_skills` in a key's metadata. Other skills then count as unregistered for that key. A key without it can reach every tier
- [x] Permission checks never go above the highest tier the key can reach

## 🧪 Example Session

| # | What you do | Gateway decision |
|---|---|---|
| 1 | Ask a plain question in a new session | `light` (Haiku 4.5, low) |
| 2 | Run `/design-review` | `complex` (Opus 5.5, high), sticky |
| 3 | Ask a follow-up | stays on `complex` |
| 4 | Run `/change-review` | switches to `moderate` (Sonnet 5, medium) |
| 5 | Ask Claude to use a subagent | `light`, one tier below `moderate` |
| 6 | Run `/personal-notes` (not in the catalog) | `light`, and the next message stays there |
| 7 | Run `/compact` in a `complex` session over the cap | stays on `complex`, no cap |
| 8 | Edit a skill file, then run the skill | **denied**: the skill changed since it was hashed |
| 9 | Paste a ~200 KB log into a `light` session | **denied**: input over the 64k cap |
| 10 | Start a new session | back to `light` |

A denial shows up in Claude Code as, for example: `API Error: 400 llm-trunk: ~89409 input tokens exceeds the light tier cap (64000) — run /compact or start a new session`.

> ⚠️ **NOTE:** Changing to a tier with a different model or effort makes the next message uncached (see [Concepts 101](#-concepts-101)). Subagents are the cheapest place to use a lower tier, since they start with a fresh context.

## 🚀 Installation & Usage

▫️ **Prerequisites:**

| | macOS | Ubuntu |
|---|---|---|
| Docker with Compose v2 | [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) | [Docker Engine + Compose plugin](https://docs.docker.com/engine/install/ubuntu/), then `sudo usermod -aG docker $USER` and log in again |
| Python 3.11+ | `brew install python` | `sudo apt install python3 python3-venv` |
| `curl`, `openssl` | preinstalled | `sudo apt install curl openssl` |
| [Claude Code](https://docs.claude.com/en/docs/claude-code/setup) | ✓ | ✓ |
| An Anthropic API key | ✓ | ✓ |

▫️ **Step 1 - Clone and configure:**
```
git clone https://github.com/pdudotdev/llm-trunk
cd llm-trunk
cp .env.example .env
```
Fill in `.env`:
- `LITELLM_MASTER_KEY`: must start with `sk-`, e.g. `echo "sk-$(openssl rand -hex 24)"`
- `ANTHROPIC_API_KEY`: the key the gateway uses to call Anthropic
- `POSTGRES_PASSWORD`: `openssl rand -hex 24`

▫️ **Step 2 - Install the Python tools:**
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```
Run `source .venv/bin/activate` again in each new terminal.

▫️ **Step 3 - Start the gateway:**
```
docker compose up -d
curl http://127.0.0.1:4000/health/liveliness    # "I'm alive!"
```

▫️ **Step 4 - Register your skills:**
```
python3 scripts/hash_skill.py path/to/.claude/skills/my-skill/SKILL.md
```
Add the printed values to `catalog.yaml` under `skills`, with a tier:
```yaml
  my-skill:
    tier: moderate
    sha256: "…"
    bytes: 424
```
Re-hash a skill whenever you edit its `SKILL.md`, or it will be denied. Skills you don't register still work, on the lowest tier.

▫️ **Step 5 - Point Claude Code at the gateway:**
```
./scripts/create_usage_key.sh
cp client-settings.json.example path/to/your-client-repo/.claude/settings.json
```
The script prints a `key` (`sk-…`, $50 budget per 30 days). Put it in the copied `settings.json` as `ANTHROPIC_AUTH_TOKEN`.

▫️ **Step 6 - Use it:**
```
cd path/to/your-client-repo && claude       # terminal 1: work as usual
python3 scripts/dashboard.py                # terminal 2, in llm-trunk: live costs
```
The dashboard starts empty; add `--since 1h` to load recent history. Scroll the feed with ↑/↓ or the mouse wheel, `g` jumps back to the newest.

> ⚠️ **NOTE:** Changes to `catalog.yaml` apply immediately. Changes under `policy/` or to `litellm/config.yaml` need `docker compose restart litellm`.

> ⚠️ **NOTE:** The routing history is kept in the gateway container's log. `docker compose down` erases it.

## ⚠️ Limitations

▫️ **Desktop Code tab:**
The Claude Desktop app's Code tab ignores a project's `settings.json`, so it cannot be routed per-repo. Desktop Code sessions always use your default settings (subscription or machine-wide). To use the gateway with a project, use the **CLI** (`claude` command) or the **VS Code extension** instead.

▫️ **Machine-wide routing (for API-only teams):**
If your team has no Claude Code subscriptions and routes all work through the gateway, set these in `~/.claude/settings.json`:
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
    "ANTHROPIC_AUTH_TOKEN": "sk-…"
  }
}
```
⚠️ **Warnings:** Every Claude Code session on this machine will use the gateway API key and bill against it. The gateway must always be running, or all sessions will fail. This setup is best for dedicated dev machines or CI environments; for mixed personal and team work, use per-repo opt-in instead.

## 💡 Concepts 101

▫️ **Prompt caching (the `(c)` tag)**

Anthropic caches the start of each request. A conversation only grows at the end, so each new message reuses everything before it from the cache and pays full price only for the new part:

| Request | Contents | From cache | New |
|---|---|---|---|
| Turn 1 | tools + system + **msg 1** | nothing | everything |
| Turn 2 | … + msg 1 + reply 1 + **msg 2** | up to msg 1 | reply 1 + msg 2 |
| Turn 3 | … + msg 2 + reply 2 + **msg 3** | up to msg 2 | reply 2 + msg 3 |

- **Price:** a cache read costs 0.1× the normal input price (0.05× on Opus 5.5); a cache write costs 1.25×
- **Expiry:** about 5 minutes without use
- **Reset by:** changing the model or the effort settings, or rewriting earlier history (e.g. `/compact`). That's why changing tiers costs one uncached message

▫️ **Claude Code's own requests (the `(i)` tag)**

| Request | When it's sent | Tier |
|---|---|---|
| **Session title** | Once, after your first messages | `light` |
| **Next-prompt suggestion** | A few seconds after a reply | the session's |
| **Away summary** | About 3 minutes after you leave the terminal | the session's |
| **Permission check** | Before actions, in auto mode | the one running the model asked for |

None of these extend the sticky timer, so they can't keep an expensive tier alive while nobody is working.

## 📂 Project Files

| File | Role |
|---|---|
| [`catalog.yaml`](catalog.yaml) | Tiers and registered skills |
| [`litellm/config.yaml`](litellm/config.yaml) | The model and prices for each tier |
| [`pricing.yaml`](pricing.yaml) | Anthropic's list prices, for the "without llm-trunk" comparison |
| [`docker-compose.yml`](docker-compose.yml) | LiteLLM and Postgres, reachable only from this machine |
| [`.env.example`](.env.example) · [`client-settings.json.example`](client-settings.json.example) | Templates for `.env` and your client repo's `.claude/settings.json` |
| [`policy/`](policy/) | The routing callback and rules |
| [`scripts/`](scripts/) | Key creation, skill hashing, dashboard, report, rule checker |
| [`scenarios/`](scenarios/) | The scripted Claude Code session |
| [`tests/`](tests/) | Automated tests, plus the manual sanity suite in `tests/sanity/` |

## ⬆️ Planned Upgrades
- [ ] Cache-aware switching: only move to a cheaper tier when the savings outweigh re-caching
- [ ] Per-department virtual keys, each limited with `allowed_skills`, with spend shown per key

## 📄 Disclaimer
You're responsible for creating your own API keys, paying for your usage, and checking `catalog.yaml` against your own skill files before routing real work through llm-trunk.

## 📜 License
Licensed under the [**GNU General Public License v3.0**](LICENSE).

## 📧 Hi
Wanna say hello? DM me on [**LinkedIn**](https://www.linkedin.com/in/tmihaicatalin/).
