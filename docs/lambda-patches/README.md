# Lambda patches — paper trail + replay guide

These are patches applied to the deployed AWS Lambda function
`medcloud-voice-api` while the authoritative source repo location is
unknown (as of 2026-04-23; Lakshay investigating). Each file in this
directory captures a change that was deployed out-of-band — the
workflow was:

```
aws lambda get-function --function-name medcloud-voice-api \
    --query 'Code.Location' --output text \
    | xargs curl -sS -o /tmp/lambda-fix.zip
unzip /tmp/lambda-fix.zip -d /tmp/lambda-fix
# ... edit /tmp/lambda-fix/src/index.mjs ...
( cd /tmp/lambda-fix && zip -r ../lambda-fix-out.zip . )
aws lambda update-function-code --function-name medcloud-voice-api \
    --zip-file fileb:///tmp/lambda-fix-out.zip
```

Once the Lambda source repo location is confirmed (or a fresh
`cosentus-voice-api-lambda` repo is created), these patches should be
replayed against it, committed into normal history, and this directory
deleted.

## How to replay against a new / recovered source repo

The patches here are unified diffs against the file we pulled out of
the deployed zip, which is `src/index.mjs`. The deployed Lambda is
Node 20.x with everything in a single `index.mjs` file plus a
`node_modules/` directory (no bundler, no build step observed).

For each patch below:

1. **Locate the function** — use the *Function* column to `grep` the
   authoritative source for the symbol, rather than trying to match
   the line numbers in the patch (our `/tmp/lambda-fix` line numbers
   will almost certainly drift from a clean repo).
2. **Match the BEFORE text** — each patch entry below includes a
   short literal "before" snippet that was in the code when we
   deployed. If that exact text is present in the new source, apply
   the change. If it's been refactored, apply the intent.
3. **Run the verification commands** — each patch has a copy-pasteable
   test command that confirms the deployed behavior matches what the
   patch produces. Run these against your dev/staging Lambda after
   replay.

The patches are also safe to skip entirely if the new repo's code
already reflects the deployed behavior (e.g. if the owner did clean
ports from live before the repo was shared).

## Applied patches

---

### P1 — 2026-04-20 — remove Fish from TTS provider + model validation lists

**File**: [`2026-04-20-remove-fish-from-tts-validation.patch`](./2026-04-20-remove-fish-from-tts-validation.patch)

**Function to patch**: module-scope `const VALID_TTS_PROVIDERS` and
`const TTS_MODELS_BY_PROVIDER` declarations. Look for them near the
`VALID_LLM_MODELS` declaration at module top.

**Before (grep for this)**:

```js
const VALID_TTS_PROVIDERS = ['elevenlabs', 'fish'];
const TTS_MODELS_BY_PROVIDER = {
  elevenlabs: ['eleven_turbo_v2_5', 'eleven_flash_v2_5', 'eleven_multilingual_v2'],
  fish: ['s1', 's2', 's2-mini', 's2-pro'],
};
```

**After**:

```js
const VALID_TTS_PROVIDERS = ['elevenlabs'];
const TTS_MODELS_BY_PROVIDER = {
  elevenlabs: ['eleven_turbo_v2_5', 'eleven_flash_v2_5', 'eleven_multilingual_v2'],
};
```

**Context**: part of the Fish Audio retirement. No agent in Aurora was
using Fish any more by the time of deploy. The Python pipeline's Fish
branch was removed in parallel (`voice-engine` main commit `28d7763`)
and the frontend auto-updates from `GET /api/agent-schema`.

**Deployed**: 2026-04-20 21:55 UTC.

**Verification (run after replay)**:

```bash
curl -sS https://api.cosentusaibackend.com/api/agent-schema \
  -H "X-API-Key: $COSENTUS_API_KEY" \
  | jq '{tts_providers, tts_models_by_provider, tts_models}'
```

Expected:

```json
{
  "tts_providers": ["elevenlabs"],
  "tts_models_by_provider": {
    "elevenlabs": ["eleven_turbo_v2_5", "eleven_flash_v2_5", "eleven_multilingual_v2"]
  },
  "tts_models": ["eleven_turbo_v2_5", "eleven_flash_v2_5", "eleven_multilingual_v2"]
}
```

Also try to create an agent with `tts_provider: "fish"` — expect
`400 Validation failed` with `Invalid tts_provider: fish. Valid:
elevenlabs`.

---

### P2 — 2026-04-20 — validate merged draft state on PUT + POST /agent-drafts

**File**: [`2026-04-20-validate-merged-draft-state.patch`](./2026-04-20-validate-merged-draft-state.patch)

**Functions to patch**: the handlers for `POST /api/agent-drafts`
(which is an upsert — ON CONFLICT DO UPDATE) and `PUT /api/agent-drafts/:id`
(the same row, by agent_id). In the deployed code both handlers live
in the single `index.mjs` router around the `if (method === 'POST' && path === '/api/agent-drafts')`
and `if (method === 'PUT' && path.startsWith('/api/agent-drafts/'))`
branches.

**Before (both handlers had the same pattern)**:

```js
const vErrors = validateAgentData(body);
if (vErrors.length) return respond(400, { error: 'Validation failed', details: vErrors });
```

**After (both handlers)**:

```js
// Validate the MERGED state (existing row + incoming fields), not
// just the incoming patch. A partial payload like {tts_model: "X"}
// can otherwise land on an existing row and leave it in an impossible
// combination with a previously-set field.
const existing = await pool.query(
  'SELECT * FROM voice_agent_drafts WHERE agent_id = $1',
  // POST uses body.agent_id; PUT uses the path param :id
  [body.agent_id ?? id]
);
const merged = { ...(existing.rows[0] || {}), ...body };
const vErrors = validateAgentData(merged);
if (vErrors.length) return respond(400, { error: 'Validation failed', details: vErrors });
```

**Context**: the frontend's agent editor previously sent single-field
updates (e.g. `{tts_model: "eleven_flash_v2_5"}`) which could land on
a row with `tts_provider: "fish"` and pass validation. The Publish
flow then rejected at a downstream layer, producing a confusing error
message. The frontend was fixed in parallel (writes `tts_provider`
atomically with voice + model) but the Lambda needed defensive
merged-state validation for any non-UI caller.

**Deployed**: 2026-04-20 21:46 UTC.

**Verification (run after replay)**:

```bash
# 1. Create a throwaway agent in a known-good state
curl -sS -X POST https://api.cosentusaibackend.com/api/agents \
  -H "X-API-Key: $COSENTUS_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"test-merged-validation","tts_provider":"elevenlabs",
       "tts_model":"eleven_flash_v2_5","tts_voice_id":"<valid elevenlabs voice>"}'

# 2. Try a partial update that would produce an invalid merged state.
#    (Only meaningful once the Lambda has a second provider in the future.
#     Today 'elevenlabs' is the only valid provider, so use a more direct
#     check: PUT a bogus model.)
curl -sS -X PUT https://api.cosentusaibackend.com/api/agent-drafts/test-merged-validation \
  -H "X-API-Key: $COSENTUS_API_KEY" -H "Content-Type: application/json" \
  -d '{"tts_model":"s2-pro"}'
# Expected: 400 Validation failed (tts_model invalid for provider elevenlabs)

# 3. Confirm DB row unchanged after the 400
curl -sS https://api.cosentusaibackend.com/api/agents/test-merged-validation \
  -H "X-API-Key: $COSENTUS_API_KEY" | jq '.tts_model'
# Expected: "eleven_flash_v2_5" (still)

# 4. Clean up
curl -sS -X DELETE https://api.cosentusaibackend.com/api/agents/test-merged-validation \
  -H "X-API-Key: $COSENTUS_API_KEY"
```

---

## Order of operations when replaying

Both patches are independent — either order is fine. The Fish-removal
patch (P1) is guaranteed safe because no live agent references Fish.
The merged-state validation patch (P2) is guaranteed safe because the
change is strictly more restrictive than the old behavior (it rejects
some previously-accepted inconsistent states; it doesn't accept any
new input).

## If neither patch replays cleanly

Don't force them. The deployed Lambda is the ground truth. Work
backwards from live behavior:

```bash
# Fetch the live code (requires IAM: lambda:GetFunction)
aws lambda get-function --function-name medcloud-voice-api \
    --query 'Code.Location' --output text \
    | xargs curl -sS -o /tmp/live-lambda.zip
unzip /tmp/live-lambda.zip -d /tmp/live-lambda
diff -ruN /path/to/new-repo/src/ /tmp/live-lambda/src/
```

Anything the diff shows that's NOT from these two patches is either
(a) a pre-existing change from whoever managed the Lambda before us
that we never documented, or (b) a more recent patch that landed
after 2026-04-20 and needs a new entry here.

## See also

- `docs/lambda-patches/TECH-DEBT.md` — broader audit of Lambda issues
  observed while working with it. Not blocking patch replay but worth
  reading before the new repo owner starts making changes.
