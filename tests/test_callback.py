"""The LiteLLM callback: classifying requests, stickiness, background calls,
access control and how the outgoing request is rewritten."""
import pytest
from conftest import (
    BODIES,
    HTTPException,
    assistant,
    cb,
    compaction_request,
    key,
    permission_check_request,
    request,
    routing,
    skill_turn,
    subagent_request,
    unregistered_turn,
    user,
)

# --- Skill detection -----------------------------------------------------------


def test_arguments_after_the_body_are_allowed(gateway):
    assert gateway.send(request(skill_turn("plan", arguments="the login page")))["model"] == "complex"


def test_skill_invoked_by_claude_through_the_skill_tool(gateway):
    tool_use = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "review"}}]}
    tool_result = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "Launching skill: review"},
            {"type": "text", "text": f"Base directory for this skill: /repo/.claude/skills/review\n\n{BODIES['review']}"},
        ],
    }
    data = gateway.send(request(user("Is this change ok?"), tool_use, tool_result))
    assert data["model"] == "moderate" and routing(data)["skill_hash"]


def test_another_command_tag_before_the_skill_does_not_hide_it(gateway):
    turn = skill_turn("plan")
    turn["content"].insert(0, {"type": "text", "text": "<command-name>/model</command-name>"})
    assert gateway.send(request(turn))["model"] == "complex"


def test_only_the_newest_user_turn_counts(gateway):
    # An invocation from an earlier turn is resent with the history, but it
    # isn't live: a fresh gateway (no sticky route) routes this to the lowest tier.
    data = gateway.send(request(skill_turn("plan"), assistant(), user("Unrelated question")))
    assert data["model"] == "light" and routing(data)["request_type"] == "normal"


# --- Unregistered skills and built-in commands -------------------------------


def test_unregistered_skill_goes_to_the_lowest_tier(gateway):
    data = gateway.send(request(unregistered_turn()))
    assert data["model"] == "light"
    assert (routing(data)["request_type"], routing(data)["unregistered_skill"]) == ("skill", "personal-notes")


def test_unregistered_skill_through_the_skill_tool(gateway):
    tool_use = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "mine"}}]}
    tool_result = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "Launching skill: mine"},
        {"type": "text", "text": "Base directory for this skill: /repo/.claude/skills/mine\n\nMy own skill.\n"},
    ]}
    gateway.send(request(skill_turn("plan")))
    data = gateway.send(request(skill_turn("plan"), assistant(), user("do it"), tool_use, tool_result))
    assert data["model"] == "light" and routing(data)["unregistered_skill"] == "mine"


def test_built_in_command_is_not_an_unregistered_skill(gateway):
    gateway.send(request(skill_turn("plan")))
    # /model carries no skill body, so the session keeps its sticky tier.
    data = gateway.send(request(skill_turn("plan"), assistant(), user("<command-name>/model</command-name>\n<command-args>sonnet</command-args>")))
    assert data["model"] == "complex"
    assert routing(data)["unregistered_skill"] is None


def test_unregistered_skill_ends_the_sticky_route(gateway):
    gateway.send(request(skill_turn("plan")))
    gateway.send(request(skill_turn("plan"), assistant(), unregistered_turn()))
    follow_up = gateway.send(request(skill_turn("plan"), assistant(), unregistered_turn(), assistant(), user("more")))
    assert follow_up["model"] == "light" and routing(follow_up)["skill_id"] is None


# --- Sticky routes ---------------------------------------------------------------


def _invoke_plan(gateway, session="session-1"):
    gateway.send(request(skill_turn("plan"), session=session))


def _follow_up(gateway, session="session-1"):
    return gateway.send(request(skill_turn("plan"), assistant(), user("Next?"), session=session))


def test_sticky_route_survives_just_under_idle_timeout(gateway, clock):
    _invoke_plan(gateway)
    clock.advance(cb.STICKY_IDLE_TIMEOUT_SECONDS - 1)
    assert _follow_up(gateway)["model"] == "complex"


def test_sticky_route_expires_after_idle_timeout(gateway, clock, capsys):
    _invoke_plan(gateway)
    clock.advance(cb.STICKY_IDLE_TIMEOUT_SECONDS + 1)
    assert _follow_up(gateway)["model"] == "light"
    assert '"skill_id": "plan", "tier": "complex", "why": "idle"' in capsys.readouterr().out


def test_sticky_route_expires_at_max_age_even_while_used(gateway, clock, capsys):
    _invoke_plan(gateway)
    for _ in range(3):  # a real turn every 500 s keeps the idle timer fresh...
        clock.advance(500)
        assert _follow_up(gateway)["model"] == "complex"
    clock.advance(cb.STICKY_MAX_AGE_SECONDS - 1500 + 1)  # ...but not past max age
    assert _follow_up(gateway)["model"] == "light"
    assert '"why": "max-age"' in capsys.readouterr().out


def test_reinvoking_resets_max_age(gateway, clock):
    _invoke_plan(gateway)
    clock.advance(1500)
    _invoke_plan(gateway)
    clock.advance(400)
    assert _follow_up(gateway)["model"] == "complex"


def test_sticky_table_is_bounded(gateway, monkeypatch):
    monkeypatch.setattr(cb, "MAX_STICKY_SESSIONS", 2)
    for session in ("a", "b", "c"):
        _invoke_plan(gateway, session)
    assert _follow_up(gateway, "a")["model"] == "light"  # oldest evicted
    assert _follow_up(gateway, "c")["model"] == "complex"


# --- Subagents -------------------------------------------------------------------


@pytest.mark.parametrize(("parent_skill", "expected"), [("plan", "moderate"), ("review", "light"), ("verify", "light"), (None, "light")])
def test_subagent_one_tier_below_the_parent(gateway, parent_skill, expected):
    if parent_skill:
        gateway.send(request(skill_turn(parent_skill)))
    data = gateway.send(subagent_request())
    assert data["model"] == expected
    assert routing(data)["session_tier"] == {"plan": "complex", "review": "moderate", "verify": "light", None: "light"}[parent_skill]


def test_subagent_keeps_its_tier_for_all_its_requests(gateway):
    gateway.send(request(skill_turn("plan")))
    assert gateway.send(subagent_request("agent-1"))["model"] == "moderate"
    gateway.send(request(skill_turn("plan"), assistant(), skill_turn("review")))  # parent moves to moderate
    assert gateway.send(subagent_request("agent-1"))["model"] == "moderate"  # same subagent, same tier
    assert gateway.send(subagent_request("agent-2"))["model"] == "light"  # a new one follows the parent


def test_skill_inside_a_subagent_does_not_lift_it(gateway):
    data = gateway.send(subagent_request("agent-1", skill_turn("plan")))
    assert data["model"] == "light" and routing(data)["request_type"] == "subagent"


def test_subagent_does_not_touch_the_parent_route(gateway, clock):
    _invoke_plan(gateway)
    clock.advance(500)
    gateway.send(subagent_request())
    clock.advance(200)  # 700 s since the parent's last real turn
    assert _follow_up(gateway)["model"] == "light"


# --- Compaction ------------------------------------------------------------------


def test_compaction_is_detected_before_skills(gateway):
    # The compacted turn is an unregistered skill invocation: without
    # compaction coming first, this would drop to the lowest tier.
    gateway.send(request(skill_turn("plan")))
    data = gateway.send(compaction_request(skill_turn("plan"), assistant(), unregistered_turn()))
    assert data["model"] == "complex" and routing(data)["request_type"] == "compaction"
    assert data["reasoning_effort"] == "high"  # same thinking settings as the tier, so the cache matches


def test_compaction_needs_the_claude_code_header(gateway):
    data = gateway.send(compaction_request(user("x" * 1000), claude_code=False))
    assert routing(data)["request_type"] == "normal"


def test_compaction_does_not_refresh_the_route(gateway, clock):
    _invoke_plan(gateway)
    clock.advance(500)
    gateway.send(compaction_request(skill_turn("plan"), assistant(), user("go on")))
    clock.advance(200)
    assert _follow_up(gateway)["model"] == "light"


# --- Claude Code background calls ---------------------------------------------


def test_session_title_goes_to_the_lowest_tier_without_effort(gateway):
    _invoke_plan(gateway)
    data = gateway.send(request(user("<session>fix the login bug</session>"), tools=False))
    assert data["model"] == "light"
    assert routing(data)["background"] == "title"
    assert "reasoning_effort" not in data and routing(data)["effort"] is None


def test_title_never_invokes_a_skill(gateway):
    quoted = f"<session><command-name>/plan</command-name>\nBase directory for this skill: x\n\n{BODIES['plan']}</session>"
    data = gateway.send(request(user(quoted), tools=False))
    assert data["model"] == "light" and routing(data)["unregistered_skill"] is None


def test_suggestion_stays_on_the_tier_with_its_effort(gateway):
    _invoke_plan(gateway)
    data = gateway.send(request(skill_turn("plan"), assistant(), user("[SUGGESTION MODE: suggest the next prompt]")))
    assert routing(data)["background"] == "prompt_suggestion"
    assert (data["model"], data["reasoning_effort"]) == ("complex", "high")


def test_recap_does_not_keep_a_route_alive(gateway, clock):
    _invoke_plan(gateway)
    clock.advance(500)
    recap = gateway.send(request(skill_turn("plan"), assistant(), user("The user stepped away and is coming back. Recap.")))
    assert routing(recap)["background"] == "away_summary" and recap["model"] == "complex"
    clock.advance(200)
    assert _follow_up(gateway)["model"] == "light"


def test_background_detection_needs_the_claude_code_header(gateway, clock):
    _invoke_plan(gateway)
    clock.advance(500)
    gateway.send(request(skill_turn("plan"), assistant(), user("The user stepped away and is coming back."), claude_code=False))
    clock.advance(200)  # the look-alike counted as a real turn, so the route is still fresh
    assert _follow_up(gateway)["model"] == "complex"


def test_permission_check_passes_through_untouched(gateway):
    _invoke_plan(gateway)
    sent = permission_check_request()
    data = gateway.send(sent)
    assert data["model"] == "claude-sonnet-5"  # the model Claude Code chose for its safety check
    assert data["max_tokens"] == 64 and "reasoning_effort" not in data
    assert (routing(data)["background"], routing(data)["tier"]) == ("permission_check", None)


# --- Access control ----------------------------------------------------------------


def test_allowed_skills_limits_which_tiers_a_key_reaches(gateway):
    restricted = key(allowed_skills=["review"])
    data = gateway.send(request(skill_turn("plan")), restricted)
    assert data["model"] == "light" and routing(data)["unregistered_skill"] == "plan"
    assert gateway.send(request(skill_turn("review"), session="s2"), restricted)["model"] == "moderate"


def test_malformed_allowed_skills_fails_closed(gateway):
    assert gateway.send(request(skill_turn("plan")), key(allowed_skills="plan"))["model"] == "light"


def _header_request(skill_id, skill_hash):
    data = request(user("Run it"))
    data["litellm_metadata"]["headers"].update({"x-skill-id": skill_id, "x-skill-hash": skill_hash})
    return data


def test_skill_headers_ignored_without_trust(gateway, catalog):
    assert gateway.send(_header_request("plan", catalog["skills"]["plan"]["sha256"]))["model"] == "light"


def test_skill_headers_honored_for_trusted_key(gateway, catalog):
    data = gateway.send(_header_request("plan", catalog["skills"]["plan"]["sha256"]), key(trust_skill_headers=True))
    assert data["model"] == "complex"


def test_trust_flag_must_be_a_real_boolean(gateway, catalog):
    data = gateway.send(_header_request("plan", catalog["skills"]["plan"]["sha256"]), key(trust_skill_headers="true"))
    assert data["model"] == "light"


def test_trusted_headers_with_wrong_hash_are_denied(gateway):
    with pytest.raises(HTTPException) as denied:
        gateway.send(_header_request("plan", "0" * 64), key(trust_skill_headers=True))
    assert "changed since it was hashed" in denied.value.detail


def test_trusted_headers_for_an_unknown_skill_are_denied(gateway):
    with pytest.raises(HTTPException) as denied:
        gateway.send(_header_request("nope", "0" * 64), key(trust_skill_headers=True))
    assert "not in catalog.yaml" in denied.value.detail


# --- How the outgoing request is rewritten ------------------------------------


def test_client_thinking_and_effort_are_replaced_by_the_tier(gateway):
    data = gateway.send(request(skill_turn("plan"), thinking={"type": "adaptive"}, output_config={"effort": "max", "format": "text"}))
    assert "thinking" not in data
    assert data["output_config"] == {"format": "text"}
    assert data["reasoning_effort"] == "high"


def test_empty_output_config_is_removed(gateway):
    assert "output_config" not in gateway.send(request(user("Hi"), output_config={"effort": "max"}))


@pytest.mark.parametrize(("requested", "expected"), [(32000, 4000), (1000, 1000), (None, 4000)])
def test_max_tokens_capped_at_the_tier_max_output(gateway, requested, expected):
    data = request(user("Hi"))
    if requested is None:
        del data["max_tokens"]
    else:
        data["max_tokens"] = requested
    assert gateway.send(data)["max_tokens"] == expected


def test_mid_conversation_system_message_folds_into_user_turn(gateway):
    data = gateway.send(request(user("Look at this"), {"role": "system", "content": "Contents of app.py"}, assistant()))
    assert [message["role"] for message in data["messages"]] == ["user", "assistant"]
    assert data["messages"][0]["content"][-1] == {"type": "text", "text": "Contents of app.py"}


def test_leading_system_message_is_kept(gateway):
    data = gateway.send(request({"role": "system", "content": "Be brief."}, user("Hi")))
    assert [message["role"] for message in data["messages"]] == ["system", "user"]


def test_system_message_after_tool_use_goes_after_tool_results(gateway):
    tool_use = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}
    tool_result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}
    data = gateway.send(request(user("Read it"), tool_use, {"role": "system", "content": "ctx"}, tool_result))
    last = data["messages"][-1]["content"]
    assert last[0]["type"] == "tool_result" and last[-1] == {"type": "text", "text": "ctx"}


# --- Input size estimate -------------------------------------------------------------


def test_estimate_counts_characters_over_three():
    # 300 chars of text plus the keys and role: (4 + 4 + 7 + 300) // 3
    assert cb._estimate_input_tokens({"messages": [{"role": "user", "content": "x" * 300}]}) == 105


def test_estimate_prices_media_by_block_not_encoded_length():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 1_000_000}}
    estimate = cb._estimate_input_tokens({"messages": [{"role": "user", "content": [image]}]})
    assert cb.MEDIA_BLOCK_TOKENS <= estimate < cb.MEDIA_BLOCK_TOKENS + 50


def test_estimate_ignores_earlier_thinking_blocks():
    thinking = {"role": "assistant", "content": [{"type": "thinking", "thinking": "z" * 30_000}]}
    assert cb._estimate_input_tokens({"messages": [thinking]}) < 50


def test_estimate_includes_tool_definitions():
    tools = [{"name": "Big", "description": "d" * 3000, "input_schema": {}}]
    assert cb._estimate_input_tokens({"messages": [], "tools": tools}) >= 1000


# --- Every request is labelled ------------------------------------------------------


def test_every_request_carries_type_and_tier(gateway):
    gateway.send(request(skill_turn("plan")))
    for data in (
        request(skill_turn("plan"), assistant(), user("next")),
        subagent_request(),
        compaction_request(skill_turn("plan"), assistant(), user("go")),
        request(skill_turn("plan"), assistant(), user("[SUGGESTION MODE: x]")),
    ):
        fields = routing(gateway.send(data))
        assert fields["request_type"] in ("normal", "subagent", "compaction", "background")
        assert fields["tier"] in ("light", "moderate", "complex")
        assert fields["session_tier"] == "complex"
