# Supabase migrations

Run these **in order** in the Supabase Dashboard → SQL Editor.

1. `001_create_calls_table.sql`
2. `002_create_batches_table.sql`
3. `003_add_enable_prompt_caching.sql`

The `agents` and `agent_versions` tables are created via the Supabase dashboard or a separate migration managed outside this folder.

## Storage buckets (manual)

Create two buckets in **Storage**:

| Bucket        | Use                          |
|---------------|------------------------------|
| `recordings`  | Per-call WAV files           |
| `batch-files` | Uploaded Excel + result Excel |

Set policies as needed for your environment (service role bypasses RLS for server-side uploads).
