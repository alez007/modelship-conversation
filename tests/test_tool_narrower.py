"""Unit tests for the per-utterance tool narrower (pure logic, no HA cluster needed).

Run in a Home Assistant test env (the module imports ``homeassistant`` at load). The
functions under test are pure — they take plain dicts/sets, no fixtures required.
"""

from custom_components.modelship_conversation.tool_narrower import (
    _keyword_domains,
    _select,
    _text_domains,
)


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
    return [t["name"] for t in kept] if kept is not None else None


def test_media_keeps_media_and_core_drops_noise():
    kept = _select(_TOOLS, {"HassMediaPause"}, {"media_player"}, _DMAP, 99)
    assert set(_names(kept)) == {
        "HassTurnOn", "HassTurnOff", "GetLiveContext",
        "HassMediaPause", "HassMediaNext", "HassMediaUnpause", "HassSetVolume",
    }


def test_domain_only_signal_keeps_core_plus_domain_tool():
    kept = _select(_TOOLS, set(), {"light"}, _DMAP, 6)
    assert set(_names(kept)) == {"HassTurnOn", "HassTurnOff", "GetLiveContext", "HassLightSet"}


def test_no_concrete_match_returns_none_keep_all():
    # only core would survive -> too weak to narrow on -> signal "keep everything"
    assert _select(_TOOLS, set(), set(), _DMAP, 6) is None


def test_cap_prioritises_matched_then_domain():
    tools = [_f("HassTurnOn"), _f("HassTurnOff"), _f("GetLiveContext")] + [
        _f(f"X{i}") for i in range(5)
    ]
    dmap = {f"X{i}": {"media_player"} for i in range(5)}
    kept = _select(tools, {"X0"}, {"media_player"}, dmap, 4)
    assert len(kept) == 4
    assert "X0" in _names(kept)  # matched intent survives the cap


def test_non_function_tool_always_preserved_and_exempt_from_cap():
    extra = {"type": "web_search"}
    kept = _select([*_TOOLS, extra], {"HassMediaPause"}, {"media_player"}, _DMAP, 6)
    assert extra in kept


def test_text_domains_from_literal_exposed_name():
    name_domains = {"small_bedroom_light": {"light"}, "sony tv": {"media_player"}}
    area_domains = {"Living Room": {"light", "media_player"}}
    assert _text_domains("turn off the small_bedroom_light", name_domains, area_domains) == {"light"}
    assert _text_domains("whats on in the Living Room", name_domains, area_domains) == {
        "light", "media_player",
    }
    # short names (<3 chars) ignored to avoid spurious substring hits
    assert _text_domains("watch tv now", {"tv": {"media_player"}}, {}) == set()


def test_keyword_backstop():
    assert _keyword_domains("dim the lamp") == {"light"}
    assert _keyword_domains("skip the track") == {"media_player"}
    assert _keyword_domains("whats the temperature outside") == {"climate"}
    assert _keyword_domains("hello there how are you") == set()
