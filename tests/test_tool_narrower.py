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
    kept = _select(_TOOLS, {"HassMediaPause"}, {"media_player"}, _DMAP, 99)
    assert set(_names(kept)) == {
        "HassTurnOn", "HassTurnOff", "GetLiveContext",
        "HassMediaPause", "HassMediaNext", "HassMediaUnpause", "HassSetVolume",
    }


def test_domain_only_signal_keeps_core_plus_domain_tool():
    kept = _select(_TOOLS, set(), {"light"}, _DMAP, 6)
    assert set(_names(kept)) == {"HassTurnOn", "HassTurnOff", "GetLiveContext", "HassLightSet"}


def test_generic_match_narrows_to_core():
    # "turn on my sony tv": hassil matches the generic HassTurnOn (no domain slot). That IS a
    # signal -> narrow to core only. No media tools are offered, so the model cannot mis-pick
    # HassMediaUnpause for a power command.
    kept = _select(_TOOLS, {"HassTurnOn"}, set(), _DMAP, 6)
    assert _names(kept) == ["HassTurnOn", "HassTurnOff", "GetLiveContext"]


def test_no_signal_strips_all_action_tools():
    # hassil matched nothing (greeting / chit-chat / broken hassil) -> fail closed: every
    # action tool is stripped so a tiny model can't hallucinate a call on "hi".
    assert _select(_TOOLS, set(), set(), _DMAP, 6) == []


def test_cap_keeps_core_first_then_matched():
    tools = [_f("HassTurnOn"), _f("HassTurnOff"), _f("GetLiveContext")] + [
        _f(f"X{i}") for i in range(5)
    ]
    dmap = {f"X{i}": {"media_player"} for i in range(5)}
    kept = _select(tools, {"X0"}, {"media_player"}, dmap, 4)
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
    kept = _names(_select(tools, matched, set(), {}, 6))
    assert {"HassTurnOn", "HassTurnOff", "GetLiveContext"}.issubset(kept)
    assert kept[:3] == ["HassTurnOn", "HassTurnOff", "GetLiveContext"]


def test_non_function_tool_always_preserved_and_exempt_from_cap():
    extra = {"type": "web_search"}
    kept = _select([*_TOOLS, extra], {"HassMediaPause"}, {"media_player"}, _DMAP, 6)
    assert extra in kept


def test_non_function_tool_survives_no_signal_strip():
    # even when all action tools are stripped, passthrough (web_search, ...) is preserved.
    extra = {"type": "web_search"}
    assert _select([*_TOOLS, extra], set(), set(), _DMAP, 6) == [extra]
