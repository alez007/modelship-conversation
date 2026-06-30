"""Modelship-specific behaviour layered on top of the vanilla ``openai_conversation`` entity.

All of the fork's runtime features live here (and in the sibling :mod:`history`,
:mod:`tool_enums` and :mod:`tool_narrower` modules) so that ``entity.py`` and ``config_flow.py``
stay near-verbatim copies of upstream Home Assistant Core: they call into this module at a
handful of one-line hook points. That keeps re-syncing against a newer Core trivial — see
``NOTICE`` / ``README`` for the recipe.

Every feature here is opt-in and off by default, so with the defaults the integration behaves
exactly like upstream ``openai_conversation`` against a capable model. The toggles exist to make
tiny local models (FunctionGemma-270M, Qwen3-0.6B, ...) usable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from openai.types.responses import ToolChoiceFunctionParam, ToolParam
import voluptuous as vol

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant

from .history import drop_history
from .tool_enums import inject_assist_enums
from .tool_narrower import narrow_tools

# Subentry option keys + defaults for the small-model tweaks. Persisted in config entries, so
# the string values must stay stable. Kept here (not in const.py) so const.py is a pure rename
# of upstream.
CONF_NARROW_TOOLS = "narrow_tools"
CONF_NARROW_MAX_TOOLS = "narrow_max_tools"
CONF_STATELESS = "stateless"
CONF_STOP_AFTER_ACTION = "stop_after_action"

RECOMMENDED_NARROW_TOOLS = False
RECOMMENDED_NARROW_MAX_TOOLS = 6
RECOMMENDED_STATELESS = False
RECOMMENDED_STOP_AFTER_ACTION = False


def _latest_user_text(chat_log: conversation.ChatLog) -> str:
    """Return the most recent user-turn text from the chat log (for tool narrowing)."""
    for content in reversed(chat_log.content):
        if getattr(content, "role", None) == "user":
            text = getattr(content, "content", None)
            if isinstance(text, str) and text:
                return text
    return ""


def _completed_action_speech(
    contents: Iterable[conversation.Content],
) -> str | None:
    """If a control intent just completed successfully, return its confirmation speech.

    Small local models keep emitting tool calls after a successful action instead of
    ending the turn with text — burning iterations and polluting the next turn's
    history (see the FunctionGemma findings). When ``stop_after_action`` is on, the
    caller uses this to end the turn as soon as an action lands. Returns ``None`` for
    query results (e.g. ``GetLiveContext``) so multi-step "check then act" flows still
    round-trip back to the model.
    """
    for content in contents:
        if not isinstance(content, conversation.ToolResultContent):
            continue
        result = content.tool_result
        if not isinstance(result, dict) or result.get("response_type") != "action_done":
            continue
        data = result.get("data") or {}
        if not data.get("success") or data.get("failed"):
            continue
        speech = result.get("speech")
        if isinstance(speech, dict):
            plain = speech.get("plain")
            if isinstance(plain, dict) and plain.get("speech"):
                return str(plain["speech"])
        return "Done."
    return None


def apply_stateless(
    options: Mapping[str, Any], messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Stateless mode: send only this utterance (+ developer prompt and the current turn's
    tool scaffolding), so a tiny model can't copy its own earlier mistakes out of the replayed
    history. Sliced before ``model_args`` so ``input == messages``. No-op when off (default).
    """
    if options.get(CONF_STATELESS, RECOMMENDED_STATELESS):
        return drop_history(messages)
    return messages


async def prepare_tools(
    hass: HomeAssistant,
    options: Mapping[str, Any],
    tools: list[ToolParam],
    chat_log: conversation.ChatLog,
) -> str | None:
    """Constrain device-tool name/area to exact exposed values, and (opt-in) narrow the tool
    list to the utterance's likely domains for small local models.

    Returns the name of a tool to force on the first turn for live-state queries (see
    :func:`force_first_tool`), or ``None``.
    """
    # Constrain device-tool name/area to exact exposed values (variant B).
    inject_assist_enums(hass, tools)

    # Narrowing would hurt large models that handle the full catalog, so it is opt-in.
    if not options.get(CONF_NARROW_TOOLS, RECOMMENDED_NARROW_TOOLS):
        return None
    return await narrow_tools(
        hass,
        tools,
        _latest_user_text(chat_log),
        options.get(CONF_NARROW_MAX_TOOLS, RECOMMENDED_NARROW_MAX_TOOLS),
    )


def force_first_tool(
    force_tool: str | None, tools: list[ToolParam], model_args: dict[str, Any]
) -> bool:
    """Pin the narrower's chosen tool via ``tool_choice`` for the first turn, so a small model
    fetches real state instead of answering from nothing. Skipped if image generation already
    claimed ``tool_choice``. Returns whether a tool was pinned (caller releases it after the
    first call, see :func:`release_forced_tool`).
    """
    if not force_tool or "tool_choice" in model_args:
        return False
    forced = next(
        (t for t in tools if isinstance(t, dict) and t.get("name") == force_tool),
        None,
    )
    if forced is None:
        return False
    model_args["tool_choice"] = ToolChoiceFunctionParam(type="function", name=force_tool)
    # Strip the filter slots from a forced GetLiveContext: a small model fills name/area/domain
    # with arbitrary enum values unrelated to the question, so the empty call returns the full
    # live context that holds the answer.
    if force_tool == "GetLiveContext":
        forced["parameters"] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    return True


def release_forced_tool(forced_first_turn: bool, model_args: dict[str, Any]) -> bool:
    """Drop the one-shot forced ``tool_choice`` after the first call so the synthesis turn (and
    any further rounds) sample freely. Returns the new flag value (always ``False``).
    """
    if forced_first_turn:
        model_args.pop("tool_choice", None)
    return False


def stop_after_action(
    options: Mapping[str, Any],
    new_content: Iterable[conversation.Content],
    chat_log: conversation.ChatLog,
    agent_id: str,
) -> bool:
    """Opt-in: with a small local model, end the turn as soon as a control intent completes
    instead of round-tripping the success back (the model would just keep emitting tool calls).
    Ends the turn with the intent's own confirmation so HA has a final assistant turn to speak.

    Returns ``True`` when the turn was ended (caller should ``break``). No-op when off (default)
    so capable models can still chain multi-action requests.
    """
    if not options.get(CONF_STOP_AFTER_ACTION, RECOMMENDED_STOP_AFTER_ACTION):
        return False
    speech = _completed_action_speech(new_content)
    if speech is None:
        return False
    chat_log.async_add_assistant_content_without_tools(
        conversation.AssistantContent(agent_id=agent_id, content=speech)
    )
    return True


def add_options_schema(step_schema: dict[Any, Any]) -> None:
    """Add the small-model toggles to the conversation options step schema (config flow)."""
    step_schema.update(
        {
            vol.Optional(
                CONF_NARROW_TOOLS, default=RECOMMENDED_NARROW_TOOLS
            ): bool,
            vol.Optional(
                CONF_STOP_AFTER_ACTION, default=RECOMMENDED_STOP_AFTER_ACTION
            ): bool,
            vol.Optional(CONF_STATELESS, default=RECOMMENDED_STATELESS): bool,
        }
    )
