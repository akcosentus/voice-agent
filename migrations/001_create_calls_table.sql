-- Run in Supabase SQL Editor (after creating project).
-- Calls log: one row per Twilio call / voice session.

create table if not exists calls (
    id text primary key,
    agent_name text not null,
    target_number text not null,
    direction text not null default 'outbound',
    status text not null default 'pending',
    started_at timestamptz,
    ended_at timestamptz,
    duration_secs double precision,
    case_data jsonb default '{}'::jsonb,
    transcript jsonb default '[]'::jsonb,
    recording_path text,
    post_call_analyses jsonb default '{}'::jsonb,
    error text,
    batch_id uuid,
    batch_row_index integer,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_calls_agent on calls(agent_name);
create index if not exists idx_calls_batch on calls(batch_id);
create index if not exists idx_calls_status on calls(status);
create index if not exists idx_calls_created on calls(created_at desc);
