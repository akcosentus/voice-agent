# `medcloud-voice-api` — observed tech debt

Notes from working against the deployed Lambda over March–April 2026.
Written for whoever ends up owning the new `cosentus-voice-api-lambda`
repo (once it exists). Nothing here is blocking; everything here has
bitten us at least once.

Prioritized roughly by (likelihood-to-bite-next) × (effort-to-fix).

---

## TL;DR — top five to do first

1. **Stand up a real source repo.** Everything else in this doc
   assumes you have one. Right now the deployed zip is the only source
   of truth and patches are `.patch` files in a downstream repo
   (`docs/lambda-patches/`). Every hour without a proper repo is more
   risk — there's no rollback plan beyond manually reversing edits.
2. **Add CI + unit tests for the validators.** `validateAgentData` is
   the single biggest bug surface (two of the two patches we've had
   to apply touched it). It's a pure function with no external
   dependencies — trivially unit-testable.
3. **Monitoring** — CloudWatch alarms on error rate, duration, and
   throttles. We've deployed patches blind; nobody would notice if
   the error rate spiked until the frontend stopped working.
4. **Consolidate the three code paths for "save an agent draft"**
   (POST upsert, PUT update, and inferred PATCH-on-POST from the
   frontend). Currently all three exist; each time we fix a bug we
   have to apply the fix to multiple handlers (see patch P2).
5. **Document the deployed schema.** There is no single source of
   truth for "what fields does an agent have and what are their valid
   values" — the answer is split across Aurora CHECK constraints, the
   Lambda's `validateAgentData`, the Lambda's `AGENT_SCHEMA.defaults`,
   and the frontend's `agent-draft.ts` mapper. All four have drifted
   at least once.

---

## 1. Workflow + DevEx

### 1.1 No source-controlled repo

Already called out above. Patches get committed as `.patch` files in
the voice-engine repo's `docs/lambda-patches/` folder. No PRs, no
reviews, no CI, no versioning. The deployed zip is the source of
truth.

**Fix**: create `cosentus-voice-api-lambda`, seed from the current
deployed zip, apply the two recorded patches, merge.

### 1.2 No local dev loop

To test a Lambda change today we:

1. Edit `/tmp/lambda-fix/src/index.mjs`
2. Zip + deploy to prod-shared AWS account
3. Smoke test via curl against the live API Gateway
4. Revert by editing again if broken

There's no "run the Lambda locally against a throwaway Postgres"
option. That's why patches like P2 (merged-state validation) required
a live throwaway agent to test.

**Fix**: SAM local + Postgres in docker-compose, or a simple
`node src/index.mjs --serve-http` wrapper that mimics the API Gateway
event shape. Either unblocks tests + dev.

### 1.3 No tests

Zero test coverage observed. Every deploy is a smoke test against live
Aurora. Two of the deploys we did (P1, P2) had smoke tests that were
only possible because we created+deleted throwaway agents inline.

**Fix**: Jest or Vitest covering `validateAgentData`, the
type-coercion helpers, and the route handlers (mocked pool). 100 LoC
of tests would have caught both of the patches we had to deploy.

### 1.4 No rollback mechanism

`aws lambda update-function-code` overwrites. To roll back we'd have
to reapply the previous zip manually. Lambda has versioning built in
(immutable version snapshots + aliases) but it wasn't used in any of
our deploys.

**Fix**: enable versioning on every publish. Point an alias
(`$LATEST` → `live`) at the current version. Rolling back = point the
alias at the previous version.

---

## 2. API design

### 2.1 POST and PUT for agent-drafts do the same thing

`POST /api/agent-drafts` is an upsert (ON CONFLICT DO UPDATE).
`PUT /api/agent-drafts/:id` is also an upsert for the same row.
The frontend uses POST because PUT returned 404 for fresh-agent
creation (which is the non-REST behavior we inherited).

P2 had to patch both handlers with the same merged-state validation
change because either one could hit the bug depending on which the
caller used.

**Fix**: pick one. REST convention says POST creates, PUT updates —
make PUT the upsert and remove the POST handler, or consolidate both
into a single `saveDraft(id, body)` helper that both routes call.
Either kills the duplication.

### 2.2 NUMERIC fields come back as strings

`core/lambda_client.py` has explicit `float()` casts on every
`temperature`, `tts_stability`, etc. field because the Lambda returns
them as strings (`pg`'s default behavior for NUMERIC types). Every
new NUMERIC field added to the schema requires remembering to cast
in the Python client or it becomes a type error downstream.

**Fix**: in the Lambda, coerce NUMERIC columns to `Number()` before
serializing. The pg types module supports this via `types.setTypeParser`.
One-line change, removes 6+ lines of defensive casting in
`core/lambda_client.py`.

### 2.3 JSONB vs text[] column shape mismatch

We had to migrate `stt_keywords` and `calling_window_days` from `text[]`
to `jsonb` because the Lambda's `pg` client was returning `text[]`
Postgres arrays as JSON-quoted-string values, not parsed arrays. Other
array-shaped fields (`tool_types`?, anything else?) may have the same
issue lurking.

**Fix**: audit every `text[]` column in the voice_agents and
voice_agent_drafts tables. Migrate to `jsonb` + `JSON.stringify` on
write where possible. Or use `pg-types` array parsers consistently.

### 2.4 `ON CONFLICT DO NOTHING` on writes that should be DO UPDATE

Caught once already: the original `POST /api/calls` used
`ON CONFLICT (id) DO NOTHING`, which caused batch calls to stick at
`status='pending'` because the terminal-state update never landed.
Changed to DO UPDATE. Similar pattern could exist on
`POST /api/batches`, `POST /api/batch-rows`, `POST /api/voices/add`,
etc.

**Fix**: code review every `ON CONFLICT` clause. DO NOTHING is almost
never right for a CRUD write — if you really want idempotency, return
the existing row instead of silently dropping the update.

### 2.5 No OpenAPI / schema definition

Request and response shapes are defined implicitly by whatever the
code does. The frontend + Python client both hand-roll type
definitions that have to be kept in sync. Drift is inevitable.

**Fix**: publish an OpenAPI spec. zod / Joi schemas per route can
double as runtime validation + OpenAPI generation source.

### 2.6 API Gateway vs direct Lambda invocation

`core/lambda_client.py` invokes the Lambda directly via `boto3.invoke`
because an earlier attempt at the API Gateway URL returned 403. The
API Gateway is still wired up and hit by the frontend, so that
403 was probably an auth header mismatch specific to the Python
client. Current state: two entry points (API Gateway for frontend,
direct invoke for Python EC2 backend) with different auth, monitoring,
and IAM surfaces.

**Fix**: pick one. Direct invoke is cheaper + no CORS layer, but
requires AWS SDK credentials on every caller. API Gateway is standard
and visible in the console. Unifying will reduce IAM and monitoring
surface.

---

## 3. Schema + validation

### 3.1 Hardcoded enum lists duplicated in Python + Lambda + frontend

The Lambda has `VALID_LLM_MODELS`, `VALID_TTS_PROVIDERS`,
`TTS_MODELS_BY_PROVIDER`, `VALID_STT_PROVIDERS`, `VALID_TOOL_TYPES`,
plus probably more we haven't seen. These are hand-maintained arrays.
The Python side has similar lists in `core/config_loader.py` and
tool registrations. The frontend has them too (in `agent-draft.ts`).

Every time we add a new model / provider / tool type, three files
need updating, typically over three separate PRs, and the order has
to be right or the Lambda will reject what the frontend sent.

**Fix**: single source of truth in the Lambda, exposed via
`GET /api/agent-schema`. Python + frontend both read this on startup.
Already partly in place — the frontend's schema fetcher exists — but
Python doesn't use it yet.

### 3.2 `AGENT_SCHEMA.defaults` is implicit migration

Every time we add a new field with a default (recording_enabled,
ivr_goal, enable_prompt_caching, stt_keywords as JSONB, etc.), the
Lambda gets a `defaults` update, but EXISTING rows in
`voice_agents` / `voice_agent_drafts` keep their old state. The
frontend's mapper then has to handle `undefined` by falling back to
the default.

If one of those falls out of sync (e.g. the frontend mapper forgets
to check for a new default), the UI shows `undefined` or blanks out
a control until the user manually saves.

**Fix**: run a DB migration every time a default is added, backfilling
existing rows. Tracked in the same PR that adds the default.

### 3.3 `recording_enabled` is redundant

We added `recording_enabled: true` as a schema default to unblock the
frontend mapper, but the system-wide rule is "every call is recorded,
period." The field is non-configurable. It's only there to stop the
frontend from logging a warning about a missing key.

**Fix**: remove the field. Pipeline hardcodes recording. Frontend
mapper doesn't reference it.

### 3.4 `max_tokens` default of 390 is a workaround

Default was bumped from 200 to 390 to stay under a DB CHECK constraint
of 400. Why does max_tokens have a CHECK at all? Feels like leftover
from a safety instinct that isn't enforced elsewhere (the Python
pipeline doesn't check against 400; the LLM provider doesn't care).

**Fix**: drop the CHECK constraint. Set a reasonable ceiling in the
validator (say 2048) if we want a soft guard.

### 3.5 `validateAgentData` is a long hand-rolled function

~200 lines of `if / else / push to errors` across the fields. Easy to
forget a case when adding a new field. Both patches we've deployed
touched the validator one way or another.

**Fix**: zod or Joi schema. Replace the hand-rolled if-chain with
declarative schema validation. Auto-generates OpenAPI (see §2.5).

---

## 4. Operations

### 4.1 No monitoring

No CloudWatch alarms on error rate, duration, throttles. Incidents
would be user-reported. The Lambda's logs go to CloudWatch but nobody
watches them.

**Fix**: alarms on `Errors > 1 per 5 minutes`, `Duration p95 > 2000ms`,
`Throttles > 0`. Wire to Slack or whatever your on-call is.

### 4.2 No provisioned concurrency

Every call incurs a cold-start penalty. With pg pooling it's ~1s on
the first request after idle. Frontend interactions feel sluggish
compared to warm.

**Fix**: set provisioned concurrency to 1–2 during business hours.
Cheap insurance.

### 4.3 IAM role is unknown to us

The Lambda's execution role has some combination of Aurora + Secrets
Manager + probably S3 permissions. We've never audited it. Could be
overscoped.

**Fix**: `aws iam get-role --role-name <lambda-role>` → trim to the
minimum set (SecretsManager:GetSecretValue on specific ARN, RDS data
API if used, S3 on specific bucket).

### 4.4 Secrets handling — audit needed

The Lambda probably stores the Aurora connection string in a
Secrets Manager secret, fetched on cold start. We haven't verified
it's scoped, rotated, or that the Lambda caches it correctly.

**Fix**: audit. Documentation for the new repo should list every
secret the Lambda expects to be present in SM + minimum IAM.

---

## 5. Client coupling

### 5.1 `core/lambda_client.py` has grown defensive workarounds

Cast-to-float on NUMERIC fields (§2.2), `not result` checks on a
Lambda that sometimes returns `null` (docs never say when), custom
`_is_error` that has to handle both dict and non-dict return values.
These all exist because the Lambda's return shape is inconsistent.

**Fix**: stabilize the return shape. Always return `{ ok: true, data: ... }`
or `{ ok: false, error: ... }`. Python client becomes trivial.

### 5.2 Method override path — POST instead of PUT

Python uses POST for creating AND updating agents because early PUT
attempts 404'd. Frontend does the same. Two clients doing the same
wrong thing is technically internally consistent, but it ships an
anti-pattern forward.

**Fix**: see §2.1.

---

## 6. Observed behaviors whose source we never located

These may or may not be bugs; we never traced them deep enough to be
sure. Flagging so the new owner can investigate when they have source
access.

- On `POST /api/voices/add`, the Lambda previously saved only
  `voice_id + name`, discarding `preview_url`. We patched to save the
  full ElevenLabs metadata. Unclear whether similar metadata-loss
  happens on other POSTs that wrap third-party API responses.
- Agent drafts occasionally got `tts_provider` reverted to `fish` on
  the frontend before the Fish retirement PR. We never found the
  write path responsible. Could be a stale frontend autosave; could
  be a Lambda default fallback.
- `POST /api/batches` was missing from the Lambda initially; the
  frontend 404'd. We added it. No idea why it was missing — either
  the deployed code was an older version that predated batches, or
  there was a recent regression.

---

## Known-unknowns before starting the new repo

Questions we'd need answered before diving in:

1. **Is Node 20.x confirmed as the deployed runtime?** We inferred
   from the zip layout. Check `aws lambda get-function-configuration`.
2. **Is there a build step or is `index.mjs` shipped verbatim?** We
   saw no bundler artifacts, but the deployed zip might be produced
   by a CI we don't know about.
3. **What's the current memory + timeout?** Affects provisioned
   concurrency math (§4.2).
4. **What's the Aurora connection pool size?** `pg` default is 10.
   With Lambda cold-starts this can starve.
5. **Is there a separate dev/staging Lambda?** Our patches have been
   going straight to `medcloud-voice-api` which serves live traffic.

All answerable with `aws lambda get-function --function-name ...` and
one `aws secretsmanager describe-secret` once the IAM is right.
