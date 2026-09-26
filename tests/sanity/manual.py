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
| 2 | `/design-review Move sessions to Redis` | `🔖 invoked` · `skill (design-review)` · **complex** · Opus 5.5 · high. The sessions pane's STICKY shows **📌 10:00 design-review**, counting down | The skill's SHA-256 hash matches its entry in `catalog.yaml`, and that entry assigns the complex tier. This starts the session's 10-minute "sticky" timer. |
| 3 | Within 10 min, ask a follow-up ("Which risk first?") | `📌 sticky` on **complex**, STICKY back to **10:00**. Any `(i)` suggestion rows are also on complex, but they don't reset the timer. | Follow-ups keep the skill's tier (bounded stickiness). Only your own turns refresh the timer. Background calls stay on the session tier so the prompt cache isn't rebuilt on another model. |
| 4 | Still in that session: "Use a subagent to list the folders in .claude/skills" | `🤖 subagent` rows on **moderate · Sonnet 5**, while the main session stays complex | Subagents run one tier below the session, and each keeps the tier it started on. |
| 5 | `/commit-message Fixed a typo`, then a follow-up | `🔖 invoked` · `skill (commit-message)` on **light**, then `📌 sticky` on light | A newly invoked registered skill sets the session's tier, even if that's lower than before. Stickiness follows the most recent skill. |
| 6 | `/personal-notes check redis ttl`, then a follow-up | `🔹 unregistered` · `skill (personal-notes)` on **light**, then `⚪ untagged` on light, and STICKY shows `—` | A skill not in the catalog gets no tier of its own and ends the sticky route. Unknown code never gets an expensive model. |
| 7 | In `company-client/.claude/skills/change-review/SKILL.md`, add one line. Run `/change-review x`. Then undo the edit and run it again. | First run: `⛔ denied`, and "changed since it was hashed" appears in both the dashboard and Claude Code (`API Error: 400 …`). After the undo: `🔖 invoked` on moderate. | Any edit changes the hash, so an edited skill can't keep its tier until someone re-hashes it. The request is refused before it reaches Anthropic, so it costs $0. |
| 8 | In a light session, paste a file of more than about 250 KB (over 64k tokens), then run `/compact` | The paste shows `⛔ denied … exceeds the light tier cap (64000)`, and Claude Code shows the same message. `/compact` then shows `🧹 compaction` and succeeds. | Each tier has a maximum input size (`max_input`), so a huge paste can't run up a bill. Compaction is exempt, so a session over the limit can always be shrunk. |
| 9 | Switch to auto mode (Shift+Tab) and ask for something that edits a file | `🔒 permission (i)` rows on the tier running the model Claude Code asked for (with the default Opus 5.5: **complex**). A skill session's timer doesn't change. | The permission check is a safety check, so it's never moved to a weaker model. With a key limited by `allowed_skills` it stays within that key's tiers (the ceiling fix). Background calls never extend stickiness. |
| 10 | After test 2 or 5, leave the session idle for more than 10 minutes, then send a message | An `⏳ expired … (idle) → back to untagged` row appears just before your message. Your message shows `⚪ untagged` on **light**. | The sticky tier times out after 10 minutes idle, or 30 minutes after the last invocation. Expiry is recorded at the next request, so that's when the row appears. |

**Overall pass:** the dashboard's SAVED figure grows as you go. After the run, `python3 scripts/check_rules.py --since 1h` reports **no rule violations**.
"""

if __name__ == "__main__":
    print(__doc__)
