"""Unit tests for stateless-mode history dropping (pure, no HA needed)."""

from custom_components.modelship_conversation.history import drop_history


def _dev(text="prompt"):
    return {"type": "message", "role": "developer", "content": text}


def _user(text):
    return {"type": "message", "role": "user", "content": text}


def _assistant(text):
    return {"type": "message", "role": "assistant", "content": text}


def _call(name):
    return {"type": "function_call", "name": name, "arguments": "{}", "call_id": "c1"}


def _output():
    return {"type": "function_call_output", "call_id": "c1", "output": "{}"}


def test_drops_prior_turns_keeps_prompt_and_current_user():
    messages = [
        _dev(),
        _user("turn on my sony tv"),
        _call("HassTurnOn"),
        _output(),
        _assistant("Done."),
        _user("turn off the tv"),
    ]
    assert drop_history(messages) == [_dev(), _user("turn off the tv")]


def test_keeps_current_turn_tool_scaffolding():
    # The in-flight roundtrip (call + output after the last user msg) must be preserved,
    # or GetLiveContext-style answers would lose the tool result mid-turn.
    messages = [
        _dev(),
        _user("is the tv on"),
        _assistant("hi"),
        _user("what about the lamp"),
        _call("GetLiveContext"),
        _output(),
    ]
    assert drop_history(messages) == [
        _dev(),
        _user("what about the lamp"),
        _call("GetLiveContext"),
        _output(),
    ]


def test_noop_when_user_is_first_real_turn():
    # Nothing earlier than the user turn to drop -> unchanged (no duplicated prompt).
    messages = [_dev(), _user("hi"), _call("X"), _output()]
    assert drop_history(messages) is messages


def test_noop_when_no_user_message():
    messages = [_dev(), _assistant("hello")]
    assert drop_history(messages) is messages
