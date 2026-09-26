"""The LiteLLM callback: skill detection, stickiness, background calls, access
control and how the outgoing request is rewritten."""
import pytest
from conftest import (
    BODIES,
    HTTPException,
    assistant,
    cb,
    key,
    request,
    routing,
    skill_turn,
    user,
)

# --- Skill detection -----------------------------------------------------------


def test_arguments_after_the_body_are_allowed(gateway):
    data = gateway.send(request(skill_turn("plan", arguments="the login page")))
    assert data["model"] == "plan-lane"


def test_skill_invoked_by_claude_through_the_skill_tool(gateway):
    tool_use = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "verify"}}],
    }
    tool_result = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "Launching skill: verify"},
            {"type": "text", "text": f"Base directory for this skill: /repo/.claude/skills/verify\n\n{BODIES['verify']}"},
        ],
    }
    data = gateway.send(request(user("Is the fix good?"), tool_use, tool_result))
    assert data["model"] == "verify-lane"
    assert routing(data)["skill_hash"]


def test_another_command_tag_before_the_skill_does_not_hide_it(gateway):
    turn = skill_turn("plan")
    turn["content"].insert(0, {"type": "text", "text": "<command-name>/model</command-name>"})
    assert gateway.send(request(turn))["model"] == "plan-lane"


def test_only_the_newest_user_turn_counts(gateway):
    # An invocation from an earlier turn is resent with the history, but it
    # isn't live: a fresh gateway (no sticky route) must route this untagged.
    data = gateway.send(request(skill_turn("plan"), assistant(), user("Unrelated question")))
    assert data["model"] == "untagged"


def test_skill_not_in_catalog_is_untagged(gateway):
    data = gateway.send(request(skill_turn("personal-skill", body="My own notes.\n")))
    assert data["model"] == "untagged"


# --- Sticky routes ---------------------------------------------------------------


def _invoke_plan(gateway, session="session-1"):
    gateway.send(request(skill_turn("plan"), session=session))


def _follow_up(gateway, session="session-1"):
    return gateway.send(request(skill_turn("plan"), assistant(), user("Next?"), session=session))


def test_sticky_route_survives_just_under_idle_timeout(gateway, clock):
    _invoke_plan(gateway)
    clock.advance(cb.STICKY_IDLE_TIMEOUT_SECONDS - 1)
    assert _follow_up(gateway)["model"] == "plan-lane"


def test_sticky_route_expires_after_idle_timeout(gateway, clock, capsys):
    _invoke_plan(gateway)
    clock.advance(cb.STICKY_IDLE_TIMEOUT_SECONDS + 1)
    assert _follow_up(gateway)["model"] == "untagged"
    assert 'llm-trunk expired: {"skill_id": "plan", "why": "idle"' in capsys.readouterr().out


def test_sticky_route_expires_at_max_age_even_while_used(gateway, clock, capsys):
    _invoke_plan(gateway)
    for _ in range(3):  # a real turn every 500 s keeps the idle timer fresh...
        clock.advance(500)
        assert _follow_up(gateway)["model"] == "plan-lane"
    clock.advance(cb.STICKY_MAX_AGE_SECONDS - 1500 + 1)  # ...but not past max age
    assert _follow_up(gateway)["model"] == "untagged"
    assert '"why": "max-age"' in capsys.readouterr().out


def test_reinvoking_resets_max_age(gateway, clock):
    _invoke_plan(gateway)
    clock.advance(1500)
    _invoke_plan(gateway)
    clock.advance(400)  # 1900 s since the first invocation, 400 s since the second
    assert _follow_up(gateway)["model"] == "plan-lane"


def test_sticky_table_is_bounded(gateway, monkeypatch):
    monkeypatch.setattr(cb, "MAX_STICKY_SESSIONS", 2)
    for session in ("a", "b", "c"):
        _invoke_plan(gateway, session)
    assert _follow_up(gateway, "a")["model"] == "untagged"  # oldest evicted
    assert _follow_up(gateway, "c")["model"] == "plan-lane"


# --- Claude Code background calls ---------------------------------------------


def test_session_title_is_background_and_gets_no_effort(gateway):
    data = gateway.send(request(user("<session>fix the login bug</session>"), tools=False))
    assert routing(data)["background"] == "title"
    assert "reasoning_effort" not in data
    assert routing(data)["effort"] is None


def test_title_never_invokes_a_skill(gateway):
    quoted = f"<session><command-name>/plan</command-name>\nBase directory for this skill: x\n\n{BODIES['plan']}</session>"
    data = gateway.send(request(user(quoted), tools=False))
    assert data["model"] == "untagged"


def test_suggestion_rides_the_lane_with_its_effort(gateway):
    _invoke_plan(gateway)
    data = gateway.send(request(skill_turn("plan"), assistant(), user("[SUGGESTION MODE: suggest the next prompt]")))
    assert routing(data)["background"] == "prompt_suggestion"
    assert data["model"] == "plan-lane"
    assert data["reasoning_effort"] == "high"


def test_recap_does_not_keep_a_route_alive(gateway, clock):
    _invoke_plan(gateway)
    clock.advance(500)
    recap = gateway.send(
        request(skill_turn("plan"), assistant(), user("The user stepped away and is coming back. Recap."))
    )
    assert routing(recap)["background"] == "away_summary"
    assert recap["model"] == "plan-lane"  # rides the lane...
    clock.advance(200)  # ...but didn't refresh it: 700 s since the last real turn
    assert _follow_up(gateway)["model"] == "untagged"


def test_background_detection_needs_the_claude_code_header(gateway, clock):
    _invoke_plan(gateway)
    clock.advance(500)
    gateway.send(
        request(skill_turn("plan"), assistant(), user("The user stepped away and is coming back."), claude_code=False)
    )
    clock.advance(200)  # the look-alike counted as a real turn, so the route is still fresh
    assert _follow_up(gateway)["model"] == "plan-lane"


# --- Access control ----------------------------------------------------------------


def test_allowed_skills_limits_which_lanes_a_key_reaches(gateway):
    restricted = key(allowed_skills=["verify"])
    assert gateway.send(request(skill_turn("plan")), restricted)["model"] == "untagged"
    assert gateway.send(request(skill_turn("verify"), session="s2"), restricted)["model"] == "verify-lane"


def test_malformed_allowed_skills_fails_closed(gateway):
    assert gateway.send(request(skill_turn("plan")), key(allowed_skills="plan"))["model"] == "untagged"


def _header_request(skill_id, skill_hash):
    data = request(user("Run it"))
    data["litellm_metadata"]["headers"].update({"x-skill-id": skill_id, "x-skill-hash": skill_hash})
    return data


def test_skill_headers_ignored_without_trust(gateway, catalog):
    data = gateway.send(_header_request("plan", catalog["skills"]["plan"]["sha256"]))
    assert data["model"] == "untagged"


def test_skill_headers_honored_for_trusted_key(gateway, catalog):
    trusted = key(trust_skill_headers=True)
    data = gateway.send(_header_request("plan", catalog["skills"]["plan"]["sha256"]), trusted)
    assert data["model"] == "plan-lane"


def test_trust_flag_must_be_a_real_boolean(gateway, catalog):
    data = gateway.send(_header_request("plan", catalog["skills"]["plan"]["sha256"]), key(trust_skill_headers="true"))
    assert data["model"] == "untagged"


def test_trusted_headers_with_wrong_hash_are_denied(gateway):
    with pytest.raises(HTTPException) as denied:
        gateway.send(_header_request("plan", "0" * 64), key(trust_skill_headers=True))
    assert "changed since it was hashed" in denied.value.detail


# --- How the outgoing request is rewritten ------------------------------------


def test_client_thinking_and_effort_are_replaced_by_the_lane(gateway):
    data = gateway.send(
        request(
            skill_turn("plan"),
            thinking={"type": "adaptive"},
            output_config={"effort": "max", "format": "text"},
        )
    )
    assert "thinking" not in data
    assert data["output_config"] == {"format": "text"}
    assert data["reasoning_effort"] == "high"


def test_empty_output_config_is_removed(gateway):
    data = gateway.send(request(user("Hi"), output_config={"effort": "max"}))
    assert "output_config" not in data


@pytest.mark.parametrize(("requested", "expected"), [(32000, 4000), (1000, 1000), (None, 4000)])
def test_max_tokens_capped_at_lane_max_output(gateway, requested, expected):
    data = request(user("Hi"))
    if requested is None:
        del data["max_tokens"]
    else:
        data["max_tokens"] = requested
    assert gateway.send(data)["max_tokens"] == expected


def test_mid_conversation_system_message_folds_into_user_turn(gateway):
    data = gateway.send(
        request(user("Look at this"), {"role": "system", "content": "Contents of app.py"}, assistant())
    )
    roles = [message["role"] for message in data["messages"]]
    assert roles == ["user", "assistant"]
    assert data["messages"][0]["content"][-1] == {"type": "text", "text": "Contents of app.py"}


def test_leading_system_message_is_kept(gateway):
    data = gateway.send(request({"role": "system", "content": "Be brief."}, user("Hi")))
    assert [message["role"] for message in data["messages"]] == ["system", "user"]


def test_system_message_after_tool_use_goes_after_tool_results(gateway):
    tool_use = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}
    tool_result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}
    data = gateway.send(request(user("Read it"), tool_use, {"role": "system", "content": "ctx"}, tool_result))
    last = data["messages"][-1]["content"]
    assert last[0]["type"] == "tool_result"  # tool results must lead the turn
    assert last[-1] == {"type": "text", "text": "ctx"}


# --- Input size estimate -------------------------------------------------------------


def test_estimate_counts_characters_over_three():
    data = {"messages": [{"role": "user", "content": "x" * 300}]}
    # 300 chars of text plus the keys and role: (4 + 4 + 7 + 300) // 3
    assert cb._estimate_input_tokens(data) == 105


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
