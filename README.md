# Modelship Conversation

A Home Assistant conversation/voice integration that points the
[OpenAI Conversation](https://www.home-assistant.io/integrations/openai_conversation)
experience at a local [Modelship](https://github.com/alez007/modelship) server
instead of api.openai.com.

It is a thin fork of Home Assistant Core's `openai_conversation` integration
(see [`NOTICE`](NOTICE)). Modelship exposes an OpenAI-compatible API — including
the `/v1/responses` adapter that this integration drives — so the upstream
conversation entity, the Assist LLM tool loop (native HA control), streaming,
STT and TTS all work unchanged against your own hardware. The fork adds exactly
two things the official integration lacks:

- a configurable **base URL**, so it can reach your Modelship instance, and
- an **optional API key** (Modelship needs none; a dummy `sk-noauth` is sent if
  you leave it blank).

## What it provides

A single config entry (the connection to Modelship) with four subentries:

| Subentry      | Talks to Modelship endpoint     | Notes |
|---------------|---------------------------------|-------|
| Conversation  | `/v1/responses`                 | Native HA control via the Assist LLM API + editable prompt template |
| AI Task       | `/v1/responses`                 | `generate_data` / structured output |
| Speech-to-text| `/v1/audio/transcriptions`      | e.g. whispercpp |
| Text-to-speech| `/v1/audio/speech`              | e.g. kokoroonnx / orpheus |

With all four configured you can assign the whole Assist pipeline (STT →
conversation → TTS) to Modelship over plain HTTP, with no Wyoming bridge.

## Installation (HACS, custom repository)

1. HACS → ⋮ → **Custom repositories**.
2. URL: `https://github.com/alez007/modelship-conversation`, category
   **Integration**.
3. Install **Modelship Conversation**, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Modelship Conversation.**

## Configuration

When adding the integration:

- **Base URL** — your Modelship OpenAI endpoint, e.g.
  `http://homeassistant:8000/v1`.
- **API key** — leave blank for Modelship (a dummy key is used).

The connection is validated against `GET {base_url}/models`.

Then configure each subentry. **Model names are routing keys** that Modelship
matches against the deployment `name` in `models.yaml`. To keep the upstream
model gating dormant, name your Modelship deployments with OpenAI-style names:

| Subentry      | Suggested model name  | Why |
|---------------|-----------------------|-----|
| Conversation  | `gpt-4o-mini`         | Doesn't match the `o*`/`gpt-5*` reasoning prefixes → plain temperature/top_p path |
| STT           | `whisper-1`           | Fits the STT model dropdown (free-text anyway) |
| TTS           | `gpt-4o-mini-tts`     | Any name works |

### A note on TTS voices

The TTS subentry advertises OpenAI voice IDs (`alloy`, `echo`, …). Modelship's
Kokoro plugin uses its own voice IDs (`af_heart`, …) and is configured to fall
back to `af_heart` for any unrecognized voice, so TTS works out of the box; set
the voice to a real Kokoro ID once you want a specific voice.

## Re-syncing the fork against upstream

The merge base is pinned in [`NOTICE`](NOTICE). All fork-specific *behaviour*
lives in standalone modules that upstream never ships, so re-syncing only ever
touches the upstream files at a handful of one-line hook points:

| Module (ours only) | What it holds |
|--------------------|---------------|
| `modelship.py`     | Facade: small-model option constants/defaults, the runtime hooks (`apply_stateless`, `prepare_tools`, `force_first_tool`, `release_forced_tool`, `stop_after_action`) and the config-flow `add_options_schema` helper |
| `history.py`       | `drop_history` — stateless mode |
| `tool_enums.py`    | `inject_assist_enums` — constrain device-tool args to exposed values |
| `tool_narrower.py` | `narrow_tools` — trim the tool list per utterance |

`conversation.py`, `ai_task.py`, `stt.py`, `tts.py` are byte-identical to
upstream — copy them straight over, no edits.

To update:

1. Copy `homeassistant/components/openai_conversation/` from the newer Core tag
   (keeping our `modelship.py`, `history.py`, `tool_enums.py`, `tool_narrower.py`).
2. Re-apply the delta set (everything below is branding/connection plumbing
   except the two one-line `modelship.*` hooks):
   - `manifest.json` — domain/name/codeowners/urls, `iot_class: local_polling`,
     add `version`, drop `quality_scale`.
   - `const.py` — rename `DOMAIN`/default names, add the `CONF_BASE_URL`
     connection block. (Feature constants now live in `modelship.py`.)
   - `__init__.py` — build `AsyncOpenAI(base_url=…, api_key=… or "sk-noauth")`;
     drop the deprecated `generate_*` services and the legacy migrations.
   - `config_flow.py` — add required `CONF_BASE_URL`, make the API key optional,
     validate against `{base_url}/models`, retitle the entry; add
     `from . import modelship` and the `modelship.add_options_schema(step_schema)`
     call in the conversation options step.
   - `entity.py` — add `from . import modelship`, then the five hook one-liners
     in `_async_handle_chat_log`: `apply_stateless`, `prepare_tools` (after the
     tool list is built), `force_first_tool` (before the loop),
     `release_forced_tool` and `stop_after_action` (in the loop). The loop body
     also keeps `new_content` in a local so `stop_after_action` can read it.
   - `strings.json` / `translations/en.json` — repoint
     `component::openai_conversation::` key references to
     `component::modelship_conversation::`, add the base-URL field, the three
     small-model toggles (`narrow_tools`, `stop_after_action`, `stateless`),
     rebrand.
   - delete `services.yaml`, `quality_scale.yaml`; trim `icons.json`.
3. Bump the merge-base tag in `NOTICE`.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
