"""Inject exposed-value enums into Home Assistant Assist device tools.

modelship-specific addition (not in upstream ``openai_conversation``). Local models
paraphrase tool-arg values (e.g. ``name="small bedroom light"`` instead of the exposed
``small_bedroom_light``), which HA's intent matcher rejects. Constraining ``name``/``area``
to the exact exposed strings (and dropping the over-filled ``device_class``/``floor``
slots) makes the calls HA-matchable. On vLLM the enum is a soft hint; on llama_cpp with
``constrain_tool_calls`` it becomes a hard GBNF wall. This is "variant B".

The enum is *scoped per intent*: a domain-specific intent (``HassMediaPause`` ->
``media_player``, ``HassLightSet`` -> ``light``) only advertises the entities/areas it can
actually act on, instead of the whole exposed set. Each registered intent handler declares
its target domains via ``platforms`` / ``required_domains`` (see :func:`_intent_domain_map`);
intents with neither (``HassTurnOn``, ``GetState``, ...) are genuinely generic and keep the
full enum. This keeps the per-request tool payload small — without it, the entire exposed
entity list is duplicated into every device tool.

Kept deliberately self-contained so re-syncs with upstream stay easy: ``entity.py`` only
calls :func:`inject_assist_enums`. The only HA-core coupling is intentionally fail-soft —
handler attributes are read via ``getattr`` and a missing/unknown intent degrades to the
full enum (today's behavior), never a crash or a silently-dropped valid entity.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    intent,
)

from .const import LOGGER

# Exposure key used by the conversation platform.
_ASSISTANT = "conversation"

# Slots small models over-fill with garbage; dropped from every device tool. ``domain`` is
# intentionally kept (dropping it was worse on the 0.5B) but is now enum-scoped like
# name/area below — left as a free string array, the 270M fills it with entity names
# (e.g. domain=["forecast home", ...]) -> MatchFailedError(DOMAIN).
_TRIM_PROPS = ("device_class", "floor")


def inject_assist_enums(hass: HomeAssistant, tools: list[dict[str, Any]]) -> None:
    """Mutate each Assist tool schema in place (variant B).

    ``tools`` must contain only the Assist LLM API tools (call before web_search /
    code_interpreter / image tools are appended).
    """
    name_domains, area_domains = _collect_exposed(hass)
    all_names = sorted(n for n in name_domains if n)
    all_areas = sorted(a for a in area_domains if a)
    all_domains = sorted({d for doms in name_domains.values() for d in doms})
    domain_map = _intent_domain_map(hass)
    LOGGER.debug(
        "inject_assist_enums: %d exposed names, %d areas, %d domain-scoped intents; "
        "names=%s areas=%s",
        len(all_names),
        len(all_areas),
        len(domain_map),
        all_names[:30],
        all_areas,
    )
    for tool in tools:
        _apply_to_tool(
            tool, name_domains, all_names, area_domains, all_areas, all_domains, domain_map
        )


def _apply_to_tool(
    tool: dict[str, Any],
    name_domains: dict[str, set[str]],
    all_names: list[str],
    area_domains: dict[str, set[str]],
    all_areas: list[str],
    all_domains: list[str],
    domain_map: dict[str, set[str]],
) -> None:
    """Pure schema mutation — unit-testable without Home Assistant."""
    if not isinstance(tool, dict) or tool.get("type") != "function":
        return
    
    # Handle nested or flat structure
    fn_def = tool.get("function")
    if isinstance(fn_def, dict):
        params = fn_def.get("parameters")
        name = fn_def.get("name")
    else:
        params = tool.get("parameters")
        name = tool.get("name")

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

    # Target domains for this intent (None == generic -> advertise everything). Tool names
    # are the (slugified) intent_type; an unknown name degrades safely to the full enum.
    target = domain_map.get(name)
    names = all_names if target is None else [n for n in all_names if name_domains[n] & target]
    areas = all_areas if target is None else [a for a in all_areas if area_domains[a] & target]
    domains = all_domains if target is None else sorted(target)

    # Copy-on-write, not in-place: HA's converted intent schemas reuse one shared dict
    # object for several slots (e.g. HassTurnOn's `name` and `area` are the *same* dict),
    # so `props["name"]["enum"] = ...` would also overwrite `area`'s enum (last write wins).
    if names and isinstance(props.get("name"), dict):
        props["name"] = {**props["name"], "enum": names}
    if areas and isinstance(props.get("area"), dict):
        props["area"] = {**props["area"], "enum": areas}
    # ``domain`` is an array of strings; constrain its *items* to real exposed domains so
    # the grammar can't accept hallucinated entity-names there (the emitter enforces an
    # enum on array items). Copy-on-write at both levels for the same shared-dict reason.
    dom = props.get("domain")
    if domains and isinstance(dom, dict):
        if "anyOf" in dom and isinstance(dom["anyOf"], list):
            new_anyof = []
            for variant in dom["anyOf"]:
                if not isinstance(variant, dict):
                    new_anyof.append(variant)
                elif variant.get("type") == "string":
                    new_anyof.append({**variant, "enum": domains})
                elif variant.get("type") == "array" and isinstance(variant.get("items"), dict):
                    new_anyof.append({**variant, "items": {**variant["items"], "enum": domains}})
                elif "type" not in variant:
                    # HA renders the scalar leg of ``vol.Any(str, [str])`` as an untyped
                    # ``{}`` (GetLiveContext's ``domain``): the type info is lost so it
                    # accepts anything, and the branches above skip it -> the single-domain
                    # form stays unconstrained. Treat it as the string slot and enum it.
                    new_anyof.append({**variant, "type": "string", "enum": domains})
                else:
                    new_anyof.append(variant)
            props["domain"] = {**dom, "anyOf": new_anyof}
        elif isinstance(dom.get("items"), dict):
            props["domain"] = {**dom, "items": {**dom["items"], "enum": domains}}


def _intent_domain_map(hass: HomeAssistant) -> dict[str, set[str]]:
    """Map intent_type -> target entity domains for domain-specific intents.

    Built from the live intent registry: a handler's target domains are
    ``platforms ∪ required_domains`` (e.g. ``HassMediaPause`` -> ``{"media_player"}``,
    ``HassLightSet`` -> ``{"light"}``). Generic intents (``HassTurnOn``, ``GetState``, ...)
    declare neither and are omitted, so callers treat a missing key as "no scoping".

    Fail-soft on purpose: attributes are read via ``getattr`` so a HA-core rename drops the
    intent from the map (-> full enum) rather than raising.
    """
    out: dict[str, set[str]] = {}
    for handler in intent.async_get(hass):
        domains: set[str] = set()
        platforms = getattr(handler, "platforms", None)
        if platforms:
            domains.update(platforms)
        required = getattr(handler, "required_domains", None)
        if required:
            domains.update(required)
        if domains:
            out[handler.intent_type] = domains
    return out


def _collect_exposed(
    hass: HomeAssistant,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Collect exposed entity-names and area-names, each mapped to their domain(s).

    Names/areas are the exact strings HA's matcher accepts; the domain sets let
    :func:`_apply_to_tool` keep only the values a given intent can target.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    name_domains: dict[str, set[str]] = {}
    area_domains_by_id: dict[str, set[str]] = {}
    for state in hass.states.async_all():
        if not async_should_expose(hass, _ASSISTANT, state.entity_id):
            continue
        domain = state.entity_id.split(".", 1)[0]
        name_domains.setdefault(state.name, set()).add(domain)  # exposed friendly name
        entry = ent_reg.async_get(state.entity_id)
        if entry is None:
            continue
        # aliases may contain HA's ComputedNameType singleton (means "the computed full
        # name", already covered by state.name) alongside real string aliases.
        for alias in entry.aliases:
            if isinstance(alias, str):
                name_domains.setdefault(alias, set()).add(domain)
        area_id = entry.area_id
        if area_id is None and entry.device_id:
            device = dev_reg.async_get(entry.device_id)
            area_id = device.area_id if device else None
        if area_id:
            area_domains_by_id.setdefault(area_id, set()).add(domain)

    area_domains: dict[str, set[str]] = {}
    for area_id, domains in area_domains_by_id.items():
        area = area_reg.async_get_area(area_id)
        if area is None:
            continue
        area_domains.setdefault(area.name, set()).update(domains)
        for alias in area.aliases:
            if isinstance(alias, str):
                area_domains.setdefault(alias, set()).update(domains)

    return name_domains, area_domains
