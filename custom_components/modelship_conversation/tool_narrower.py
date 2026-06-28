"""Narrow the Assist tool list to what an utterance actually asks for.

modelship-specific addition (not in upstream ``openai_conversation``). Small local models
(e.g. FunctionGemma-270M) degrade sharply as the tool count grows: selection accuracy
collapses from ~5/6 at <=5 tools to ~2/6 at the full ~21-tool Assist catalog, even when the
extra tools don't semantically overlap — it's an attention/capacity limit, not just
ambiguity. So before the tools reach the model we trim them to the ones relevant to *this*
utterance.

**hassil is the only relevance signal.** ``_recognize_hassil`` runs Home Assistant's own
curated sentence templates (``home_assistant_intents``) over the utterance with a *lenient*
``recognize_all(allow_unmatched_entities=True)`` pass — more permissive than the strict
pipeline pass that already failed (or the LLM wouldn't be involved). It returns the matched
intents (and any domain slots they filled); a matched ``HassLightSet`` contributes
``light``, etc. We deliberately do **not** add invented backstops (literal-substring name
matching, keyword tables): hassil already parses the exact same exposed names as real
grammar slots, so anything a substring loop could catch hassil catches better — and the only
case it *wouldn't* (a bare name with no recognizable intent) is precisely the low-confidence
case we'd rather not act on.

Kept tools depend on the signal:

* **hassil matched something** -> an always-on core (``HassTurnOn``/``HassTurnOff``/
  ``GetLiveContext``) + tools whose intent matched or whose target domain was detected,
  capped at ``max_tools``.
* **hassil matched nothing** -> strip *all* action tools. A blank match means the utterance
  isn't a recognized device command (a greeting, chit-chat, a general question), and handing
  a tiny model the full catalog on "hi" just invites a hallucinated call. We fail **closed**,
  and loud: if hassil itself is broken (import/version drift), *every* request lands here and
  the breakage is immediately obvious — far louder, and easier to catch, than a model that
  silently picks wrong some of the time.

Off by default and opt-in per subentry (``CONF_NARROW_TOOLS``): narrowing helps a tiny local
model but would hurt a large model that handles the full catalog fine. Self-contained like
:mod:`.tool_enums` so upstream re-syncs stay easy — ``entity.py`` calls only
:func:`narrow_tools`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import LOGGER
from .tool_enums import _collect_exposed, _intent_domain_map

# Tools kept whenever there is *any* hassil signal: the generic on/off verbs and the
# live-state query tool. Status questions route to GetLiveContext, so keeping it core is the
# whole "query rule". (On a *blank* match these are dropped too — see module docstring.)
_CORE_TOOLS = frozenset({"HassTurnOn", "HassTurnOff", "GetLiveContext"})

# Built once per language (the intent templates are language-global, not hass-specific).
# ``None`` is cached too so a failed/unavailable load isn't retried every request.
_INTENTS_CACHE: dict[str, Any] = {}

# hassil is now load-bearing: if it can't run, every request narrows to zero tools. That
# must not be silent, but it also must not spam a warning per request — log the real cause
# exactly once per process.
_HASSIL_FAILED_LOGGED = False


def _hassil_unavailable(reason: str, exc: BaseException | None = None) -> tuple[set[str], set[str]]:
    """Log (once) why hassil couldn't run and return the empty signal."""
    global _HASSIL_FAILED_LOGGED
    if not _HASSIL_FAILED_LOGGED:
        _HASSIL_FAILED_LOGGED = True
        LOGGER.warning(
            "narrow_tools: hassil unavailable (%s) -> every request narrows to ZERO tools "
            "until fixed",
            reason,
            exc_info=exc,
        )
    return set(), set()


async def narrow_tools(
    hass: HomeAssistant,
    tools: list[dict[str, Any]],
    user_text: str,
    max_tools: int = 6,
) -> None:
    """Trim ``tools`` in place to what ``user_text`` asks for.

    ``tools`` must contain only the Assist LLM API tools (call after
    :func:`.tool_enums.inject_assist_enums`, before web_search / code_interpreter / image
    tools are appended). A blank hassil match strips all action tools (fail closed); only an
    error building the exposed maps / intent registry leaves the list untouched.
    """
    if not user_text or not tools:
        return
    try:
        name_domains, area_domains = _collect_exposed(hass)
        domain_map = _intent_domain_map(hass)
        # Prune the intent->domain map to domains this house actually owns: an intent whose
        # target domain isn't installed (HassVacuumStart with no vacuum) can never be a real
        # hit, so it must never reach the ``domain_hit`` bucket. Pure inventory fact.
        exposed_domains = set().union(*name_domains.values(), *area_domains.values())
        domain_map = {i: d for i, d in domain_map.items() if d & exposed_domains}
        matched_intents, domains = await _detect(hass, user_text, name_domains, area_domains, domain_map)
    except Exception:  # only a registry/exposed-map error fails open; hassil whiffs do not
        LOGGER.debug("narrow_tools: detection failed, keeping full list", exc_info=True)
        return

    kept = _select(tools, matched_intents, domains, domain_map, max_tools, name_domains, area_domains)
    if len(kept) >= len(tools):
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


async def _detect(
    hass: HomeAssistant,
    text: str,
    name_domains: dict[str, set[str]],
    area_domains: dict[str, set[str]],
    domain_map: dict[str, set[str]],
) -> tuple[set[str], set[str]]:
    """Return (matched intent names, detected domains) for ``text`` — hassil only."""
    matched_intents, slot_domains = await _recognize_hassil(
        hass, text, list(name_domains), list(area_domains)
    )
    domains: set[str] = set(slot_domains)
    for name in matched_intents:
        domains |= domain_map.get(name, set())  # HassLightSet -> {"light"}, etc.
    return matched_intents, domains


async def _recognize_hassil(
    hass: HomeAssistant,
    text: str,
    name_strings: list[str],
    area_strings: list[str],
) -> tuple[set[str], set[str]]:
    """Lenient hassil pass over HA's curated templates. Fail-soft -> ``(set(), set())``.

    ``allow_unmatched_entities`` makes this *more* permissive than the pipeline's strict
    pass (which already failed, or the LLM wouldn't be involved): "turn on the tv lights"
    matches the ``HassTurnOn`` template even when "tv lights" isn't a resolvable slot.

    A ``(set(), set())`` return (hassil unavailable, or genuinely no match) is treated by the
    caller as "no device command" and fails closed — see the module docstring.
    """
    try:
        from hassil.errors import MissingListError
        from hassil.intents import TextSlotList
        from hassil.recognize import recognize_all
    except Exception as err:
        return _hassil_unavailable("import failed", err)
    try:
        lang = hass.config.language or "en"
        intents = await _cached_intents(hass, lang) or await _cached_intents(hass, "en")
        if intents is None:
            return _hassil_unavailable("could not load intents")
        slot_lists: dict[str, Any] = {}
        if name_strings:
            slot_lists["name"] = TextSlotList.from_strings(name_strings)
        if area_strings:
            slot_lists["area"] = TextSlotList.from_strings(area_strings)

        # Home Assistant updates occasionally add new slot lists (like {floor}) to intents.
        # If one is missing from slot_lists, recognize_all raises MissingListError and aborts.
        # We catch it, populate an empty list to satisfy the parser, and retry so we don't fail.
        def _run_recognize() -> list[Any]:
            while True:
                try:
                    return list(
                        recognize_all(
                            text, intents, slot_lists=slot_lists, allow_unmatched_entities=True
                        )
                    )
                except MissingListError as e:
                    # e.g. "Missing slot list {floor}" -> "floor"
                    missing = str(e).split("{")[-1].split("}")[0]
                    slot_lists[missing] = TextSlotList.from_strings([])

        # recognize_all (via unicode_rbnf) does blocking disk reads for number parsing.
        results = await hass.async_add_executor_job(_run_recognize)

        matched: set[str] = set()
        domains: set[str] = set()
        for result in results:
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


async def _cached_intents(hass: HomeAssistant, lang: str) -> Any:
    """Load and cache the hassil ``Intents`` for a language (caches failures as ``None``)."""
    if lang in _INTENTS_CACHE:
        return _INTENTS_CACHE[lang]

    def _load() -> Any:
        try:
            from hassil.intents import Intents
            from home_assistant_intents import get_intents

            data = get_intents(lang)
            if data:
                return Intents.from_dict(data)
        except Exception:
            LOGGER.debug("narrow_tools: could not load intents for %s", lang, exc_info=True)
        return None

    intents = await hass.async_add_executor_job(_load)
    _INTENTS_CACHE[lang] = intents
    return intents


def _select(
    tools: list[dict[str, Any]],
    matched_intents: set[str],
    domains: set[str],
    domain_map: dict[str, set[str]],
    max_tools: int,
    name_domains: dict[str, set[str]],
    area_domains: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Pure tool-filtering — unit-testable without Home Assistant.

    Returns the kept tools. Non-function tools (web_search, ...) are always preserved. With a
    hassil signal: core (kept first, never evicted) + matched intents + domain hits, capped.
    With *no* signal (``matched_intents`` and ``domains`` both empty): only the non-function
    passthrough survives — all action tools are stripped (fail closed; see module docstring).
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
    # No hassil signal -> not a recognized device command -> strip every action tool.
    if not matched_intents and not domains:
        return passthrough
    fns = core + matched + domain_hit
    if len(fns) > max_tools:
        # Core stays FIRST and is never evicted: it's a static list, so serializing it ahead
        # of the per-utterance tools keeps llama.cpp's prefix cache warm through the developer
        # prompt + core (a variable tool first would bust the cache right after the prompt).
        # Remaining budget: matched intents first, then domain hits.
        fns = core + (matched + domain_hit)[: max(0, max_tools - len(core))]

    # 1. Broaden arrays: small fine-tunes (FunctionGemma-270m) often output single strings
    # for list parameters (like `domain:<escape>light<escape>`). HA's schemas use `type: array`,
    # which causes the grammar compiler to strictly enforce `[` brackets and blocks the model,
    # making it fall back to plain text hallucination. We rewrite string arrays to `anyOf`
    # [string, array] for ALL kept tools so the grammar allows both formats.
    for tool in fns:
        params = tool.get("parameters")
        if not isinstance(params, dict):
            continue
        props = params.get("properties")
        if not isinstance(props, dict):
            continue
        for k, v in props.items():
            if isinstance(v, dict) and v.get("type") == "array":
                items = v.get("items")
                if isinstance(items, dict) and items.get("type") == "string":
                    # Rewrite to anyOf
                    props[k] = {
                        "anyOf": [
                            items,  # The string variant (with or without enum)
                            v,      # The original array variant
                        ]
                    }

    # 2. Narrow generic enums: if hassil detected specific domains, narrow the enum lists
    # within generic tools so HassTurnOn doesn't offer "sony tv" when asked to turn on lights.
    if domains:
        for tool in fns:
            if domain_map.get(tool.get("name")) is not None:
                continue

            params = tool.get("parameters")
            if not isinstance(params, dict):
                continue
            props = params.get("properties")
            if not isinstance(props, dict):
                continue

            # Narrow 'name' enum
            if isinstance(props.get("name"), dict) and "enum" in props["name"]:
                new_names = [n for n in props["name"]["enum"] if name_domains.get(n, set()) & domains]
                if new_names:
                    props["name"] = {**props["name"], "enum": new_names}
                else:
                    props["name"] = {k: v for k, v in props["name"].items() if k != "enum"}

            # Narrow 'area' enum
            if isinstance(props.get("area"), dict) and "enum" in props["area"]:
                new_areas = [a for a in props["area"]["enum"] if area_domains.get(a, set()) & domains]
                if new_areas:
                    props["area"] = {**props["area"], "enum": new_areas}
                else:
                    props["area"] = {k: v for k, v in props["area"].items() if k != "enum"}

            # Narrow 'domain' enum (which might now be inside `anyOf` due to step 1)
            dom = props.get("domain")
            if isinstance(dom, dict) and "anyOf" in dom:
                # the anyOf array has [string_variant, array_variant]
                arr_variant = dom["anyOf"][1]
                if isinstance(arr_variant.get("items"), dict) and "enum" in arr_variant["items"]:
                    new_domains = [d for d in arr_variant["items"]["enum"] if d in domains]
                    if new_domains:
                        dom["anyOf"][0] = {**dom["anyOf"][0], "enum": new_domains}
                        arr_variant["items"] = {**arr_variant["items"], "enum": new_domains}
                    else:
                        dom["anyOf"][0] = {k: v for k, v in dom["anyOf"][0].items() if k != "enum"}
                        arr_variant["items"] = {k: v for k, v in arr_variant["items"].items() if k != "enum"}

    return passthrough + fns
