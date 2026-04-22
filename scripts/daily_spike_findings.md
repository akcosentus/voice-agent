# Daily.co WebRTC transport — spike findings

**Date**: 2026-04-22
**Branch**: `spike/daily-transport` (deleted after this was written)
**Goal**: prove Daily + Pipecat works end-to-end before committing to the
AWS-reference-repo fork migration.

## TL;DR

**Yes, it works. Subjectively it felt faster than our current Twilio path.**
No blockers found. Two minor things for the migration backlog, listed at
the bottom. Migration to Daily is a go from a technical proof-of-concept
standpoint.

## What was tested

Single-file spike script at `scripts/daily_spike.py` on the
`spike/daily-transport` branch:

- Pipecat `DailyTransport` (not our production `FastAPIWebsocketTransport`)
- SileroVAD with `stop_secs=0.6`
- Deepgram STT (cloud API, same as prod)
- Anthropic Claude Sonnet 4.6 direct (no Bedrock), generic 276-char prompt
- ElevenLabs TTS (`ZoiZ8fuDWInAcwPXaVeq` voice, `eleven_turbo_v2_5` model)

Room created via Daily REST API using Pipecat's `DailyRESTHelper`. Bot
joined with an owner meeting token; human joined from a browser tab via
Daily's hosted web UI (`cosentus.daily.co/*`).

Session: 1 connect, 4 conversational turns, 1 disconnect (browser
closed by human). ~39 seconds of actual conversation.

## Did it work

Yes. End-to-end, first try, no retries. Summary:

| Signal | Result |
|---|---|
| Room create via REST | 0.8s |
| Pipeline startup (bot joins room) | <1s after room created |
| `on_client_connected` fires on browser join | ✅ |
| `on_client_disconnected` fires on browser leave | ✅ |
| Room delete on teardown | ✅ |
| Errors during session | 0 |

## Audio quality vs Twilio production

> *Alex, first-person*: Daily felt noticeably faster than Twilio in the
> test call. Response snappier, no audio artifacts. Could be partly
> because no Chris-sized system prompt, but the transport itself felt
> good.

Objective numbers back that up — see the TTFB table below. The
subjective snappiness is probably a combination of (a) no full 35K-char
Chris prompt adding LLM first-turn cost, (b) WebRTC's native media
path vs Twilio's 8kHz μ-law resample leg, and (c) possibly fewer
intermediate relays. Worth keeping both (a) and (b) in mind when we
benchmark the migration against real Chris traffic.

## Latency numbers (Pipecat metrics TTFB)

Rough totals end-to-end (user-speech-ends → bot-audio-starts): **~1.5s**
across 4 turns. Prior Twilio production runs measured ~1.35s average.
So: essentially equivalent, within run-to-run variance.

| Turn | Deepgram STT | Anthropic LLM | ElevenLabs TTS |
|---|---|---|---|
| 1 | 591 ms | 1,628 ms | 183 ms |
| 2 | 265 ms | 981 ms | 182 ms |
| 3 | 215 ms | 892 ms | 167 ms |
| 4 | 571 ms | 870 ms | 304 ms |
| **avg** | **411 ms** | **1,090 ms** | **210 ms** |

For comparison, Task 3 Twilio verification measured on a real Chris
call:

| Service | Twilio avg | Daily avg | Δ |
|---|---|---|---|
| Deepgram STT TTFB | ~300 ms | 411 ms | +100 ms |
| Anthropic LLM TTFB | ~1,170 ms | 1,090 ms | −80 ms |
| ElevenLabs TTS TTFB | ~150 ms | 210 ms | +60 ms |

Nothing stands out as a regression. The STT and TTS deltas are small
enough to be random routing / region variance rather than an
architectural cost of Daily.

## What Chris-voice actually said during the test

Captured from Pipecat's `Generating TTS` debug logs in order:

```
"Hey, welcome!"
"Great to have you here — how can I help you today?"
"I'm doing great, thanks for asking!"
"How about you, how are you doing?"
"That's awesome to hear!"
"So what can I help you with today?"
"My job is just to chat with you and help you out with whatever you need,
 whether that's answering questions, talking through ideas, or just
 having a conversation!"
```

Multi-frame TTS streaming works; no chunks dropped or silences.
Interruption was allowed (`PipelineParams(allow_interruptions=True)`)
and behaved correctly during the test.

## Pipecat-specific observations

1. **DailyTransport event names match our Twilio handler's semantics.**
   It fires `on_client_connected` / `on_client_disconnected` — identical
   to `FastAPIWebsocketTransport`. This means the existing
   `core/call_lifecycle.py::run_call()` function *could* be reused
   wholesale for the Daily path with no refactor. Unlike our LiveKit
   POC (which used `on_first_participant_joined` /
   `on_participant_left`), Daily is a closer drop-in.

2. **`DailyRESTHelper` is a clean fit for room+token management.**
   Two calls: `create_room(DailyRoomParams())` and
   `get_token(room_url, expiry_time=3600, owner=True)`. No need to
   hand-roll HTTP calls against the Daily REST API.

3. **Room cleanup via `delete_room_by_name()`** works reliably. Rooms
   also self-expire after the configured TTL (default 1h) so teardown
   isn't load-bearing for safety.

4. **Pipecat 0.0.106 needs the `daily` extra explicitly.**
   `pipecat-ai[daily]` pulls in `daily-python==0.25.0` (11.3 MiB). Our
   main repo `pyproject.toml` does NOT include this extra. The
   migration will add it.

## For the migration backlog (explicitly deferred)

Neither of these is a Daily-specific bug — they're things main already
does correctly and the spike script deliberately skipped for simplicity.
Listing them so nobody forgets during migration:

### 1. Prompt caching flag for AnthropicLLMService

The spike constructed the LLM as:

```python
llm = AnthropicLLMService(
    api_key=..., model="claude-sonnet-4-6",
)
```

Cache DEBUG across all 4 turns showed
`Cache creation input tokens: 0, Cache read input tokens: 0` — meaning
Pipecat did NOT send `cache_control` markers and Anthropic did not cache
anything.

Our main `core/pipeline.py::_create_llm` passes
`enable_prompt_caching=True` (hardcoded; confirmed firing in the Task 3
post-commit verification where turn 2+ showed ~10K cache-read tokens).
The migration code must carry that flag over.

### 2. Pipecat loguru filter for system-prompt masking

The spike's DEBUG log contains a line like:

```
pipecat.services.anthropic.llm:_process_context:512 -
AnthropicLLMService#0: Generating chat from universal context
[You are a friendly assistant...] | [{'role': 'user', 'content': 'Hello?'}]
```

This is the exact prompt-dump leak Task 3 masked in main via
`core/logging.py` (the `_phi_filter` loguru filter that matches the
`": Generating chat from "` fingerprint). The spike script is
standalone and bypasses that filter.

In the migrated codebase, the loguru filter MUST be installed before
any Pipecat module emits its first log — i.e. imported at the top of
whatever process entry point the Daily-based service runs under. Task
3's `server/main.py:23` pattern (`import core.logging  # noqa`) is the
right model.

## Things I did NOT test in this spike (deliberate scope limit)

- Multiple simultaneous rooms / bot instances
- Phone dial-in via Daily's PSTN (`dialin_settings` on DailyParams)
- SIP outbound (Daily has it; we'd need that for outbound parity with
  Twilio)
- Recording (Daily has cloud recording; needs a config pass)
- Reconnect behavior after transient network drops
- Fargate / container environment — all of this ran on my Mac

Any of those are follow-on spikes if we hit unknowns during migration.

## Verdict

Migration to Daily is safe from a transport-compatibility standpoint.
Latency is comparable. The event-handler surface is closer to our
current Twilio path than LiveKit was. We should proceed with the
AWS-reference-repo fork migration based on these findings.
