"""Unit tests for the per-utterance tool narrower (pure logic, no HA cluster needed).

Run in a Home Assistant test env (the module imports ``homeassistant`` at load). The
function under test (``_select``) is pure — it takes plain dicts/sets, no fixtures required.
"""

from custom_components.modelship_conversation.tool_narrower import _select


def _f(name: str) -> dict:
    return {"type": "function", "name": name, "parameters": {}}


_TOOLS = [
    _f(n)
    for n in (
        "HassTurnOn", "HassTurnOff", "GetLiveContext", "HassLightSet", "HassMediaPause",
        "HassMediaNext", "HassMediaUnpause", "HassSetVolume", "HassVacuumStart",
        "HassSetPosition", "HassBroadcast", "HassCancelAllTimers", "HassGetWeather",
    )
]
_DMAP = {
    "HassLightSet": {"light"},
    "HassMediaPause": {"media_player"},
    "HassMediaNext": {"media_player"},
    "HassMediaUnpause": {"media_player"},
    "HassSetVolume": {"media_player"},
    "HassVacuumStart": {"vacuum"},
    "HassSetPosition": {"cover"},
    "HassGetWeather": {"weather"},
}


def _names(kept):
    return [t["name"] for t in kept]


def test_media_keeps_media_and_core_drops_noise():
    kept = _select(_TOOLS, {"HassMediaPause"}, {"media_player"}, _DMAP, 99, {}, {})
    assert set(_names(kept)) == {
        "HassTurnOn", "HassTurnOff", "GetLiveContext",
        "HassMediaPause", "HassMediaNext", "HassMediaUnpause", "HassSetVolume",
    }


def test_domain_only_signal_keeps_core_plus_domain_tool():
    kept = _select(_TOOLS, set(), {"light"}, _DMAP, 6, {}, {})
    assert set(_names(kept)) == {"HassTurnOn", "HassTurnOff", "GetLiveContext", "HassLightSet"}


def test_generic_match_narrows_to_core():
    # "turn on my sony tv": hassil matches the generic HassTurnOn (no domain slot). That IS a
    # signal -> narrow to core only. No media tools are offered, so the model cannot mis-pick
    # HassMediaUnpause for a power command.
    kept = _select(_TOOLS, {"HassTurnOn"}, set(), _DMAP, 6, {}, {})
    assert _names(kept) == ["HassTurnOn", "HassTurnOff", "GetLiveContext"]


def test_no_signal_strips_all_action_tools():
    # hassil matched nothing (greeting / chit-chat / broken hassil) -> fail closed: every
    # action tool is stripped so a tiny model can't hallucinate a call on "hi".
    assert _select(_TOOLS, set(), set(), _DMAP, 6, {}, {}) == []


def test_cap_keeps_core_first_then_matched():
    tools = [_f("HassTurnOn"), _f("HassTurnOff"), _f("GetLiveContext")] + [
        _f(f"X{i}") for i in range(5)
    ]
    dmap = {f"X{i}": {"media_player"} for i in range(5)}
    kept = _select(tools, {"X0"}, {"media_player"}, dmap, 4, {}, {})
    assert len(kept) == 4
    names = _names(kept)
    # all 3 core tools survive the cap (never evicted) and come first
    assert names[:3] == ["HassTurnOn", "HassTurnOff", "GetLiveContext"]
    assert "X0" in names  # matched intent takes the one remaining slot


def test_cap_never_evicts_core_even_when_full():
    # many matched intents must not push the static core out (the bug: HassTurnOn
    # vanished for "turn on the tv" because 5 media intents filled the cap).
    tools = [_f("HassTurnOn"), _f("HassTurnOff"), _f("GetLiveContext")] + [
        _f(f"M{i}") for i in range(5)
    ]
    matched = {f"M{i}" for i in range(5)}
    kept = _names(_select(tools, matched, set(), {}, 6, {}, {}))
    assert {"HassTurnOn", "HassTurnOff", "GetLiveContext"}.issubset(kept)
    assert kept[:3] == ["HassTurnOn", "HassTurnOff", "GetLiveContext"]


def test_non_function_tool_always_preserved_and_exempt_from_cap():
    extra = {"type": "web_search"}
    kept = _select([*_TOOLS, extra], {"HassMediaPause"}, {"media_player"}, _DMAP, 6, {}, {})
    assert extra in kept


def test_non_function_tool_survives_no_signal_strip():
    # even when all action tools are stripped, passthrough (web_search, ...) is preserved.
    extra = {"type": "web_search"}
    assert _select([*_TOOLS, extra], set(), set(), _DMAP, 6, {}, {}) == [extra]


def test_broaden_arrays_rewrites_to_anyof():
    tools = [
        {
            "type": "function",
            "name": "HassTurnOn",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["light", "switch"]}
                    },
                    "other_prop": {
                        "type": "string"
                    }
                }
            }
        }
    ]
    kept = _select(tools, {"HassTurnOn"}, set(), {}, 6, {}, {})
    domain_prop = kept[0]["parameters"]["properties"]["domain"]
    assert "anyOf" in domain_prop
    assert domain_prop["anyOf"][0] == {"type": "string", "enum": ["light", "switch"]}
    assert domain_prop["anyOf"][1] == {"type": "array", "items": {"type": "string", "enum": ["light", "switch"]}}


def test_narrow_generic_enums_name_area_domain():
    tools = [
        {
            "type": "function",
            "name": "HassTurnOn",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["sony tv", "Livingroom Lamps", "small_bedroom_light"]
                    },
                    "area": {
                        "type": "string",
                        "enum": ["Living Room", "Small Bedroom"]
                    },
                    "domain": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["light", "media_player", "vacuum"]}
                    }
                }
            }
        }
    ]
    name_domains = {
        "sony tv": {"media_player"},
        "Livingroom Lamps": {"light"},
        "small_bedroom_light": {"light"}
    }
    area_domains = {
        "Living Room": {"light", "media_player"},
        "Small Bedroom": {"light"}
    }
    
    kept = _select(tools, {"HassTurnOn"}, {"light"}, {}, 6, name_domains, area_domains)
    properties = kept[0]["parameters"]["properties"]
    
    # "sony tv" should be excluded from "name" enum because its domain is "media_player" (not "light")
    assert set(properties["name"]["enum"]) == {"Livingroom Lamps", "small_bedroom_light"}
    
    # Both rooms have light, so both remain
    assert set(properties["area"]["enum"]) == {"Living Room", "Small Bedroom"}
    
    # "domain" enum rewritten and narrowed
    domain_prop = properties["domain"]
    assert "anyOf" in domain_prop
    assert domain_prop["anyOf"][0]["enum"] == ["light"]
    assert domain_prop["anyOf"][1]["items"]["enum"] == ["light"]


def test_narrow_generic_enums_keeps_full_enum_if_none_match():
    # No-drop guard: when the detected domain filters an enum down to nothing, keep the
    # FULL enum rather than stripping it. A bare slot lets the model hallucinate any string,
    # which is strictly worse than an over-broad but still-valid enum. This is the second
    # line of defense behind narrow_tools' exposed-domain intersection: even if a false
    # domain leaks through (the "turn off the tv" -> scene/script regression), the sony-tv
    # enum survives instead of vanishing.
    tools = [
        {
            "type": "function",
            "name": "HassTurnOn",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["sony tv"]
                    },
                    "area": {
                        "type": "string",
                        "enum": ["Small Bedroom"]
                    },
                    "domain": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["media_player"]}
                    }
                }
            }
        }
    ]
    name_domains = {"sony tv": {"media_player"}}
    area_domains = {"Small Bedroom": {"light"}}

    # domains is {"vacuum"} — nothing matches, so every enum is kept intact.
    kept = _select(tools, {"HassTurnOn"}, {"vacuum"}, {}, 6, name_domains, area_domains)
    properties = kept[0]["parameters"]["properties"]

    assert properties["name"]["enum"] == ["sony tv"]
    assert properties["area"]["enum"] == ["Small Bedroom"]

    # domain array is still broadened to anyOf (step 1), but both variants keep their enum.
    domain_prop = properties["domain"]
    assert "anyOf" in domain_prop
    assert domain_prop["anyOf"][0]["enum"] == ["media_player"]
    assert domain_prop["anyOf"][1]["items"]["enum"] == ["media_player"]


def test_false_domain_does_not_blank_enums_regression():
    # Regression: hassil false-matched "turn off the tv" to scene/script turn-off templates
    # and reported domains={scene, script}, none of which the house exposes. The old code
    # then filtered every generic enum to empty and DELETED it, so the tools reached the
    # model bare and it free-typed a wrong call. narrow_tools now intersects detected
    # domains with exposed domains (dropping scene/script -> empty -> no narrowing), and
    # _select keeps full enums on an empty filter. Both mean the sony-tv enum must survive.
    tools = [
        {
            "type": "function",
            "name": "HassTurnOff",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": ["sony tv", "Livingroom Lamps"]},
                    "area": {"type": "string", "enum": ["Living Room"]},
                },
            },
        }
    ]
    name_domains = {"sony tv": {"media_player"}, "Livingroom Lamps": {"input_boolean"}}
    area_domains = {"Living Room": {"media_player", "input_boolean"}}

    # A bogus domain leaking into _select must not strip the enums.
    kept = _select(tools, {"HassTurnOff"}, {"scene", "script"}, {}, 6, name_domains, area_domains)
    props = kept[0]["parameters"]["properties"]
    assert set(props["name"]["enum"]) == {"sony tv", "Livingroom Lamps"}
    assert props["area"]["enum"] == ["Living Room"]


def test_nested_openai_structure_enum_narrowing_and_broadening():
    # Simulate a nested tool as passed in production (OpenAI format)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "HassTurnOn",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": ["sony tv", "Livingroom Lamps", "small_bedroom_light"]
                        },
                        "area": {
                            "type": "string",
                            "enum": ["Living Room", "Small Bedroom"]
                        },
                        "domain": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["light", "media_player", "vacuum"]}
                        }
                    }
                }
            }
        }
    ]
    name_domains = {
        "sony tv": {"media_player"},
        "Livingroom Lamps": {"light"},
        "small_bedroom_light": {"light"}
    }
    area_domains = {
        "Living Room": {"light", "media_player"},
        "Small Bedroom": {"light"}
    }
    
    # Run _select with domains = {"light"}
    kept = _select(tools, {"HassTurnOn"}, {"light"}, {}, 6, name_domains, area_domains)
    assert len(kept) == 1
    
    properties = kept[0]["function"]["parameters"]["properties"]
    
    # "sony tv" is excluded because its domain is "media_player"
    assert set(properties["name"]["enum"]) == {"Livingroom Lamps", "small_bedroom_light"}
    
    # Both rooms have light, so both remain
    assert set(properties["area"]["enum"]) == {"Living Room", "Small Bedroom"}
    
    # "domain" array broadened to anyOf and narrowed to "light"
    domain_prop = properties["domain"]
    assert "anyOf" in domain_prop
    assert domain_prop["anyOf"][0]["enum"] == ["light"]
    assert domain_prop["anyOf"][1]["items"]["enum"] == ["light"]


