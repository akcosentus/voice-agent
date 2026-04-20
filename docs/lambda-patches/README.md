# Lambda patches — paper trail

These are patches applied to the deployed AWS Lambda function
`medcloud-voice-api` while the authoritative source repo location is
unknown. Each file in this directory captures a change that was
deployed out-of-band (zip + `aws lambda update-function-code`) without
going through a normal git workflow in the Lambda repo.

Once the Lambda source repo location is confirmed, these patches
should be applied there, committed into that repo's history, and this
directory can be deleted.

## Convention

- One file per deployment. Filename: `YYYY-MM-DD-short-kebab-description.patch`.
- Unified diff format, relative to whatever `/tmp/lambda-fix/src/index.mjs`
  held at the time.
- Matching entry in this README under **Applied patches**: date, summary,
  smoke-test results, outcome.

## Applied patches

### 2026-04-20 — remove Fish from TTS provider + model validation lists

**File**: [`2026-04-20-remove-fish-from-tts-validation.patch`](./2026-04-20-remove-fish-from-tts-validation.patch)

**Summary**: Part of the Fish Audio retirement. Removes `'fish'` from
`VALID_TTS_PROVIDERS` and the `fish:` key from `TTS_MODELS_BY_PROVIDER`.
Frontend auto-updates from the agent schema endpoint — no frontend
change needed.

**Deployed**: 2026-04-20 21:55 UTC.

**Post-deploy verification**: `GET /api/agent-schema` returns
`tts_providers: ['elevenlabs']` and `tts_models_by_provider` with only
the ElevenLabs key.

### 2026-04-20 — validate merged draft state on PUT + POST /agent-drafts

**File**: [`2026-04-20-validate-merged-draft-state.patch`](./2026-04-20-validate-merged-draft-state.patch)

**Summary**: Both Lambda draft handlers (POST upsert and PUT update)
were validating only the incoming patch, not the merged resulting
state. A partial payload like `{tts_model: "X"}` could land on an
existing row and leave the draft in an impossible combination (e.g.
`tts_provider="fish"` with an ElevenLabs-only model), which the
Publish flow then rejected at the wrong layer.

Fix: load the existing row by `agent_id`, merge with incoming fields,
run `validateAgentData` on the merged object. Reject at save time if
the result would be inconsistent.

**Deployed**: 2026-04-20 21:46 UTC via
`aws lambda update-function-code --function-name medcloud-voice-api`.

**Smoke tests**:

1. _Regression_ — PUT `{"has_unpublished_changes": true}` against
   `chris-claim-status` draft (already consistent ElevenLabs state).
   Expected 200 OK, TTS fields unchanged, `updated_at` tick. Passed.
2. _Rejection_ — PUT `{"tts_model": "s2-pro"}` against throwaway agent
   `test-validator-hardening` (stored state `tts_provider=elevenlabs`,
   `s2-pro` is Fish-only). Expected 400 Validation failed with
   `"Invalid tts_model: s2-pro for provider elevenlabs. Valid:
   eleven_turbo_v2_5, eleven_flash_v2_5, eleven_multilingual_v2"`.
   Passed. DB row unchanged post-rejection.

Test agent cleaned up after (`DELETE /api/agents/test-validator-hardening`).
