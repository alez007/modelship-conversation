"""Narrow the Assist tool list to the domains an utterance plausibly targets.

modelship-specific addition (not in upstream ``openai_conversation``). Small local models
(e.g. FunctionGemma-270M) degrade sharply as the tool count grows: selection accuracy
collapses from ~5/6 at <=5 tools to ~2/6 at the full ~21-tool Assist catalog, even when the
extra tools don't semantically overlap — it's an attention/capacity limit, not just
ambiguity. So before the tools reach the model we trim them to the ones relevant to *this*
utterance.

The relevance signal is layered, strongest first, and every layer is fail-soft:

1. **hassil + home_assistant_intents** — Home Assistant's own curated sentence templates.
   A *lenient* recognize pass (``allow_unmatched_entities=True``) recovers the intent (and
   for domain-specific intents, the domain) even when the strict pipeline pass failed on a
   fuzzy entity name — which is *why* the request reached the LLM at all. Matched intents
   name tools to keep directly; ``HassLightSet`` -> ``light`` etc. contribute domains.
2. **Exposed entity/area names mentioned literally** — reuses tool_enums' exposed-name ->
   domain map; if the utterance contains an exposed name we know its domain for certain.
3. **A small keyword -> domain backstop** for when 1 and 2 yield nothing.

Kept tools = an always-on core (``HassTurnOn``/``HassTurnOff``/``GetLiveContext``) + tools
whose intent matched or whose target domain was detected, capped at ``max_tools``. If *no*
signal is found, the full list is kept (today's behavior): narrowing can only match or beat
the un-narrowed baseline, never strip a valid tool on a total miss.

Off by default and opt-in per subentry (``CONF_NARROW_TOOLS``): narrowing helps a tiny local
model but would hurt a large model that handles the full catalog fine. Self-contained like
:mod:`.tool_enums` so upstream re-syncs stay easy — ``entity.py`` calls only
:func:`narrow_tools`.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant

from .const import LOGGER
from .tool_enums import _collect_exposed, _intent_domain_map

# Tools kept regardless of detected domain: the generic on/off verbs and the live-state
# query tool. Status questions route to GetLiveContext, so keeping it core is the whole
# "query rule" — hassil tags "is the light on?" as a query intent, but even if it whiffs,
# GetLiveContext stays available.
_CORE_TOOLS = frozenset({"HassTurnOn", "HassTurnOff", "GetLiveContext"})

# Backstop ONLY — primary signal is hassil (layer 1) + exposed names (layer 2). Maps an
# entity domain to trigger words that imply it. Deliberately small; HA's templates do the
# real work when available.
_KEYWORD_DOMAINS: dict[str, frozenset[str]] = {
    "light": frozenset({"light", "lights", "lamp", "lamps", "dimmer", "brightness", "bulb"}),
    "media_player": frozenset(
        {"tv", "television", "music", "song", "track", "volume", "play", "playing",
         "pause", "resume", "movie", "speaker", "media", "radio", "podcast"}
    ),
    "cover": frozenset(
        {"cover", "covers", "blind", "blinds", "curtain", "curtains", "shade", "shades",
         "garage", "shutter", "shutters"}
    ),
    "climate": frozenset(
        {"temperature", "thermostat", "heating", "cooling", "heat", "warmer", "cooler",
         "degrees", "ac"}
    ),
    "fan": frozenset({"fan", "fans"}),
    "lock": frozenset({"lock", "unlock", "locked", "unlocked"}),
    "vacuum": frozenset({"vacuum", "hoover", "roomba"}),
    "weather": frozenset({"weather", "forecast", "rain", "raining", "sunny"}),
}

# Built once per language (the intent templates are language-global, not hass-specific).
# ``None`` is cached too so a failed/unavailable load isn't retried every request.
_INTENTS_CACHE: dict[str, Any] = {}


def narrow_tools(
    hass: HomeAssistant,
    tools: list[dict[str, Any]],
    user_text: str,
    max_tools: int = 6,
) -> None:
    """Trim ``tools`` in place to those relevant to ``user_text``.

    ``tools`` must contain only the Assist LLM API tools (call after
    :func:`.tool_enums.inject_assist_enums`, before web_search / code_interpreter / image
    tools are appended). Fail-soft throughout: any failure leaves the full list untouched.
    """
    if not user_text or not tools:
        return
    try:
        name_domains, area_domains = _collect_exposed(hass)
        domain_map = _intent_domain_map(hass)
        matched_intents, domains = _detect(
            hass, user_text, name_domains, area_domains, domain_map
        )
    except Exception:  # narrowing must never break a request
        LOGGER.debug("narrow_tools: detection failed, keeping full list", exc_info=True)
        return

    if not matched_intents and not domains:
        LOGGER.debug(
            "narrow_tools: no signal for %r, keeping full list (%d tools)",
            user_text,
            len(tools),
        )
        return

    kept = _select(tools, matched_intents, domains, domain_map, max_tools)
    if kept is None or len(kept) >= len(tools):
        return
    LOGGER.debug(
        "narrow_tools: %r -> intents=%s domains=%s; %d -> %d tools %s",
        user_text,
        sorted(matched_intents),
        sorted(domains),
        len(tools),
        len(kept),
        [t.get("name") for t in kept],
    )
    tools[:] = kept


def _detect(
    hass: HomeAssistant,
    text: str,
    name_domains: dict[str, set[str]],
    area_domains: dict[str, set[str]],
    domain_map: dict[str, set[str]],
) -> tuple[set[str], set[str]]:
    """Return (matched intent names, detected domains) for ``text``, layered and fail-soft."""
    matched_intents, slot_domains = _recognize_hassil(
        hass, text, list(name_domains), list(area_domains)
    )
    domains: set[str] = set(slot_domains)
    for name in matched_intents:
        domains |= domain_map.get(name, set())  # HassLightSet -> {"light"}, etc.
    domains |= _text_domains(text, name_domains, area_domains)
    if not domains:
        domains |= _keyword_domains(text)
    return matched_intents, domains


def _recognize_hassil(
    hass: HomeAssistant,
    text: str,
    name_strings: list[str],
    area_strings: list[str],
) -> tuple[set[str], set[str]]:
    """Lenient hassil pass over HA's curated templates. Fail-soft -> ``(set(), set())``.

    ``allow_unmatched_entities`` makes this *more* permissive than the pipeline's strict
    pass (which already failed, or the LLM wouldn't be involved): "turn on the tv lights"
    matches the ``HassTurnOn`` template even when "tv lights" isn't a resolvable slot.
    """
    try:
        from hassil.intents import TextSlotList
        from hassil.recognize import recognize_all
    except Exception:
        return set(), set()
    try:
        lang = hass.config.language or "en"
        intents = _cached_intents(lang) or _cached_intents("en")
        if intents is None:
            return set(), set()
        slot_lists: dict[str, Any] = {}
        if name_strings:
            slot_lists["name"] = TextSlotList.from_strings(name_strings)
        if area_strings:
            slot_lists["area"] = TextSlotList.from_strings(area_strings)

        matched: set[str] = set()
        domains: set[str] = set()
        for result in recognize_all(
            text, intents, slot_lists=slot_lists, allow_unmatched_entities=True
        ):
            name = getattr(getattr(result, "intent", None), "name", None)
            if isinstance(name, str):
                matched.add(name)
            entities = getattr(result, "entities", None) or {}
            dom = entities.get("domain")
            value = getattr(dom, "value", None)
            if isinstance(value, str):
                domains.add(value)
        return matched, domains
    except Exception:
        LOGGER.debug("narrow_tools: hassil recognize failed", exc_info=True)
        return set(), set()


def _cached_intents(lang: str) -> Any:
    """Load and cache the hassil ``Intents`` for a language (caches failures as ``None``)."""
    if lang in _INTENTS_CACHE:
        return _INTENTS_CACHE[lang]
    intents: Any = None
    try:
        from hassil.intents import Intents
        from home_assistant_intents import get_intents

        data = get_intents(lang)
        if data:
            intents = Intents.from_dict(data)
    except Exception:
        LOGGER.debug("narrow_tools: could not load intents for %s", lang, exc_info=True)
    _INTENTS_CACHE[lang] = intents
    return intents


def _text_domains(
    text: str,
    name_domains: dict[str, set[str]],
    area_domains: dict[str, set[str]],
) -> set[str]:
    """Domains of any exposed entity/area name that appears literally in ``text``."""
    low = text.lower()
    out: set[str] = set()
    for mapping in (name_domains, area_domains):
        for nm, doms in mapping.items():
            if nm and len(nm) >= 3 and nm.lower() in low:
                out |= doms
    return out


def _keyword_domains(text: str) -> set[str]:
    """Backstop: map utterance tokens to domains via the small keyword table."""
    tokens = set(re.findall(r"[a-z]+", text.lower()))
    return {domain for domain, words in _KEYWORD_DOMAINS.items() if tokens & words}


def _select(
    tools: list[dict[str, Any]],
    matched_intents: set[str],
    domains: set[str],
    domain_map: dict[str, set[str]],
    max_tools: int,
) -> list[dict[str, Any]] | None:
    """Pure tool-filtering — unit-testable without Home Assistant.

    Returns the kept tools, or ``None`` to signal "keep everything" (nothing matched).
    Non-function tools are always kept. Core is kept first and never evicted; on overflow
    the remaining budget goes to matched intents, then domain hits.
    """
    passthrough: list[dict[str, Any]] = []  # non-function tools (web_search, ...) — never dropped
    core: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []
    domain_hit: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            passthrough.append(tool)
            continue
        name = tool.get("name")
        if name in _CORE_TOOLS:
            core.append(tool)
        elif name in matched_intents:
            matched.append(tool)
        elif domain_map.get(name, set()) & domains:
            domain_hit.append(tool)
    # No concrete tool matched (only the generic core would survive) -> too weak a signal
    # to narrow on; keep everything (fail-open) rather than strip to core.
    if not matched and not domain_hit:
        return None
    fns = core + matched + domain_hit
    if len(fns) > max_tools:
        # Core stays FIRST and is never evicted: it's a static list, so serializing it
        # ahead of the per-utterance tools keeps llama.cpp's prefix cache warm through
        # the developer prompt + core (a variable tool first would bust the cache right
        # after the prompt). Dropping core also stranded "turn on X" with no HassTurnOn.
        # Remaining budget: matched intents first, then domain hits.
        fns = core + (matched + domain_hit)[: max(0, max_tools - len(core))]
    return passthrough + fns
