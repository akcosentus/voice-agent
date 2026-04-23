# LiveKit transport reference

Reference material preserved from the deleted `run.py` top-level entry point.
Original `run.py` was a CLI for running an agent locally against a LiveKit
room; it was superseded by the Twilio WebSocket + WebRTC test-call handlers
in production, but the LiveKit transport construction snippet is useful for
the upcoming LiveKit self-hosted migration.

## Pipecat LiveKit transport construction

From `run.py`, 2026-04-20, built against Pipecat 0.0.108:

```python
from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.livekit.transport import LiveKitTransport, LiveKitParams
from pipecat.runner.livekit import configure

from core.config_loader import load_agent_config
from core.pipeline import build_pipeline, build_pipeline_components


async def main(agent_name: str, cases_file: str, case_index: int):
    config = load_agent_config(agent_name)

    with open(cases_file) as f:
        cases = json.load(f)
    case_data = cases[case_index]

    # Pipeline is transport-agnostic; these two calls are the SAME as the
    # Twilio and WebRTC handlers in server/ws_handler.py and
    # server/webrtc_handler.py.
    components = build_pipeline_components(config, case_data, transport_type="livekit")

    # `configure()` returns (url, token, room_name) from CLI args or env.
    # In the ECS migration this gets replaced by a dispatch service that
    # creates per-call rooms and tokens, and a handler that receives them.
    (url, token, room_name) = await configure()

    transport = LiveKitTransport(
        url=url,
        token=token,
        room_name=room_name,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )

    bundle = build_pipeline(components, transport, config=config)

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant_id):
        print(f"Participant joined. {config.display_name} is listening...")
        if bundle.audiobuffer:
            await bundle.audiobuffer.start_recording()

    runner = PipelineRunner()
    await runner.run(bundle.task)
```

## Key notes for the migration

1. **The pipeline is already transport-agnostic.** `build_pipeline_components`
   and `build_pipeline` take transport as a parameter; no pipeline code needs
   to change to add a LiveKit handler alongside the existing Twilio WebSocket
   + SmallWebRTC handlers.

2. **`LiveKitParams` fields** available in Pipecat 0.0.108 include
   `audio_in_enabled`, `audio_out_enabled`, and the usual sample-rate
   controls. Check `pipecat.transports.livekit.transport.LiveKitParams` for
   the full list at migration time; avoid copying defaults from memory.

3. **`transport_type="livekit"`** is currently the value `webrtc_handler.py`
   passes to `build_pipeline_components`. It skips the Twilio-specific
   `AlwaysUserMuteStrategy` (which we only need because Twilio Media Streams
   loops bot output back into the inbound audio path). LiveKit does not
   have that loopback, so "livekit" is the correct transport_type for
   native LiveKit calls too.

4. **`PipelineRunner`** is not used in production today — the FastAPI
   `PipelineTask` runs inside the server's asyncio event loop. The
   per-call ECS model may bring `PipelineRunner` back into use (one runner
   per container).

5. **Room creation / token generation** was done via `pipecat.runner.livekit.configure()`
   in `run.py`, which reads CLI args. In production this becomes a
   dispatch service (one of LiveKit's native patterns) that creates a
   room per call and issues a bot token, or a cloud-functions webhook
   that fires when a SIP call arrives and an agent needs to be dispatched.

## Source history

- Original file: `run.py` (deleted 2026-04-20 in cleanup commit, part of the
  pre-LiveKit-migration housekeeping sprint).
- Last Pipecat version `run.py` was verified against: 0.0.108.
- Agents at the time: `chris/claim_status` (pre-migration slash-delimited
  naming, now `chris-claim-status` in the current Aurora schema).
