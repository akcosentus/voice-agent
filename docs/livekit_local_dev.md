# LiveKit local dev — runbook

This is how to run the voice agent locally against a LiveKit server
and talk to it from a browser. Intended for iterating on the LiveKit
migration before we cut over from Twilio in production.

Prerequisites, one-shot commands, troubleshooting. Not a design doc —
for that, see `docs/livekit_reference.md` and the comments at the top
of `server/livekit_handler.py`.

## Prerequisites

### Required

- macOS (Apple Silicon or Intel). Linux should work but not tested here.
- Homebrew (`brew`)
- `uv` managing the repo venv (`.venv/` already present after
  `uv sync`)
- A working `.env` at the repo root with at least:
  - `ANTHROPIC_API_KEY`
  - `FISH_API_KEY`
  - `DEEPGRAM_API_KEY`
  - `COSENTUS_API_KEY` (for agent-config lookup via Lambda)
  - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` if you want the
    recording to upload to S3 — not required for the conversation
    itself; upload failure is logged and ignored.

### Container runtime

We use Colima + the standalone docker CLI (Docker Desktop avoided
intentionally — licensing). One-time install:

```bash
brew install colima docker
colima start --cpu 2 --memory 4
docker version                  # verify daemon reachable
```

To stop the runtime later:

```bash
colima stop
```

Per-session restart if the VM's already been created:

```bash
colima start
```

## Step 1 — start LiveKit server locally

One terminal, foreground, `Ctrl-C` to stop. `--rm` so the container
disappears on exit.

```bash
docker run --rm \
  --name livekit-dev \
  -p 7880:7880 \
  -p 7881:7881 \
  -p 7882:7882/udp \
  -e LIVEKIT_KEYS="devkey: secret" \
  livekit/livekit-server --dev --bind 0.0.0.0
```

The `--bind 0.0.0.0` flag is **required** under Colima/Docker: `--dev`
alone binds only to `127.0.0.1` inside the container, and Docker's port
forwarding delivers packets from the bridge interface — the handshake
silently fails as "WebSocket protocol error: Handshake not finished".
Explicit `--bind 0.0.0.0` binds on all container interfaces so the
forwarded packets are accepted. Verified against LiveKit server 1.11.0.

`--dev` uses well-known static dev credentials:

| Field           | Value                    |
| --------------- | ------------------------ |
| API key         | `devkey`                 |
| API secret      | `secret`                 |
| WebSocket URL   | `ws://localhost:7880`    |

These match the defaults in `server/livekit_local_entrypoint.py`. You
only need to override them if you point at a non-default server.

**Sanity-check the server** before moving on. A plain HTTP GET at `/`
returns no body (LiveKit only speaks on its own paths), so test the
WebSocket upgrade path directly:

```bash
uv run python - <<'EOF'
import asyncio
from livekit import rtc
from server.livekit_local_entrypoint import _generate_token

async def probe():
    tok = _generate_token("devkey", "secret", identity="probe", room_name="probe-room")
    room = rtc.Room()
    await room.connect("ws://localhost:7880", tok)
    print(f"connected, room={room.name}")
    await room.disconnect()

asyncio.run(probe())
EOF
# Expect: connected, room=probe-room
```

If you instead see `WebSocket protocol error: Handshake not finished`,
re-check the `--bind 0.0.0.0` flag on the server.

## Step 2 — run the agent bot

Second terminal, from the repo root:

```bash
uv run python -m server.livekit_local_entrypoint \
  --agent=chris-claim-status \
  --room=test-1 \
  --url=ws://localhost:7880 \
  --api-key=devkey \
  --api-secret=secret
```

**Why the explicit `--url` / `--api-key` / `--api-secret` here?** Config
precedence is `CLI arg > $LIVEKIT_URL env var > hardcoded default`. If
your repo `.env` has `LIVEKIT_URL=wss://something.livekit.cloud` (from
LiveKit Cloud experimentation), the env var takes precedence over the
localhost hardcoded default and your bot will silently try to dial the
cloud. Passing the CLI flags forces localhost. The startup banner
prints the resolved value and where it came from
(`[cli]` / `[env:LIVEKIT_URL]` / `[default]`) for visibility.

If you're not going to use LiveKit Cloud for anything else, easier to
just comment those vars out in `.env`.

Useful flags:

- `--case-data '{"NPI":"1831323708","Patient_Name":"Alex"}'` — hydrate
  the agent prompt with case variables. Unlike production, for local
  dev you can use fake data; the PHI filter still runs, so don't use
  real patient data even locally.
- `--room=anything` — any string, the server creates the room on join.
- `--agent=<name>` — must match an agent record in Aurora (check
  `GET /api/agents`). Canonical form is hyphenated
  (`chris-claim-status`), not the pre-migration slash form
  (`chris/claim_status`).

The entrypoint prints a banner like:

```
==============================================================================
LIVEKIT LOCAL DEV — VOICE AGENT BOT
==============================================================================
  Agent:     chris-claim-status
  Room:      test-1
  LiveKit:   ws://localhost:7880
  Case keys: ['NPI', 'Patient_Name']

  Paste this URL into a browser to join the room as the caller:

    https://meet.livekit.io/custom?liveKitUrl=...&token=...

  Allow microphone access when the browser prompts. The bot is
  already in the room; on_first_participant_joined fires the
  moment you connect.
==============================================================================
```

## Step 3 — join as the caller

Copy the printed URL into a browser (Chrome or Safari both work).

1. Browser asks for microphone permission — allow it.
2. You land in `meet.livekit.io/custom` already joined to the room.
3. `on_first_participant_joined` fires inside the bot process; you'll
   see it in the bot terminal.

You're now the human in the conversation. Speak naturally.

## Clean shutdown

- **Bot:** `Ctrl-C` in the bot terminal. The handler runs the `finally`
  block, saves the call record to Lambda (if reachable), and exits.
- **LiveKit server:** `Ctrl-C` in the docker terminal. `--rm` deletes
  the container.
- **Colima VM:** leave running (cheap), or `colima stop` to reclaim
  resources. Re-running `colima start` later is idempotent.

## Troubleshooting

### `docker: command not found`

Colima isn't installed or started. See the Prerequisites section.

### `ws://localhost:7880` refused / timeout

- `docker ps` — is the container running?
- `lsof -iTCP:7880` — does something else own the port?
- LiveKit logs (docker terminal) — did it crash on startup?

### Bot connects but the browser fails to join

- Check your browser didn't block mic permissions.
- Firefox sometimes has issues with self-signed WS; Chrome/Safari are
  more tolerant for `ws://localhost`.
- If you see `401 unauthorized` in browser devtools, the token is
  malformed or the API key/secret don't match what LiveKit is running
  with. The entrypoint uses `devkey`/`secret` by default; if you pass
  `--api-key`/`--api-secret`, make sure the server is started with
  matching values.

### Bot joins, browser joins, but no audio

- Check the bot terminal for STT service logs (Deepgram). Missing
  `DEEPGRAM_API_KEY` is the most common cause.
- Check TTS logs (Fish or ElevenLabs depending on agent config).
  Missing `FISH_API_KEY` / `ELEVENLABS_API_KEY` will show as a
  connection refusal in the bot terminal.
- Not Hearing Chris's greeting? Speak first — Chris responds to audio
  input, he doesn't auto-greet in every path. This matches the Twilio
  behaviour today.

### Known rough edges

- **Multi-digit DTMF (`press_digit` tool):** the tool fires but digit
  pacing is too tight for some IVRs — only the first digit reliably
  registers. Documented in the Task 3 call transcript analysis. Fix is
  queued; do not fix in this branch.
- **`on_first_participant_joined` with caller already present:** the
  Pipecat transport fires this once the *first* participant is seen.
  If you join the room before the bot does, behaviour is unverified;
  the POC assumes the bot joins first (this is the correct production
  pattern — dispatcher starts the bot, human then dials in).

## Agent → LiveKit event mapping

| Twilio (`server/ws_handler.py`) | LiveKit (`server/livekit_handler.py`) | When                                            |
| ------------------------------- | ------------------------------------- | ----------------------------------------------- |
| `on_client_connected`           | `on_first_participant_joined`         | First human joins room — start recording etc.   |
| `on_client_disconnected`        | `on_participant_left`                 | Any participant leaves — cancel pipeline task.  |
| n/a                             | `on_connected`                        | Bot itself finished joining the room (informational). |

`on_participant_left` is the transport-agnostic event per Pipecat docs;
`on_participant_disconnected` fires alongside it with the same data.
Using the `_left` variant matches Pipecat's direction-of-travel toward
transport-neutral naming.

## Production mapping (future)

When we migrate production to LiveKit:

1. The dispatcher (Twilio SIP trunk → LiveKit or direct LiveKit SIP
   ingress) creates a room per call and hands us a bot token.
2. Our FastAPI or worker process imports `server.livekit_handler` and
   calls `run_livekit_agent(url, token, room_name, agent_name,
   case_data)` exactly as this entrypoint does — no code change.
3. `livekit_local_entrypoint.py` is local-only; the handler is not.

See `docs/livekit_reference.md` for the broader migration shape.
