"""Drop replayed conversation history for small local models ("stateless" mode).

modelship-specific addition (not in upstream ``openai_conversation``). A tiny local model
(FunctionGemma-270M, 0.5B, ...) can't hold a real multi-turn conversation, and worse, it
copies its own earlier — sometimes wrong — tool calls out of the replayed history: a single
mis-fired ``HassTurnOff`` becomes a byte-identical loop on every following "turn off ..."
because the bad call is right there in the context to imitate. In stateless mode we send
only the current utterance instead.

Kept deliberately self-contained so re-syncs with upstream stay easy: ``entity.py`` only
calls :func:`drop_history` (mirroring how it calls :func:`.tool_enums.inject_assist_enums`
and :func:`.tool_narrower.narrow_tools`).
"""

from __future__ import annotations

from typing import Any


def drop_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the developer prompt + only the latest user turn and its tool scaffolding.

    The slice is ``messages[:1] + messages[last_user:]``:

    * ``messages[0]`` is always the developer prompt, which carries the static device
      context, so device knowledge ("do I have bedroom lights?") is never lost.
    * The current turn's ``function_call`` / ``function_call_output`` items are always
      appended *after* the user message, so slicing from the last user index keeps the
      in-flight tool roundtrip (``GetLiveContext`` etc.) intact across the agentic loop.

    Returns the list unchanged when there is nothing earlier to drop: no user message, or
    the latest one already sits right after the prompt. Pure and HA-free for easy testing.
    """
    last_user = next(
        (
            i
            for i in range(len(messages) - 1, -1, -1)
            if isinstance(messages[i], dict) and messages[i].get("role") == "user"
        ),
        None,
    )
    if last_user is None or last_user <= 1:
        return messages
    return messages[:1] + messages[last_user:]
