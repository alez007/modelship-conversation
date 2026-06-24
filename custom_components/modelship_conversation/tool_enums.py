"""Inject exposed-value enums into Home Assistant Assist device tools.

modelship-specific addition (not in upstream ``openai_conversation``). Local models
paraphrase tool-arg values (e.g. ``name="small bedroom light"`` instead of the exposed
``small_bedroom_light``), which HA's intent matcher rejects. Constraining ``name``/``area``
to the exact exposed strings (and dropping the over-filled ``device_class``/``floor``
slots) makes the calls HA-matchable. On vLLM the enum is a soft hint; on llama_cpp with
``constrain_tool_calls`` it becomes a hard GBNF wall. This is "variant B".

Kept deliberately self-contained so re-syncs with upstream stay easy: ``entity.py`` only
calls :func:`inject_assist_enums`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

# Exposure key used by the conversation platform.
_ASSISTANT = "conversation"

# Slots small models over-fill with garbage; dropped from every device tool. ``domain`` is
# intentionally kept (dropping it was worse on the 0.5B — free domain array -> garbage).
_TRIM_PROPS = ("device_class", "floor")


def inject_assist_enums(hass: HomeAssistant, tools: list[dict[str, Any]]) -> None:
    """Mutate each Assist tool schema in place (variant B).

    ``tools`` must contain only the Assist LLM API tools (call before web_search /
    code_interpreter / image tools are appended).
    """
    names, areas = _collect_exposed(hass)
    for tool in tools:
        _apply_to_tool(tool, names, areas)


def _apply_to_tool(
    tool: dict[str, Any], names: list[str], areas: list[str]
) -> None:
    """Pure schema mutation — unit-testable without Home Assistant."""
    if not isinstance(tool, dict) or tool.get("type") != "function":
        return
    params = tool.get("parameters")
    if not isinstance(params, dict):
        return
    props = params.get("properties")
    if not isinstance(props, dict):
        return

    required = params.get("required")
    for prop in _TRIM_PROPS:
        props.pop(prop, None)
        if isinstance(required, list) and prop in required:
            required.remove(prop)

    if names and isinstance(props.get("name"), dict):
        props["name"]["enum"] = names
    if areas and isinstance(props.get("area"), dict):
        props["area"]["enum"] = areas


def _collect_exposed(hass: HomeAssistant) -> tuple[list[str], list[str]]:
    """Collect the exact exposed entity-names and area-names the matcher accepts."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    names: set[str] = set()
    area_ids: set[str] = set()
    for state in hass.states.async_all():
        if not async_should_expose(hass, _ASSISTANT, state.entity_id):
            continue
        names.add(state.name)  # the exposed friendly name
        entry = ent_reg.async_get(state.entity_id)
        if entry is None:
            continue
        names.update(entry.aliases)
        area_id = entry.area_id
        if area_id is None and entry.device_id:
            device = dev_reg.async_get(entry.device_id)
            area_id = device.area_id if device else None
        if area_id:
            area_ids.add(area_id)

    areas: set[str] = set()
    for area_id in area_ids:
        area = area_reg.async_get_area(area_id)
        if area:
            areas.add(area.name)
            areas.update(area.aliases)

    return sorted(n for n in names if n), sorted(a for a in areas if a)
