"""
## Manual test suite

**Setup:** open two terminals.
- **Dashboard:** in `llm-trunk`, run `python3 scripts/dashboard.py`.
- **Claude Code:** in `company-client`, run `claude`.

Start a fresh Claude Code session for tests 1–6 and run them in order.

**Reading the dashboard:** in the live feed, the route icon shows what kind of request it was. `(i)` marks a request Claude Code made on its own, not something you typed. REQ TYPE names the skill when the request invokes one, followed by the TIER, MODEL and EFFORT columns. The sessions pane shows each session's prompt-cache countdown (CACHE) and its sticky-tier countdown (STICKY).

| # | Do this | Pass criteria (dashboard) | Why |
|---|---|---|---|
| 1 | Send a plain question, e.g. "What does a load balancer do?" | A `📛 title (i)` row on **light** with no effort shown, plus `⚪ untagged` on **light · Haiku 4.5 · low** | Plain chat is the "untagged frame": it goes to the lowest tier. The title is Claude Code naming the session; it's ~1k tokens and needs no thinking. |
| 2 | `/design-review Move sessions to Redis` | `🔖 invoked` · `skill (design-review)` · **complex** · Opus 5.5 · high. The sessions pane's STICKY shows **📌 5:00 design-review**, counting down | The skill's SHA-256 hash matches its entry in `catalog.yaml`, and that entry assigns the complex tier. This starts the session's 5-minute "sticky" timer. |
| 3 | Within 5 min, ask a follow-up ("Which risk first?") | `📌 sticky` on **complex**, STICKY back to **5:00**. Any `(i)` suggestion rows are also on complex, but they don't reset the timer. | Follow-ups keep the skill's tier (bounded stickiness). Only your own turns refresh the timer. Background calls stay on the session tier so the prompt cache isn't rebuilt on another model. |
| 4 | Still in that session: "Use a subagent to list the folders in .claude/skills" | `🤖 subagent` rows on **moderate · Sonnet 5**, while the main session stays complex | Subagents run one tier below the session, and each keeps the tier it started on. |
| 5 | `/commit-message Fixed a typo`, then a follow-up | `🔖 invoked` · `skill (commit-message)` on **light**, then `📌 sticky` on light — or `⚓ held` on complex while moving would re-cache more than it saves (the NOTE gives both amounts) | A newly invoked registered skill sets the session's tier, even if that's lower than before. Stickiness follows the most recent skill; the cache decides when the move happens. |
| 6 | `/personal-notes check redis ttl`, then a follow-up | `🔹 unregistered` · `skill (personal-notes)` on **light** (or `⚓ held`, as in test 5), then `⚪ untagged` on light, and STICKY shows `—` | A skill not in the catalog gets no tier of its own and ends the sticky route. Unknown code never gets an expensive model. |
| 7 | In `company-client/.claude/skills/change-review/SKILL.md`, add one line. Run `/change-review x`. Then undo the edit and run it again. | First run: `⛔ denied`, and "changed since it was hashed" appears in both the dashboard and Claude Code (`API Error: 400 …`). After the undo: `🔖 invoked` on moderate. | Any edit changes the hash, so an edited skill can't keep its tier until someone re-hashes it. The request is refused before it reaches Anthropic, so it costs $0. |
| 8 | In a light session, paste a file of more than about 250 KB (over 64k tokens), then run `/compact` | The paste shows `⛔ denied … exceeds the light tier cap (64000)`, and Claude Code shows the same message. `/compact` then shows `🧹 compaction` and succeeds. | Each tier has a maximum input size (`max_input`), so a huge paste can't run up a bill. Compaction is exempt, so a session over the limit can always be shrunk. |
| 9 | Switch to auto mode (Shift+Tab) and ask for something that edits a file | `🔒 permission (i)` rows on the tier running the model Claude Code asked for (with the default Opus 5.5: **complex**). A skill session's timer doesn't change. | The permission check is a safety check, so it's never moved to a weaker model. With a key limited by `allowed_skills` it stays within that key's tiers (the ceiling fix). Background calls never extend stickiness. |
| 10 | After test 2 or 5, leave the session idle for more than 5 minutes, then send a message | An `⏳ expired` row appears just before your message; its note says when the route really ended (`… ended at HH:MM (idle)`). Your message shows `⚪ untagged` on **light**, with no hold: the cache has expired too. | The sticky tier times out after 5 minutes idle (the prompt cache's lifetime), or 30 minutes after the last invocation. Expiry is recorded at the next request, so that's when the row appears. |

### Cache-aware switching

Start a fresh session, at least 6 minutes after any other traffic through the gateway, so no tier has anything cached.

| # | Do this | Pass criteria (dashboard) | Why |
|---|---|---|---|
| 11 | `/design-review Move sessions to Redis`, then right away `/change-review Retry Redis writes 3 times` | The second row is `⚓ held` · `skill (change-review)` on **complex** · Opus 5.5 · high. Its NOTE reads `held for moderate: moving now ≈$0.1… · held so far $0.000`. NEXT MESSAGE shows `⚓ held for moderate · … moves after +$… more or when cold`. | Moving would re-cache the whole conversation on Sonnet 5, which reads the cache at the same price as Opus 5.5, so it would take dozens of messages to pay back. |
| 12 | Wait until the session's CACHE column shows `○ cold` (a recap may arrive at ~3 min and keep it warm for 5 more), then send a follow-up | The follow-up runs on **moderate** · Sonnet 5, with no NOTE | The cache has expired, so moving costs nothing extra. |
| 13 | Set `cache_aware: false` in `catalog.yaml`, repeat test 11 in a new session, then set it back | `/change-review` goes straight to **moderate** | The off switch, for comparing costs with and without holds. |
| 14 | `python3 scripts/check_rules.py --since 1h` | `no rule violations` | The checker confirms each hold independently: the conversation was served on that tier less than 5 minutes earlier, and the logged cost of moving exceeded what holding had cost. |

**Overall pass:** the dashboard's SAVED figure grows as you go. After the run, `python3 scripts/check_rules.py --since 1h` reports **no rule violations**.
"""

if __name__ == "__main__":
    print(__doc__)
