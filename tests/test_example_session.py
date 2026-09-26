"""The README's "Example Session" table, one test per row."""
import pytest
from conftest import BODIES, HTTPException, assistant, request, routing, skill_turn, user


def test_row1_plain_question_is_untagged(gateway):
    data = gateway.send(request(user("What does this repo do?")))
    assert data["model"] == "untagged"
    assert data["reasoning_effort"] == "low"
    assert routing(data)["skill_id"] is None


def test_row2_invoked_skill_gets_its_lane(gateway):
    data = gateway.send(request(user("Hi"), assistant(), skill_turn("plan")))
    assert data["model"] == "plan-lane"
    assert data["reasoning_effort"] == "high"
    assert routing(data)["skill_id"] == "plan"
    assert routing(data)["skill_hash"]  # invoked this turn


def test_row3_follow_up_stays_on_the_lane(gateway):
    history = [skill_turn("plan"), assistant("Here is the plan.")]
    gateway.send(request(*history[:1]))
    data = gateway.send(request(*history, user("What about edge cases?")))
    assert data["model"] == "plan-lane"
    assert routing(data)["skill_id"] == "plan"
    assert routing(data)["skill_hash"] is None  # sticky, not re-invoked


def test_row4_other_skill_switches_lane(gateway):
    gateway.send(request(skill_turn("plan")))
    data = gateway.send(request(skill_turn("plan"), assistant(), skill_turn("verify")))
    assert data["model"] == "verify-lane"
    assert data["reasoning_effort"] == "low"


def test_row7_new_session_inherits_nothing(gateway):
    gateway.send(request(skill_turn("plan"), session="old-session"))
    data = gateway.send(request(user("Look at @src/app.py"), session="new-session"))
    assert data["model"] == "untagged"


def test_row8_changed_word_is_denied_as_stale(gateway):
    edited = BODIES["plan"].replace("Write", "Draft")
    with pytest.raises(HTTPException) as denied:
        gateway.send(request(skill_turn("plan", body=edited)))
    assert denied.value.status_code == 422
    assert "changed since it was hashed" in denied.value.detail


def test_row9_appended_line_is_denied_as_stale(gateway):
    appended = BODIES["plan"] + "Also check the logs.\n"
    with pytest.raises(HTTPException) as denied:
        gateway.send(request(skill_turn("plan", body=appended)))
    assert denied.value.status_code == 422
    assert "changed since it was hashed" in denied.value.detail


def test_row10_huge_paste_hits_the_untagged_cap(gateway):
    with pytest.raises(HTTPException) as denied:
        gateway.send(request(user("x" * 200_000)))
    assert denied.value.status_code == 422
    assert "exceeds the untagged lane cap (64000)" in denied.value.detail
    assert "run /compact or start a new session" in denied.value.detail
