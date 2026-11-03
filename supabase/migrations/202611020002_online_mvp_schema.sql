-- Stage 2 online MVP schema. Timestamps use timestamptz; PostgreSQL stores
-- instants in UTC and the API renders ISO-8601 UTC values.

create table if not exists public.templates (
    id uuid primary key default gen_random_uuid(),
    template_key text not null check (char_length(template_key) between 1 and 100),
    task_type text not null check (char_length(task_type) between 1 and 40),
    language text not null default 'en' check (char_length(language) between 2 and 12),
    version integer not null default 1 check (version > 0),
    title text not null check (char_length(title) between 1 and 200),
    description text not null default '' check (char_length(description) <= 500),
    suggested_steps jsonb not null default '[]'::jsonb,
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (template_key, language, version),
    constraint templates_steps_array check (
        case when jsonb_typeof(suggested_steps) = 'array'
            then jsonb_array_length(suggested_steps) <= 30
            else false
        end
    )
);

create index if not exists templates_active_lookup_idx
    on public.templates (task_type, language, template_key, version desc)
    where is_active;

create table if not exists public.user_consents (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
    consent_type text not null check (consent_type in ('ai_processing')),
    granted boolean not null,
    policy_version text not null check (char_length(policy_version) between 1 and 40),
    created_at timestamptz not null default now(),
    unique (id, user_id)
);

create index if not exists user_consents_latest_idx
    on public.user_consents (user_id, consent_type, created_at desc);

create table if not exists public.tasks (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
    title text not null check (char_length(title) between 1 and 200),
    notes text not null default '' check (char_length(notes) <= 500),
    source text not null default 'manual'
        check (source in ('manual', 'template', 'ai')),
    template_id uuid null references public.templates(id) on delete no action,
    status text not null default 'active'
        check (status in ('active', 'completed', 'archived')),
    due_at timestamptz null,
    completed_at timestamptz null,
    version integer not null default 1 check (version > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz null,
    unique (id, user_id),
    constraint tasks_completion_timestamp check (
        (status = 'completed' and completed_at is not null)
        or (status <> 'completed' and completed_at is null)
    ),
    constraint task_source_template_consistency check (
        (source = 'template' and template_id is not null)
        or (source <> 'template' and template_id is null)
    )
);

create index if not exists tasks_user_active_updated_idx
    on public.tasks (user_id, updated_at desc, id)
    where deleted_at is null;
create index if not exists tasks_user_active_count_idx
    on public.tasks (user_id)
    where status = 'active' and deleted_at is null;
create index if not exists tasks_user_due_idx
    on public.tasks (user_id, due_at, id)
    where deleted_at is null and due_at is not null;

create table if not exists public.steps (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null,
    user_id uuid not null references public.users(id) on delete cascade,
    text text not null check (char_length(text) between 1 and 280),
    planned_minutes smallint not null check (planned_minutes between 1 and 240),
    actual_minutes smallint null check (actual_minutes between 0 and 1440),
    position smallint not null check (position between 0 and 29),
    status text not null default 'pending'
        check (status in ('pending', 'active', 'completed')),
    started_at timestamptz null,
    completed_at timestamptz null,
    version integer not null default 1 check (version > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz null,
    unique (id, user_id),
    constraint steps_task_owner_fk
        foreign key (task_id, user_id)
        references public.tasks(id, user_id)
        on delete cascade,
    constraint steps_status_timestamps check (
        (status = 'active' and started_at is not null and completed_at is null)
        or (status = 'completed' and completed_at is not null)
        or (status = 'pending' and completed_at is null)
    )
);

create index if not exists steps_task_order_idx
    on public.steps (task_id, user_id, position, id)
    where deleted_at is null;
create index if not exists steps_user_updated_idx
    on public.steps (user_id, updated_at desc, id)
    where deleted_at is null;

create table if not exists public.reward_events (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
    task_id uuid null,
    step_id uuid null,
    event_type text not null check (event_type in (
        'step_started', 'step_completed', 'task_completed',
        'streak_extended', 'streak_freeze_used', 'streak_ended', 'adjustment'
    )),
    points_delta integer not null default 0 check (points_delta between -100000 and 100000),
    streak_delta integer not null default 0 check (streak_delta between -10000 and 10000),
    event_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint reward_events_data_object check (
        jsonb_typeof(event_data) = 'object'
        and pg_catalog.pg_column_size(event_data) <= 4096
        and event_data - array['rule_version', 'freezes_after', 'streak_after', 'reason_code'] = '{}'::jsonb
    ),
    constraint reward_events_task_owner_fk
        foreign key (task_id, user_id)
        references public.tasks(id, user_id)
        on delete no action,
    constraint reward_events_step_owner_fk
        foreign key (step_id, user_id)
        references public.steps(id, user_id)
        on delete no action
);

create index if not exists reward_events_user_time_idx
    on public.reward_events (user_id, created_at desc, id);
create index if not exists reward_events_user_type_time_idx
    on public.reward_events (user_id, event_type, created_at desc);

create table if not exists public.ai_breakdown_cache (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
    cache_hash text not null check (cache_hash ~ '^[a-f0-9]{64}$'),
    prompt_version text not null check (char_length(prompt_version) between 1 and 40),
    result jsonb not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    unique (user_id, cache_hash, prompt_version),
    constraint ai_cache_result_object check (jsonb_typeof(result) = 'object'),
    constraint ai_cache_expiry check (expires_at > created_at)
);

create index if not exists ai_breakdown_cache_expiry_idx
    on public.ai_breakdown_cache (expires_at);

create table if not exists public.ai_usage_daily (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
    usage_date date not null,
    generations integer not null default 0 check (generations between 0 and 10000),
    updated_at timestamptz not null default now(),
    unique (user_id, usage_date)
);

create table if not exists public.api_idempotency (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
    idempotency_key text not null check (idempotency_key ~ '^[A-Za-z0-9._:-]{8,128}$'),
    request_hash text not null check (request_hash ~ '^[a-f0-9]{64}$'),
    state text not null check (state in ('in_progress', 'completed')),
    response_status smallint null check (response_status between 100 and 599),
    response_body jsonb null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    lease_token uuid not null,
    lease_expires_at timestamptz not null,
    expires_at timestamptz not null,
    unique (user_id, idempotency_key),
    constraint api_idempotency_response_state check (
        (state = 'in_progress' and response_status is null and response_body is null)
        or (
            state = 'completed'
            and response_status is not null
            and (
                (response_status = 204 and response_body is null)
                or (response_status <> 204 and response_body is not null)
            )
        )
    ),
    constraint api_idempotency_expiry check (
        lease_expires_at > created_at and expires_at > created_at
    )
);

create index if not exists api_idempotency_expiry_idx
    on public.api_idempotency (expires_at);

create table if not exists public.api_rate_limit_windows (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
    bucket text not null check (bucket ~ '^[a-zA-Z0-9._:-]{1,80}$'),
    window_start timestamptz not null,
    request_count integer not null check (request_count between 1 and 1000000),
    updated_at timestamptz not null default now(),
    expires_at timestamptz not null,
    unique (user_id, bucket, window_start),
    constraint api_rate_window_expiry check (expires_at > window_start)
);

create index if not exists api_rate_limit_windows_expiry_idx
    on public.api_rate_limit_windows (expires_at);

-- RLS is enabled even for public templates; their only public policy exposes
-- active, reviewed rows. No placeholder template rows are seeded here.
grant usage on schema public to anon, authenticated;

alter table public.templates enable row level security;
alter table public.user_consents enable row level security;
alter table public.tasks enable row level security;
alter table public.steps enable row level security;
alter table public.reward_events enable row level security;
alter table public.ai_breakdown_cache enable row level security;
alter table public.ai_usage_daily enable row level security;
alter table public.api_idempotency enable row level security;
alter table public.api_rate_limit_windows enable row level security;

drop policy if exists templates_read_active on public.templates;
create policy templates_read_active on public.templates
    for select to anon, authenticated using (is_active);

drop policy if exists user_consents_select_self on public.user_consents;
create policy user_consents_select_self on public.user_consents
    for select to authenticated using (user_id = (select auth.uid()));
drop policy if exists user_consents_insert_self on public.user_consents;
create policy user_consents_insert_self on public.user_consents
    for insert to authenticated with check (user_id = (select auth.uid()));

drop policy if exists tasks_all_self on public.tasks;
create policy tasks_all_self on public.tasks
    for all to authenticated
    using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));

create or replace function public.enforce_active_task_limit()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    active_count integer;
begin
    if new.status = 'active' and new.deleted_at is null then
        perform pg_catalog.pg_advisory_xact_lock(48107, pg_catalog.hashtext(new.user_id::text));
        select count(*) into active_count
        from public.tasks as task
        where task.user_id = new.user_id
          and task.status = 'active'
          and task.deleted_at is null
          and task.id is distinct from new.id;
        if active_count >= 500 then
            raise exception using errcode = '23514', message = 'active task limit reached';
        end if;
    end if;
    return new;
end;
$function$;

drop trigger if exists tasks_active_limit on public.tasks;
create trigger tasks_active_limit
    before insert or update of user_id, status, deleted_at on public.tasks
    for each row execute function public.enforce_active_task_limit();

drop policy if exists steps_all_self on public.steps;
create policy steps_all_self on public.steps
    for all to authenticated
    using (user_id = (select auth.uid()))
    with check (
        user_id = (select auth.uid())
        and exists (
            select 1 from public.tasks as parent
            where parent.id = task_id
              and parent.user_id = (select auth.uid())
        )
    );

create or replace function public.enforce_active_step_positions()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    active_count integer;
    distinct_positions integer;
begin
    perform pg_catalog.pg_advisory_xact_lock(
        48108,
        pg_catalog.hashtext(new.task_id::text)
    );

    select count(*), count(distinct position)
    into active_count, distinct_positions
    from public.steps as step
    where step.task_id = new.task_id
      and step.user_id = new.user_id
      and step.deleted_at is null;

    if active_count > 30 or active_count <> distinct_positions then
        raise exception using errcode = '23514', message = 'active step order is invalid';
    end if;
    return null;
end;
$function$;

drop trigger if exists steps_active_positions_valid on public.steps;
create constraint trigger steps_active_positions_valid
    after insert or update on public.steps
    deferrable initially deferred
    for each row execute function public.enforce_active_step_positions();

drop policy if exists reward_events_select_self on public.reward_events;
create policy reward_events_select_self on public.reward_events
    for select to authenticated using (user_id = (select auth.uid()));
drop policy if exists reward_events_insert_self on public.reward_events;
create policy reward_events_insert_self on public.reward_events
    for insert to authenticated with check (user_id = (select auth.uid()));

drop policy if exists reward_state_update_self on public.reward_state;
create policy reward_state_update_self on public.reward_state
    for update to authenticated
    using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));

drop policy if exists ai_breakdown_cache_all_self on public.ai_breakdown_cache;
create policy ai_breakdown_cache_all_self on public.ai_breakdown_cache
    for all to authenticated
    using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));
drop policy if exists ai_usage_daily_select_self on public.ai_usage_daily;
create policy ai_usage_daily_select_self on public.ai_usage_daily
    for select to authenticated using (user_id = (select auth.uid()));

drop policy if exists api_idempotency_self on public.api_idempotency;
create policy api_idempotency_self on public.api_idempotency
    for all to authenticated
    using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));
drop policy if exists api_rate_limit_self on public.api_rate_limit_windows;
create policy api_rate_limit_self on public.api_rate_limit_windows
    for all to authenticated
    using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));

-- The API does not expose direct table write access for RPC-owned counters/results.
revoke all on table public.templates, public.user_consents, public.tasks, public.steps,
    public.reward_events, public.ai_breakdown_cache, public.ai_usage_daily,
    public.api_idempotency, public.api_rate_limit_windows from anon, authenticated;

grant select on table public.templates to anon, authenticated;
grant select, insert on table public.user_consents to authenticated;
grant select on table public.tasks, public.steps to authenticated;
grant insert (user_id, title, notes, source, template_id, due_at)
    on table public.tasks to authenticated;
grant update (title, notes, source, template_id, due_at, status, completed_at,
    version, updated_at, deleted_at) on table public.tasks to authenticated;
grant insert (task_id, user_id, text, planned_minutes, position)
    on table public.steps to authenticated;
grant update (text, planned_minutes, actual_minutes, position, status, started_at,
    completed_at, version, updated_at, deleted_at) on table public.steps to authenticated;
grant select, insert on table public.reward_events to authenticated;
grant update (points_total, plant_stage, current_streak, best_streak,
    freezes_held, streak_status, last_active_local_date, updated_at)
    on table public.reward_state to authenticated;
grant select on table public.ai_breakdown_cache to authenticated;
grant select on table public.ai_usage_daily to authenticated;
grant select, insert, update, delete on table
    public.api_idempotency, public.api_rate_limit_windows to authenticated;

-- RLS scopes these system rows to auth.uid(). A trigger additionally requires
-- a transaction-local flag set only inside the invoker RPC before mutation, so
-- direct PostgREST table writes cannot reset counters or forge replay entries.
create or replace function public.require_protection_rpc_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    if tg_op = 'DELETE' and pg_catalog.pg_trigger_depth() > 1 then
        return old;
    end if;
    if pg_catalog.current_setting(tg_argv[0], true) is distinct from 'on' then
        raise exception using errcode = '42501', message = 'use the protection RPC';
    end if;
    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end;
$function$;

drop trigger if exists api_idempotency_rpc_mutation_only on public.api_idempotency;
create trigger api_idempotency_rpc_mutation_only
    before insert or update or delete on public.api_idempotency
    for each row execute function public.require_protection_rpc_mutation(
        'kickoff.idempotency_write'
    );
drop trigger if exists api_rate_limit_rpc_mutation_only on public.api_rate_limit_windows;
create trigger api_rate_limit_rpc_mutation_only
    before insert or update or delete on public.api_rate_limit_windows
    for each row execute function public.require_protection_rpc_mutation(
        'kickoff.rate_limit_write'
    );

drop trigger if exists tasks_lifecycle_rpc_only on public.tasks;
create trigger tasks_lifecycle_rpc_only
    before update of status, completed_at, deleted_at on public.tasks
    for each row execute function public.require_protection_rpc_mutation(
        'kickoff.task_lifecycle_write'
    );
drop trigger if exists steps_lifecycle_rpc_only on public.steps;
create trigger steps_lifecycle_rpc_only
    before update of status, started_at, completed_at, actual_minutes, deleted_at on public.steps
    for each row execute function public.require_protection_rpc_mutation(
        'kickoff.step_lifecycle_write'
    );
drop trigger if exists reward_events_rpc_insert_only on public.reward_events;
create trigger reward_events_rpc_insert_only
    before insert on public.reward_events
    for each row execute function public.require_protection_rpc_mutation(
        'kickoff.reward_event_write'
    );
drop trigger if exists reward_state_rpc_update_only on public.reward_state;
create trigger reward_state_rpc_update_only
    before update on public.reward_state
    for each row execute function public.require_protection_rpc_mutation(
        'kickoff.reward_state_write'
    );
revoke all on function public.require_protection_rpc_mutation() from public, anon, authenticated;

create or replace function public.reject_reward_event_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    -- Allow referential cascades on full account deletion while rejecting any
    -- direct update/delete of an append-only reward event.
    if pg_catalog.pg_trigger_depth() > 1 then
        return old;
    end if;
    raise exception using errcode = '42501', message = 'reward events are append only';
end;
$function$;

drop trigger if exists reward_events_append_only on public.reward_events;
create trigger reward_events_append_only
    before update or delete on public.reward_events
    for each row execute function public.reject_reward_event_mutation();
revoke all on function public.reject_reward_event_mutation() from public, anon, authenticated;

-- These functions are SECURITY INVOKER: they run as authenticated and remain
-- subject to the user-owned RLS policies above. Each derives ownership from
-- auth.uid(), uses a fixed empty search_path, and accepts no caller user ID.
create or replace function public.consume_api_rate_limit(
    p_bucket text,
    p_limit integer,
    p_window_seconds integer
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    instant timestamptz := clock_timestamp();
    bucket_start timestamptz;
    current_count integer;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_bucket is null or p_bucket !~ '^[a-zA-Z0-9._:-]{1,80}$'
       or p_limit is null or p_limit < 1 or p_limit > 100000
       or p_window_seconds is null or p_window_seconds < 1 or p_window_seconds > 86400 then
        raise exception using errcode = '22023', message = 'invalid rate limit parameters';
    end if;

    perform pg_catalog.set_config('kickoff.rate_limit_write', 'on', true);

    bucket_start := pg_catalog.to_timestamp(
        (
            pg_catalog.floor(extract(epoch from instant) / p_window_seconds)
            * p_window_seconds
        )::double precision
    );

    delete from public.api_rate_limit_windows
    where user_id = caller_id and expires_at <= instant;

    insert into public.api_rate_limit_windows as windows (
        user_id, bucket, window_start, request_count, updated_at, expires_at
    ) values (
        caller_id, p_bucket, bucket_start, 1, instant,
        bucket_start + pg_catalog.make_interval(secs => p_window_seconds::double precision) + interval '1 day'
    )
    on conflict (user_id, bucket, window_start)
    do update set
        request_count = windows.request_count + 1,
        updated_at = excluded.updated_at
    returning request_count into current_count;

    return pg_catalog.jsonb_build_object(
        'user_id', caller_id,
        'allowed', current_count <= p_limit,
        'remaining', greatest(p_limit - current_count, 0),
        'retry_after_seconds', greatest(
            1,
            pg_catalog.ceil(extract(epoch from (
                bucket_start + pg_catalog.make_interval(secs => p_window_seconds::double precision)
                - instant
            )))::integer
        )
    );
end;
$function$;

create or replace function public.begin_api_idempotency(p_key text, p_request_hash text)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    instant timestamptz := clock_timestamp();
    lease_id uuid := gen_random_uuid();
    inserted_count integer;
    saved public.api_idempotency%rowtype;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_key is null or p_key !~ '^[A-Za-z0-9._:-]{8,128}$'
       or p_request_hash is null or p_request_hash !~ '^[a-f0-9]{64}$' then
        raise exception using errcode = '22023', message = 'invalid idempotency parameters';
    end if;

    perform pg_catalog.set_config('kickoff.idempotency_write', 'on', true);

    delete from public.api_idempotency
    where user_id = caller_id and expires_at <= instant;

    insert into public.api_idempotency (
        user_id, idempotency_key, request_hash, state,
        created_at, updated_at, lease_token, lease_expires_at, expires_at
    ) values (
        caller_id, p_key, p_request_hash, 'in_progress',
        instant, instant, lease_id, instant + interval '5 minutes', instant + interval '24 hours'
    ) on conflict (user_id, idempotency_key) do nothing;
    get diagnostics inserted_count = row_count;

    if inserted_count = 1 then
        return pg_catalog.jsonb_build_object('state', 'new', 'lease_token', lease_id);
    end if;

    select * into saved
    from public.api_idempotency as idem
    where idem.user_id = caller_id
      and idem.idempotency_key = p_key
    for update;

    if not found then
        raise exception using errcode = '40001', message = 'idempotency record unavailable';
    end if;

    if saved.expires_at <= instant then
        lease_id := gen_random_uuid();
        update public.api_idempotency
        set request_hash = p_request_hash,
            state = 'in_progress',
            response_status = null,
            response_body = null,
            created_at = instant,
            updated_at = instant,
            lease_token = lease_id,
            lease_expires_at = instant + interval '5 minutes',
            expires_at = instant + interval '24 hours'
        where user_id = caller_id and idempotency_key = p_key;
        return pg_catalog.jsonb_build_object('state', 'new', 'lease_token', lease_id);
    end if;

    if saved.request_hash <> p_request_hash then
        return pg_catalog.jsonb_build_object('state', 'key_reused');
    end if;

    if saved.state = 'completed' then
        return pg_catalog.jsonb_build_object(
            'state', 'replay',
            'response_status', saved.response_status,
            'response_body', saved.response_body
        );
    end if;

    if saved.lease_expires_at <= instant then
        lease_id := gen_random_uuid();
        update public.api_idempotency
        set lease_token = lease_id,
            lease_expires_at = instant + interval '5 minutes',
            updated_at = instant
        where user_id = caller_id and idempotency_key = p_key;
        return pg_catalog.jsonb_build_object('state', 'new', 'lease_token', lease_id);
    end if;

    return pg_catalog.jsonb_build_object('state', 'in_progress');
end;
$function$;

create or replace function public.complete_api_idempotency(
    p_key text,
    p_request_hash text,
    p_lease_token uuid,
    p_response_status integer,
    p_response_body jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    changed_count integer;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    perform pg_catalog.set_config('kickoff.idempotency_write', 'on', true);
    if p_key is null or p_key !~ '^[A-Za-z0-9._:-]{8,128}$'
       or p_request_hash is null or p_request_hash !~ '^[a-f0-9]{64}$'
       or p_lease_token is null
       or p_response_status is null or p_response_status < 100 or p_response_status > 599
       or (p_response_status = 204 and p_response_body is not null)
       or (p_response_status <> 204 and p_response_body is null)
       or (p_response_body is not null and pg_catalog.pg_column_size(p_response_body) > 262144) then
        raise exception using errcode = '22023', message = 'invalid idempotency response';
    end if;

    update public.api_idempotency
    set state = 'completed',
        response_status = p_response_status::smallint,
        response_body = p_response_body,
        updated_at = clock_timestamp(),
        lease_expires_at = clock_timestamp(),
        expires_at = clock_timestamp() + interval '24 hours'
    where user_id = caller_id
      and idempotency_key = p_key
      and request_hash = p_request_hash
      and lease_token = p_lease_token
      and state = 'in_progress';
    get diagnostics changed_count = row_count;
    if changed_count <> 1 then
        raise exception using errcode = '40001', message = 'idempotency reservation unavailable';
    end if;
    return pg_catalog.jsonb_build_object('stored', true);
end;
$function$;

create or replace function public.release_api_idempotency(
    p_key text,
    p_request_hash text,
    p_lease_token uuid
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    removed_count integer;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    perform pg_catalog.set_config('kickoff.idempotency_write', 'on', true);
    if p_key is null or p_key !~ '^[A-Za-z0-9._:-]{8,128}$'
       or p_request_hash is null or p_request_hash !~ '^[a-f0-9]{64}$'
       or p_lease_token is null then
        raise exception using errcode = '22023', message = 'invalid idempotency parameters';
    end if;
    delete from public.api_idempotency
    where user_id = caller_id
      and idempotency_key = p_key
      and request_hash = p_request_hash
      and lease_token = p_lease_token
      and state = 'in_progress';
    get diagnostics removed_count = row_count;
    return pg_catalog.jsonb_build_object('released', removed_count = 1);
end;
$function$;

revoke all on function public.consume_api_rate_limit(text, integer, integer) from public, anon;
revoke all on function public.begin_api_idempotency(text, text) from public, anon;
revoke all on function public.complete_api_idempotency(text, text, uuid, integer, jsonb) from public, anon;
revoke all on function public.release_api_idempotency(text, text, uuid) from public, anon;
grant execute on function public.consume_api_rate_limit(text, integer, integer) to authenticated;
grant execute on function public.begin_api_idempotency(text, text) to authenticated;
grant execute on function public.complete_api_idempotency(text, text, uuid, integer, jsonb) to authenticated;
grant execute on function public.release_api_idempotency(text, text, uuid) to authenticated;

