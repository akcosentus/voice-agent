-- Run after 001_create_calls_table.sql.
-- Batches: Excel-driven campaign tracking.

create table if not exists batches (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    agent_name text not null,
    from_number text not null,
    status text not null default 'draft',
    total_rows integer default 0,
    completed_rows integer default 0,
    failed_rows integer default 0,
    column_mapping jsonb default '{}'::jsonb,
    config jsonb default '{}'::jsonb,
    input_file_path text,
    output_file_path text,
    rows jsonb default '[]'::jsonb,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_batches_agent on batches(agent_name);
create index if not exists idx_batches_status on batches(status);
create index if not exists idx_batches_created on batches(created_at desc);

-- Link calls to batches (nullable for single ad-hoc calls).
alter table calls
    drop constraint if exists fk_calls_batch;

alter table calls
    add constraint fk_calls_batch
    foreign key (batch_id) references batches(id);
