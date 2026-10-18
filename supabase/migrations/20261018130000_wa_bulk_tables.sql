-- Bulk WhatsApp sending: settings, jobs and per-recipient state. Server-side (service role) access only.

create table if not exists public.wa_settings (
    id int primary key default 1 check (id = 1),
    daily_limit int not null default 2000,
    batch_size int not null default 50,
    lease_owner text,
    lease_until timestamptz,
    updated_at timestamptz not null default now()
);
insert into public.wa_settings (id) values (1) on conflict (id) do nothing;

create table if not exists public.wa_jobs (
    id uuid primary key default gen_random_uuid(),
    campaign_id uuid,
    name text not null,
    -- running | paused | completed | completed_with_failures | cancelled
    status text not null default 'running',
    template jsonb not null,
    header_image_url text,
    last_error text,
    created_at timestamptz not null default now(),
    finished_at timestamptz,
    updated_at timestamptz not null default now()
);

create table if not exists public.wa_job_recipients (
    id bigint generated always as identity primary key,
    job_id uuid not null references public.wa_jobs (id) on delete cascade,
    voter_id uuid,
    voter_name text,
    phone text not null,
    variables jsonb not null default '[]'::jsonb,
    -- pending | sending | submitted | delivered | read | failed | skipped
    status text not null default 'pending',
    -- failed: daily_limit | rejected | delivery_failed ; skipped: invalid_number | duplicate_number | missing_variable | cancelled
    fail_reason text,
    error text,
    provider_ref text,
    sent_at timestamptz,
    delivered_at timestamptz,
    read_at timestamptz,
    updated_at timestamptz not null default now(),
    unique (job_id, voter_id)
);

create index if not exists wa_job_recipients_job_status_idx on public.wa_job_recipients (job_id, status);
create index if not exists wa_job_recipients_sent_at_idx on public.wa_job_recipients (sent_at) where sent_at is not null;

create or replace function public.wa_job_stats(p_job uuid)
returns jsonb
language sql
stable
as $$
    select coalesce(jsonb_object_agg(k, c), '{}'::jsonb)
    from (
        select case when status in ('failed', 'skipped') then status || ':' || coalesce(fail_reason, 'other') else status end as k,
               count(*) as c
        from public.wa_job_recipients
        where job_id = p_job
        group by 1
    ) t
$$;

alter table public.wa_settings enable row level security;
alter table public.wa_jobs enable row level security;
alter table public.wa_job_recipients enable row level security;

revoke all on public.wa_settings, public.wa_jobs, public.wa_job_recipients from anon, authenticated;
revoke execute on function public.wa_job_stats(uuid) from anon, authenticated, public;
grant execute on function public.wa_job_stats(uuid) to service_role;

notify pgrst, 'reload schema';
