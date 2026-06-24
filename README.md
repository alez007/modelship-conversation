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

The merge base is pinned in [`NOTICE`](NOTICE). To update:

1. Copy `homeassistant/components/openai_conversation/` from the newer Core tag.
2. Re-apply the delta set:
   - `manifest.json` — domain/name/codeowners/urls, `iot_class: local_polling`,
     add `version`, drop `quality_scale`.
   - `const.py` — rename `DOMAIN`, add `CONF_BASE_URL`.
   - `__init__.py` — build `AsyncOpenAI(base_url=…, api_key=… or "sk-noauth")`;
     drop the deprecated `generate_*` services and the legacy migrations.
   - `config_flow.py` — add required `CONF_BASE_URL`, make the API key optional,
     validate against `{base_url}/models`, retitle the entry.
   - `strings.json` / `translations/en.json` — repoint
     `component::openai_conversation::` key references to
     `component::modelship_conversation::`, add the base-URL field, rebrand.
   - delete `services.yaml`, `quality_scale.yaml`; trim `icons.json`.
3. Bump the merge-base tag in `NOTICE`.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
