-- Batch scheduling columns, atomic counter RPC, and rows_data for edited rows.

-- Scheduling and concurrency columns
ALTER TABLE batches ADD COLUMN IF NOT EXISTS concurrency integer DEFAULT 5;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS timezone text DEFAULT 'America/New_York';
ALTER TABLE batches ADD COLUMN IF NOT EXISTS calling_window_start time DEFAULT '09:00:00';
ALTER TABLE batches ADD COLUMN IF NOT EXISTS calling_window_end time DEFAULT '17:00:00';
ALTER TABLE batches ADD COLUMN IF NOT EXISTS calling_window_days jsonb DEFAULT '["Mon","Tue","Wed","Thu","Fri"]'::jsonb;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS paused_by_user boolean DEFAULT false;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS last_dialed_at timestamptz;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS start_date date;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS schedule_mode text DEFAULT 'now';

-- Edited rows storage (from frontend batch editor)
ALTER TABLE batches ADD COLUMN IF NOT EXISTS rows_data jsonb;

-- Atomic counter increment to avoid read-then-write race conditions.
-- Usage: SELECT increment_counter('batch-uuid', 'completed_rows', 1);
CREATE OR REPLACE FUNCTION increment_counter(
    batch_uuid uuid,
    field_name text,
    amount integer DEFAULT 1
) RETURNS void AS $$
BEGIN
    IF field_name = 'completed_rows' THEN
        UPDATE batches SET completed_rows = COALESCE(completed_rows, 0) + amount,
                           updated_at = now()
        WHERE id = batch_uuid;
    ELSIF field_name = 'failed_rows' THEN
        UPDATE batches SET failed_rows = COALESCE(failed_rows, 0) + amount,
                           updated_at = now()
        WHERE id = batch_uuid;
    ELSE
        RAISE EXCEPTION 'Unknown counter field: %', field_name;
    END IF;
END;
$$ LANGUAGE plpgsql;
