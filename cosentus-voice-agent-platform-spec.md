# Cosentus Voice Agent Platform — Build Specification

## Context for the developer

You are building a **config-driven voice agent platform** for a medical RCM company called Cosentus. The platform replaces Retell AI with a custom Pipecat-based system that costs 50-70% less per call.

A working proof-of-concept already exists: a single `bot.py` file that runs a voice agent called "Chris" over LiveKit WebRTC. Your job is to **restructure this into a multi-agent platform** and get it making real phone calls via Twilio.

### What already works
- Pipecat pipeline: Deepgram STT → Anthropic Claude Sonnet → ElevenLabs TTS
- Prompt hydration from `cases.json` with `{{variable}}` replacement
- Silero VAD tuned for insurance calls (confidence=0.45, start=0.25s, stop=1.0s)
- Prompt caching enabled on Anthropic (9,800 tokens cached after turn 1)
- `end_call` function tool
- Pre-seeded assistant first message for pattern anchoring

### What you're building
1. Config-driven agent system (YAML configs, prompt files, per-agent tools)
2. Twilio WebSocket telephony (outbound + inbound real phone calls)
3. Real tools: DTMF/press_digit, call transfer, end_call
4. Post-call data pipeline: transcripts, structured data extraction, recordings
5. Simple web frontend: agent picker, case data entry, call launcher, post-call viewer

### Key constraints
- **Use dummy patient data only** — no real PHI. This is a proof-of-concept.
- **Python 3.12+, uv for package management**
- **Pipecat framework** — do not build your own audio pipeline
- **Twilio WebSocket transport** — not LiveKit for telephony (simpler, more reliable for POC)
- **Anthropic Claude Sonnet** as LLM via direct API (not Bedrock yet)
- **ElevenLabs** for TTS, **Deepgram** for STT
- **FastAPI** for the backend server
- **No n8n** — all integrations are direct Python

---

## Repository Structure

```
cosentus-voice-platform/
├── agents/                          # Each agent is a folder
│   ├── chris/                       # Outbound insurance claim follow-up
│   │   ├── config.yaml              # Agent configuration
│   │   ├── prompt.txt               # System prompt with {{variables}}
│   │   └── tools.py                 # Agent-specific function tools
│   ├── cindy_outbound/              # Outbound patient balance reminders
│   │   ├── config.yaml
│   │   ├── prompt.txt
│   │   └── tools.py
│   ├── cindy_outbound_divb/         # Division B variant (different transfer #)
│   │   └── config.yaml              # Extends cindy_outbound, overrides transfer target
│   ├── cindy_inbound/               # Inbound patient billing support
│   │   ├── config.yaml
│   │   ├── prompt.txt
│   │   └── tools.py
│   └── chloe/                       # Inbound general line
│       ├── config.yaml
│       ├── prompt.txt
│       └── tools.py
├── core/                            # Shared platform code
│   ├── __init__.py
│   ├── pipeline.py                  # Builds Pipecat pipeline from config
│   ├── hydrator.py                  # Prompt {{variable}} replacement
│   ├── call_manager.py              # Manages outbound/inbound calls via Twilio
│   ├── transcript.py                # Real-time transcript capture
│   ├── post_call.py                 # Post-call data processing
│   ├── tools/                       # Shared tool implementations
│   │   ├── __init__.py
│   │   ├── dtmf.py                  # press_digit tool
│   │   ├── transfer.py              # transfer_call tool
│   │   └── end_call.py              # end_call tool
│   └── config_loader.py             # YAML config parser with inheritance
├── server/                          # FastAPI backend
│   ├── __init__.py
│   ├── main.py                      # FastAPI app, WebSocket handler for Twilio
│   ├── routes/
│   │   ├── agents.py                # GET /agents — list agents
│   │   ├── calls.py                 # POST /calls/outbound — trigger a call
│   │   └── history.py               # GET /calls/{id} — call results
│   └── ws_handler.py                # Twilio WebSocket connection handler
├── frontend/                        # Simple React frontend
│   ├── index.html
│   ├── app.jsx                      # Main app component
│   └── styles.css
├── data/
│   ├── dummy_cases/                 # Dummy test data (no PHI)
│   │   └── chris_cases.json         # Sample insurance claim cases
│   ├── calls/                       # Post-call output (auto-created)
│   │   └── {call_id}/
│   │       ├── transcript.json
│   │       ├── structured_data.json
│   │       ├── metadata.json
│   │       └── recording.wav
│   └── templates/
│       └── streams.xml              # Twilio TwiML template
├── .env                             # API keys (not committed)
├── .env.example                     # Template for .env
├── pyproject.toml
└── README.md
```

---

## Config Schema (YAML)

Each agent has a `config.yaml`. Here is the full schema:

```yaml
# agents/chris/config.yaml

name: chris
display_name: "Chris — Insurance Follow-up"
description: "Outbound agent that calls payers to check claim status"
type: outbound  # outbound | inbound

# LLM settings
llm:
  provider: anthropic           # anthropic | openai | aws_bedrock
  model: claude-sonnet-4-20250514
  max_tokens: 200
  temperature: 0.7
  enable_prompt_caching: true

# Text-to-Speech
tts:
  provider: elevenlabs          # elevenlabs | cartesia | aws_polly
  voice_id: "${ELEVENLABS_VOICE_ID}"  # resolved from .env
  model: eleven_turbo_v2_5
  settings:
    stability: 0.5
    similarity_boost: 0.8
    speed: 1.0

# Speech-to-Text
stt:
  provider: deepgram            # deepgram | aws_transcribe
  model: nova-3

# Voice Activity Detection
vad:
  confidence: 0.45
  start_secs: 0.25
  stop_secs: 1.0               # tuned for insurance reps who pause a lot

# Telephony
telephony:
  outbound_number: "${TWILIO_PHONE_NUMBER}"  # Twilio number to call FROM
  # For inbound agents: this is the number patients/callers dial

# Transfer targets (agent calls transfer_call("billing") → dials this number)
transfer_targets:
  billing: "+14085559999"
  supervisor: "+14085558888"

# What tools this agent has access to
tools:
  - end_call
  - press_digit                 # DTMF for IVR navigation
  - transfer_call               # warm/cold transfer

# Pre-seeded first assistant message (sets behavioral pattern)
first_message: "Hello, I'm just calling to check on a claim."

# Post-call configuration
post_call:
  save_transcript: true
  save_recording: true
  # Structured fields to extract from the conversation after the call
  # The post-call processor will use the LLM to extract these from the transcript
  structured_fields:
    - field: claim_number
      type: string
      description: "The claim number from the payer"
    - field: claim_status
      type: enum
      options: [paid, denied, in_process, not_on_file, underpaid, applied_to_deductible]
      description: "The status of the claim"
    - field: payment_amount
      type: string
      description: "Amount paid, if applicable"
    - field: denial_reason
      type: string
      description: "Reason for denial, if applicable"
    - field: reference_number
      type: string
      description: "Call reference number from the rep"
    - field: rep_name
      type: string
      description: "Name of the representative"
    - field: next_steps
      type: string
      description: "What actions need to be taken"
    - field: follow_up_needed
      type: boolean
      description: "Does this claim need a human callback?"
  destination: local            # local | s3 | sharepoint (future)

# Case data source — where to get the {{variables}} for this agent
case_data:
  source: json                  # json | csv | api (future: advancedmd, medcloud)
  path: "data/dummy_cases/chris_cases.json"
```

### Config inheritance for variants

Division variants extend a base config and override specific fields:

```yaml
# agents/cindy_outbound_divb/config.yaml

extends: cindy_outbound/config.yaml

overrides:
  display_name: "Cindy Outbound — Division B"
  transfer_targets:
    billing: "+14085557777"     # Division B's billing number
  telephony:
    outbound_number: "${TWILIO_PHONE_NUMBER_DIVB}"
```

The config loader merges the base config with overrides using deep merge.

---

## Core Components

### pipeline.py — Build Pipecat pipeline from config

This is the heart of the system. It reads an agent config and constructs the full Pipecat pipeline.

```python
# Pseudocode structure — implement with real Pipecat classes

def build_pipeline(config: AgentConfig, case_data: dict, websocket, call_data: dict):
    """
    Build a complete Pipecat pipeline from an agent config.
    
    1. Load and hydrate the prompt with case_data
    2. Instantiate STT, LLM, TTS based on config
    3. Register tools from config.tools + agent-specific tools
    4. Set up transcript capture
    5. Set up post-call handler
    6. Return the pipeline ready to run
    """
    # Hydrate prompt
    prompt = hydrate_prompt(config.prompt_path, case_data)
    
    # Build services based on config
    stt = create_stt(config.stt)
    llm = create_llm(config.llm)
    tts = create_tts(config.tts)
    vad = create_vad(config.vad)
    
    # Create transport (Twilio WebSocket)
    transport = create_twilio_transport(websocket, call_data)
    
    # Register tools
    tools = load_tools(config, call_data)
    
    # Build context with pre-seeded message
    context = build_context(prompt, config.first_message, tools)
    
    # Create pipeline
    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        assistant_aggregator,
    ])
    
    return pipeline
```

### hydrator.py — Prompt variable replacement

```python
def hydrate_prompt(prompt_path: str, case_data: dict) -> str:
    """
    Read prompt template and replace all {{Variable_Name}} placeholders
    with values from case_data. 
    
    - If a variable has no value, replace with empty string
    - Always inject {{current_time}} with formatted current datetime
    - The prompt file is the EXACT same format as Retell prompts
    """
    with open(prompt_path) as f:
        prompt = f.read()
    
    for key, value in case_data.items():
        prompt = prompt.replace(f"{{{{{key}}}}}", str(value) if value else "")
    
    prompt = prompt.replace("{{current_time}}", 
                           datetime.now().strftime("%A, %B %d, %Y at %I:%M %p"))
    return prompt
```

### config_loader.py — YAML config parser with inheritance

```python
def load_agent_config(agent_name: str) -> AgentConfig:
    """
    Load agent config from agents/{agent_name}/config.yaml.
    
    If config has 'extends' field, load the base config first
    and deep-merge overrides on top.
    
    Resolve ${ENV_VAR} references from .env.
    """
```

### Tools

#### dtmf.py — press_digit

```python
# This tool sends DTMF tones via the Twilio API
# The LLM calls this when navigating IVR menus
# 
# Accepts a string of digits: "1", "12345", "1234567890"
# Sends each digit sequentially with appropriate pausing
#
# Implementation: Use the Twilio REST API to send DTMF on the active call
# https://www.twilio.com/docs/voice/api/call-resource#update-a-call-resource
# 
# The call_sid is available from the WebSocket connection data
```

#### transfer.py — transfer_call

```python
# Transfers the active call to a target phone number
# The LLM calls this with a target name: transfer_call("billing")
# The target name maps to a phone number from config.transfer_targets
#
# Implementation: Use Twilio REST API to update the call with new TwiML
# that dials the transfer target. The original caller stays connected.
#
# Two modes:
# - Cold transfer: redirect call to target, bot disconnects
# - Warm transfer: bot stays on briefly to introduce, then disconnects
```

#### end_call.py — end_call

```python
# Ends the call gracefully
# Triggers post-call processing before disconnecting
# 
# Steps:
# 1. Stop the pipeline (queue EndTaskFrame)
# 2. Run post-call data extraction
# 3. Save transcript, structured data, recording
# 4. Hang up via Twilio API
```

### post_call.py — Post-call data processing

```python
"""
After each call ends, this module:

1. Takes the full transcript (list of {role, content, timestamp} messages)
2. Takes the agent config's structured_fields definition
3. Makes ONE LLM call to extract structured data from the transcript
4. Saves everything to data/calls/{call_id}/

The extraction prompt looks like:
    "Given this transcript of an insurance follow-up call, extract the following fields:
     - claim_number (string): The claim number from the payer
     - claim_status (enum: paid/denied/in_process/...): The status
     ..."

Output structure per call:

data/calls/{call_id}/
├── transcript.json       # [{role: "user", content: "...", timestamp: "..."}, ...]
├── structured_data.json  # {claim_number: "ABC123", claim_status: "paid", ...}
├── metadata.json         # {agent: "chris", duration_secs: 280, started_at: "...", 
│                         #  phone_number_dialed: "+1...", case_data: {...}}
└── recording.wav         # Full call audio (if recording enabled)
"""
```

---

## Server (FastAPI)

### main.py — FastAPI app

```python
"""
FastAPI server that:
1. Serves the frontend at /
2. Handles Twilio webhook for inbound calls at POST /twilio/incoming
3. Handles Twilio WebSocket connections at WS /ws
4. Provides REST API for the frontend:
   - GET  /api/agents           → list all configured agents
   - GET  /api/agents/{name}    → get agent config + available case data
   - POST /api/calls/outbound   → trigger an outbound call
   - GET  /api/calls            → list recent calls with metadata
   - GET  /api/calls/{id}       → get call results (transcript, structured data)

For LOCAL development:
- Run with `uvicorn server.main:app --reload`
- Use ngrok to expose the server for Twilio webhooks
- ngrok http 8000 → copy the https URL
- Set this URL in Twilio TwiML for inbound, and as the WebSocket URL for outbound
"""
```

### ws_handler.py — Twilio WebSocket handler

```python
"""
When Twilio establishes a WebSocket connection (for inbound or outbound calls):

1. Parse the initial WebSocket messages to get stream_sid, call_sid, and custom params
2. Determine which agent to use:
   - Outbound: custom params include agent_name and case_data
   - Inbound: look up which agent is mapped to the Twilio phone number that was called
3. Load the agent config
4. Build the Pipecat pipeline using pipeline.py
5. Run the pipeline until the call ends
6. Trigger post-call processing

IMPORTANT: Twilio Media Streams use 8kHz mono audio (mulaw encoding).
Set audio_in_sample_rate=8000 and audio_out_sample_rate=8000 in PipelineParams.

Use pipecat's TwilioFrameSerializer for proper audio framing.
Provide TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN to the serializer
for automatic call hangup when the pipeline ends.
"""
```

### routes/calls.py — Outbound call trigger

```python
"""
POST /api/calls/outbound
Body:
{
    "agent_name": "chris",
    "target_number": "+18001234567",    # number to call (the payer)
    "case_data": {                       # variables for prompt hydration
        "Practice_Name": "Demo Medical Group",
        "NPI": "1234567890",
        "Patient_Name": "Smith, John",
        "Patient_Birth_Date": "01/15/1985",
        "Service_Date": "12/10/2025",
        ...
    }
}

This endpoint:
1. Validates the agent exists and case_data has required fields
2. Generates a unique call_id
3. Creates a Twilio outbound call using the REST API:
   - to: target_number
   - from: config.telephony.outbound_number
   - twiml: <Response><Connect><Stream url="wss://your-ngrok-url/ws">
              <Parameter name="agent_name" value="chris"/>
              <Parameter name="call_id" value="{call_id}"/>
              <Parameter name="case_data" value="{base64_encoded_case_data}"/>
            </Stream></Connect></Response>
4. Returns {call_id, status: "initiating"}

The case_data is passed as a base64-encoded JSON string in the TwiML Stream
parameters so the WebSocket handler can access it when the connection opens.
"""
```

---

## Frontend (Simple Call Launcher)

A single-page React app served by FastAPI. No build step — use CDN React + Babel for simplicity.

### Features
1. **Agent selector**: Dropdown listing all agents from GET /api/agents
2. **Case data form**: Dynamic form fields based on the selected agent's prompt variables. Pre-populate with dummy data from the agent's case_data source.
3. **Target number input**: Phone number to dial (for outbound) or display the inbound number to call
4. **Call button**: Triggers POST /api/calls/outbound
5. **Live status**: Shows call state (ringing, connected, ended)
6. **Post-call results**: After call ends, displays:
   - Structured data (claim_number, status, etc.) in a clean card
   - Full transcript in a scrollable panel
   - Audio playback if recording exists
   - Call duration and metadata

### Tech
- React via CDN (no npm/webpack needed)
- Fetch API for REST calls
- Simple polling or WebSocket for live call status
- Tailwind via CDN for styling

---

## Dummy Test Data

Create realistic but completely fake data for testing:

```json
// data/dummy_cases/chris_cases.json
[
  {
    "Practice_Name": "Pacific Coast Orthopedics",
    "NPI": "1234567890",
    "Tax_ID": "87-6543210",
    "Billing_Address": "123 Medical Drive, Suite 200, Irvine CA 92618",
    "Call_Back#": "949-555-0100",
    "Provider": "Kim, David",
    "Patient_Name": "Smith, John",
    "Patient_Birth_Date": "01/15/1985",
    "Primary_Carrier_Policy#": "XYZ123456789",
    "Service_Date": "12/10/2025",
    "Service_Location": "Pacific Coast Surgery Center",
    "Primary_Carrier_Name": "Blue Cross Blue Shield",
    "Total_Charge": "2500.00",
    "Claim#": "CLM2025121001",
    "Ins_Pmt": "0.00",
    "Ins_Balance": "2500.00",
    "CPT_1": "27447",
    "CPT_2": "27447-59",
    "CPT_3": "",
    "CPT_4": "",
    "Acct#": "PCT-2025-0042"
  }
]
```

---

## .env.example

```bash
# Anthropic (LLM)
ANTHROPIC_API_KEY=

# Deepgram (STT)
DEEPGRAM_API_KEY=

# ElevenLabs (TTS)
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=

# Twilio (Telephony)
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=+1xxxxxxxxxx

# Server
SERVER_URL=https://your-ngrok-url.ngrok.io
PORT=8000
```

---

## pyproject.toml dependencies

```toml
[project]
name = "cosentus-voice-platform"
version = "0.1.0"
requires-python = ">=3.12"

dependencies = [
    "pipecat-ai[anthropic,deepgram,elevenlabs,silero]",
    "fastapi",
    "uvicorn[standard]",
    "python-dotenv",
    "twilio",
    "pydantic>=2.0",
    "pyyaml",
    "aiofiles",
]
```

---

## Build Order

### Step 1: Repo scaffold + config system
Create the directory structure, config_loader.py (with inheritance), and the dummy agent configs. Test that loading `agents/chris/config.yaml` returns a properly merged config object.

### Step 2: Core pipeline builder
Refactor the existing bot.py into core/pipeline.py. It should accept an AgentConfig and case_data dict, and return a configured Pipecat pipeline. This is mostly extracting what exists into a reusable function. Keep the Anthropic LLM, Deepgram STT, ElevenLabs TTS, Silero VAD configuration working exactly as it did before.

### Step 3: Twilio WebSocket transport
Replace the LiveKit transport with Twilio WebSocket transport. Set up:
- FastAPI server with WebSocket endpoint at /ws
- TwiML template for inbound calls
- Outbound call trigger using Twilio REST API
- TwilioFrameSerializer for audio framing (8kHz mulaw)
- Test with an inbound call first (you call the Twilio number, bot answers)
- Then test outbound (bot calls your phone)

### Step 4: Tools
Implement press_digit (DTMF via Twilio API), transfer_call (Twilio call update), and end_call (pipeline shutdown + Twilio hangup). Register these based on the agent config's tools list.

### Step 5: Post-call pipeline
Implement transcript capture during the call (using Pipecat's transcript processors) and structured data extraction after the call (one LLM call to extract fields defined in config). Save everything to data/calls/{call_id}/.

### Step 6: Frontend
Build the simple call launcher UI. Serve it from FastAPI at /. Connect it to the REST API endpoints.

### Step 7: Migrate Chris's prompt
Copy the existing Chris system_prompt.txt (the full insurance follow-up prompt) into agents/chris/prompt.txt. Create the dummy case data. Do a full end-to-end test: launch the frontend, select Chris, load dummy data, trigger an outbound call to your phone, play the insurance rep, and verify post-call data is captured correctly.

---

## Critical Implementation Notes

### Audio sample rates
Twilio Media Streams use 8kHz mulaw audio. You MUST set:
```python
PipelineParams(
    audio_in_sample_rate=8000,
    audio_out_sample_rate=8000,
)
```
Failure to do this causes garbled audio or silence.

### Prompt caching still works
The Anthropic prompt caching configuration from the existing bot should carry over unchanged. The system prompt is ~9,800 tokens and gets cached after the first turn. Verify cache hit/miss in logs.

### VAD settings are insurance-call specific
The VAD params (confidence=0.45, start=0.25s, stop=1.0s) are tuned for insurance calls where reps pause frequently. Other agents (patient-facing) may need different tuning — this is why VAD config is per-agent in the YAML.

### Summarization must stay disabled
Do NOT enable context summarization on the LLM context. The full conversation history must be preserved for the model to maintain behavioral patterns (brevity, echo-back, one-question-at-a-time). The context window is large enough for any single call.

### The agent's prompt IS the product
Do not modify the prompt content. The prompts have been refined through hundreds of test scenarios on Retell. The platform's job is to run the prompt faithfully with the right infrastructure around it — not to change how the prompt works.

### DTMF implementation detail
When the LLM calls press_digit with a string of digits (e.g., "1234567890" for an NPI), send each digit with a 200ms pause between them. Twilio's SendDigits TwiML parameter handles this natively: `<Play digits="1w2w3w4w5w6w7w8w9w0"/>` where `w` is a 500ms wait, or you can use the REST API's `sendDigits` parameter on call update.

### Transcript format
Store transcripts as an array of message objects:
```json
[
  {"role": "assistant", "content": "Hello, I'm just calling to check on a claim.", "timestamp": "2026-03-23T13:24:55Z"},
  {"role": "user", "content": "Hi, what can I help you with?", "timestamp": "2026-03-23T13:24:58Z"},
  {"role": "assistant", "content": "Yeah, I need to check on a claim status.", "timestamp": "2026-03-23T13:25:01Z"}
]
```

### Post-call structured extraction
Use a SEPARATE, cheaper LLM call (Claude Haiku or Sonnet) after the call ends to extract structured fields. Do NOT try to extract during the call — it adds latency and complexity. The extraction prompt is generated from the config's structured_fields definition.

### ngrok for local development
You need ngrok to expose your local server for Twilio webhooks. Run `ngrok http 8000` and use the HTTPS URL as your SERVER_URL. Update TwiML and outbound call configs to use this URL. The ngrok URL changes on restart (unless you pay for a fixed subdomain).
