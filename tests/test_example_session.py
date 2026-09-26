"""The README's "Example Session" table, one test per row."""
import pytest
from conftest import (
    BODIES,
    HTTPException,
    assistant,
    compaction_request,
    request,
    routing,
    skill_turn,
    subagent_request,
    unregistered_turn,
    user,
)


def test_row1_plain_question_goes_to_the_lowest_tier(gateway):
    data = gateway.send(request(user("What does this repo do?")))
    assert (data["model"], data["reasoning_effort"]) == ("light", "low")
    assert (routing(data)["request_type"], routing(data)["tier"]) == ("normal", "light")


def test_row2_complex_skill_gets_the_complex_tier(gateway):
    data = gateway.send(request(user("Hi"), assistant(), skill_turn("plan")))
    assert (data["model"], data["reasoning_effort"]) == ("complex", "high")
    assert routing(data)["request_type"] == "skill" and routing(data)["skill_hash"]


def test_row3_follow_up_stays_on_the_tier(gateway):
    gateway.send(request(skill_turn("plan")))
    data = gateway.send(request(skill_turn("plan"), assistant(), user("What about edge cases?")))
    assert data["model"] == "complex"
    assert routing(data)["request_type"] == "normal"  # a follow-up is a normal turn, on the skill's tier


def test_row4_moderate_skill_switches_tier(gateway):
    gateway.send(request(skill_turn("plan")))
    data = gateway.send(request(skill_turn("plan"), assistant(), skill_turn("review")))
    assert (data["model"], data["reasoning_effort"]) == ("moderate", "medium")


def test_row5_subagent_runs_one_tier_below(gateway):
    gateway.send(request(skill_turn("review")))
    data = gateway.send(subagent_request())
    assert data["model"] == "light"
    assert routing(data)["request_type"] == "subagent"


def test_row6_unregistered_skill_drops_to_the_lowest_tier_and_stays(gateway):
    gateway.send(request(skill_turn("plan")))
    data = gateway.send(request(skill_turn("plan"), assistant(), unregistered_turn()))
    assert data["model"] == "light"
    assert routing(data)["unregistered_skill"] == "personal-notes"
    follow_up = gateway.send(request(skill_turn("plan"), assistant(), unregistered_turn(), assistant(), user("And?")))
    assert follow_up["model"] == "light"


def test_row7_compaction_stays_on_the_tier_even_past_the_cap(gateway):
    gateway.send(request(skill_turn("plan")))
    history = [skill_turn("plan"), assistant("x" * 600_000)]  # ~200k tokens: over every cap
    data = gateway.send(compaction_request(*history, user("Keep going")))
    assert data["model"] == "complex"
    assert routing(data)["request_type"] == "compaction"


def test_row8_edited_skill_is_denied_as_stale(gateway):
    edited = BODIES["plan"].replace("Review", "Assess")
    with pytest.raises(HTTPException) as denied:
        gateway.send(request(skill_turn("plan", body=edited)))
    assert denied.value.status_code == 422
    assert "changed since it was hashed" in denied.value.detail


def test_row8b_appended_line_is_denied_as_stale(gateway):
    with pytest.raises(HTTPException) as denied:
        gateway.send(request(skill_turn("plan", body=BODIES["plan"] + "Also check the logs.\n")))
    assert "changed since it was hashed" in denied.value.detail


def test_row9_huge_paste_hits_the_lowest_tier_cap(gateway):
    with pytest.raises(HTTPException) as denied:
        gateway.send(request(user("x" * 200_000)))
    assert denied.value.status_code == 422
    assert "exceeds the light tier cap (64000)" in denied.value.detail


def test_row10_new_session_inherits_nothing(gateway):
    gateway.send(request(skill_turn("plan"), session="old-session"))
    data = gateway.send(request(user("Look at @src/app.py"), session="new-session"))
    assert data["model"] == "light"
