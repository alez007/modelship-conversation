"""Unit tests for the exposed-value enum injection (pure ``_apply_to_tool`` logic).

Run in a Home Assistant test env (the module imports ``homeassistant`` at load). The
function under test is pure — plain dicts/sets, no fixtures.
"""

from custom_components.modelship_conversation.tool_enums import _apply_to_tool


def _turn_off_tool() -> dict:
    # Flat Responses-API shape, as it reaches inject_assist_enums in production.
    return {
        "type": "function",
        "name": "HassTurnOff",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "area": {"type": "string"},
                "domain": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                },
            },
            "required": [],
        },
    }


_NAME_DOMAINS = {
    "sony tv": {"media_player"},
    "Livingroom Lamps": {"input_boolean"},
    "small_bedroom_light": {"light"},
}
_AREA_DOMAINS = {
    "Living Room": {"media_player", "input_boolean"},
    "Small Bedroom": {"light"},
}
_ALL_NAMES = sorted(_NAME_DOMAINS)
_ALL_AREAS = sorted(_AREA_DOMAINS)
_ALL_DOMAINS = sorted({d for ds in _NAME_DOMAINS.values() for d in ds})


def test_generic_tool_gets_full_name_area_and_domain_enums():
    tool = _turn_off_tool()
    _apply_to_tool(
        tool, _NAME_DOMAINS, _ALL_NAMES, _AREA_DOMAINS, _ALL_AREAS, _ALL_DOMAINS, {}
    )
    props = tool["parameters"]["properties"]
    assert set(props["name"]["enum"]) == set(_ALL_NAMES)
    assert set(props["area"]["enum"]) == set(_ALL_AREAS)
    # both anyOf legs of the domain slot are constrained
    assert props["domain"]["anyOf"][0]["enum"] == _ALL_DOMAINS
    assert props["domain"]["anyOf"][1]["items"]["enum"] == _ALL_DOMAINS


def test_empty_anyof_scalar_variant_gets_typed_and_enumed():
    # GetLiveContext's `domain` is `anyOf: [{}, {array}]` — HA renders the scalar leg of
    # vol.Any(str, [str]) as an untyped `{}`. Issue 3: that leg must be treated as the
    # string slot and constrained, not left wide open.
    tool = {
        "type": "function",
        "name": "GetLiveContext",
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {
                    "anyOf": [
                        {},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Filter entities by domain.",
                },
            },
            "required": [],
        },
    }
    _apply_to_tool(
        tool, _NAME_DOMAINS, _ALL_NAMES, _AREA_DOMAINS, _ALL_AREAS, _ALL_DOMAINS, {}
    )
    variants = tool["parameters"]["properties"]["domain"]["anyOf"]
    assert variants[0] == {"type": "string", "enum": _ALL_DOMAINS}
    assert variants[1]["items"]["enum"] == _ALL_DOMAINS


def test_domain_scoped_intent_only_advertises_its_domain():
    # A domain-specific intent (target={"media_player"}) must only offer media entities.
    tool = _turn_off_tool()
    tool["name"] = "HassMediaPause"
    _apply_to_tool(
        tool,
        _NAME_DOMAINS,
        _ALL_NAMES,
        _AREA_DOMAINS,
        _ALL_AREAS,
        _ALL_DOMAINS,
        {"HassMediaPause": {"media_player"}},
    )
    props = tool["parameters"]["properties"]
    assert props["name"]["enum"] == ["sony tv"]
    assert props["domain"]["anyOf"][0]["enum"] == ["media_player"]
