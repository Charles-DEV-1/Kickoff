-- Stage 6 AI controls. User cache/allowance operations remain scoped to auth.uid().
-- The cross-user provider budget contains no user data and is callable only by
-- the backend's server-side Supabase secret/service_role key.

create table if not exists public.ai_provider_usage_daily (
    provider text not null check (provider ~ '^[a-z0-9_-]{1,40}$'),
    usage_date date not null,
    requests integer not null default 0 check (requests between 0 and 1000000),
    updated_at timestamptz not null default now(),
    primary key (provider, usage_date)
);

create table if not exists public.ai_usage_reservations (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
    usage_date date not null,
    expires_at timestamptz not null,
    committed_at timestamptz null,
    created_at timestamptz not null default now(),
    check (expires_at > created_at),
    check (committed_at is null or committed_at >= created_at)
);
create index if not exists ai_usage_reservations_active_idx
    on public.ai_usage_reservations (user_id, usage_date, expires_at)
    where committed_at is null;

alter table public.ai_breakdown_cache enable row level security;
alter table public.ai_usage_daily enable row level security;
alter table public.ai_provider_usage_daily enable row level security;
alter table public.ai_usage_reservations enable row level security;

drop policy if exists ai_cache_insert_self on public.ai_breakdown_cache;
create policy ai_cache_insert_self on public.ai_breakdown_cache
    for insert to authenticated with check (user_id = (select auth.uid()));
drop policy if exists ai_cache_update_self on public.ai_breakdown_cache;
create policy ai_cache_update_self on public.ai_breakdown_cache
    for update to authenticated using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));
drop policy if exists ai_cache_delete_self on public.ai_breakdown_cache;
create policy ai_cache_delete_self on public.ai_breakdown_cache
    for delete to authenticated using (user_id = (select auth.uid()));

drop policy if exists ai_usage_insert_self on public.ai_usage_daily;
create policy ai_usage_insert_self on public.ai_usage_daily
    for insert to authenticated with check (user_id = (select auth.uid()));
drop policy if exists ai_usage_update_self on public.ai_usage_daily;
create policy ai_usage_update_self on public.ai_usage_daily
    for update to authenticated using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));
drop policy if exists ai_usage_reservation_self on public.ai_usage_reservations;
create policy ai_usage_reservation_self on public.ai_usage_reservations
    for all to authenticated using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));

revoke all on table public.ai_breakdown_cache, public.ai_usage_daily,
    public.ai_provider_usage_daily, public.ai_usage_reservations from anon, authenticated;
grant select, insert, update, delete on table public.ai_breakdown_cache to authenticated;
grant select, insert, update on table public.ai_usage_daily to authenticated;
grant select, insert, update, delete on table public.ai_usage_reservations to authenticated;
grant select, insert, update, delete on table public.ai_provider_usage_daily to service_role;
grant usage on schema public to service_role;

-- Direct client writes cannot forge cache rows or reset daily allowances.
drop trigger if exists ai_breakdown_cache_rpc_only on public.ai_breakdown_cache;
create trigger ai_breakdown_cache_rpc_only
    before insert or update or delete on public.ai_breakdown_cache
    for each row execute function public.require_protection_rpc_mutation('kickoff.ai_write');
drop trigger if exists ai_usage_daily_rpc_only on public.ai_usage_daily;
create trigger ai_usage_daily_rpc_only
    before insert or update or delete on public.ai_usage_daily
    for each row execute function public.require_protection_rpc_mutation('kickoff.ai_write');
drop trigger if exists ai_provider_usage_daily_rpc_only on public.ai_provider_usage_daily;
create trigger ai_provider_usage_daily_rpc_only
    before insert or update or delete on public.ai_provider_usage_daily
    for each row execute function public.require_protection_rpc_mutation('kickoff.ai_budget_write');
drop trigger if exists ai_usage_reservations_rpc_only on public.ai_usage_reservations;
create trigger ai_usage_reservations_rpc_only
    before insert or update or delete on public.ai_usage_reservations
    for each row execute function public.require_protection_rpc_mutation('kickoff.ai_write');

create or replace function public.get_ai_breakdown_cache(
    p_cache_hash text,
    p_prompt_version text
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    cached_result jsonb;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_cache_hash is null or p_cache_hash !~ '^[a-f0-9]{64}$'
       or p_prompt_version is null or char_length(p_prompt_version) not between 1 and 40 then
        raise exception using errcode = '22023', message = 'invalid AI cache key';
    end if;
    select c.result into cached_result
    from public.ai_breakdown_cache as c
    where c.user_id = caller_id
      and c.cache_hash = p_cache_hash
      and c.prompt_version = p_prompt_version
      and c.expires_at > pg_catalog.clock_timestamp();
    return pg_catalog.jsonb_build_object(
        'hit', cached_result is not null,
        'result', cached_result
    );
end;
$function$;

create or replace function public.save_ai_breakdown_cache(
    p_cache_hash text,
    p_prompt_version text,
    p_result jsonb,
    p_ttl_seconds integer
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    instant timestamptz := pg_catalog.clock_timestamp();
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_cache_hash is null or p_cache_hash !~ '^[a-f0-9]{64}$'
       or p_prompt_version is null or char_length(p_prompt_version) not between 1 and 40
       or p_result is null or pg_catalog.jsonb_typeof(p_result) <> 'object'
       or pg_catalog.octet_length(p_result::text) > 32768
       or p_ttl_seconds is null or p_ttl_seconds < 60 or p_ttl_seconds > 604800 then
        raise exception using errcode = '22023', message = 'invalid AI cache entry';
    end if;
    perform pg_catalog.set_config('kickoff.ai_write', 'on', true);
    delete from public.ai_breakdown_cache as c
    where c.user_id = caller_id and c.expires_at <= instant;
    insert into public.ai_breakdown_cache (
        user_id, cache_hash, prompt_version, result, created_at, expires_at
    ) values (
        caller_id, p_cache_hash, p_prompt_version, p_result, instant,
        instant + pg_catalog.make_interval(secs => p_ttl_seconds)
    )
    on conflict (user_id, cache_hash, prompt_version) do update
        set result = excluded.result,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at;
    return pg_catalog.jsonb_build_object('saved', true);
end;
$function$;

create or replace function public.read_ai_daily_usage()
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    today_utc date := (pg_catalog.clock_timestamp() at time zone 'UTC')::date;
    used_count integer := 0;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    select u.generations into used_count
    from public.ai_usage_daily as u
    where u.user_id = caller_id and u.usage_date = today_utc;
    return pg_catalog.jsonb_build_object('used', coalesce(used_count, 0), 'usage_date', today_utc);
end;
$function$;

create or replace function public.reserve_ai_daily_usage(p_daily_limit integer)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    today_utc date := (pg_catalog.clock_timestamp() at time zone 'UTC')::date;
    current_count integer := 0;
    pending_count integer := 0;
    reservation_id uuid;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_daily_limit is null or p_daily_limit < 1 or p_daily_limit > 10000 then
        raise exception using errcode = '22023', message = 'invalid AI allowance';
    end if;
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext(caller_id::text));
    perform pg_catalog.set_config('kickoff.ai_write', 'on', true);
    delete from public.ai_usage_reservations as r
    where r.user_id = caller_id
      and (r.expires_at <= pg_catalog.clock_timestamp()
           or r.usage_date < today_utc - 90);
    select u.generations into current_count
    from public.ai_usage_daily as u
    where u.user_id = caller_id and u.usage_date = today_utc;
    select count(*)::integer into pending_count
    from public.ai_usage_reservations as r
    where r.user_id = caller_id and r.usage_date = today_utc
      and r.committed_at is null and r.expires_at > pg_catalog.clock_timestamp();
    if coalesce(current_count, 0) + pending_count < p_daily_limit then
        insert into public.ai_usage_reservations (user_id, usage_date, expires_at)
        values (caller_id, today_utc, pg_catalog.clock_timestamp() + interval '30 seconds')
        returning id into reservation_id;
        return pg_catalog.jsonb_build_object(
            'allowed', true,
            'used', coalesce(current_count, 0),
            'usage_date', today_utc,
            'reservation_id', reservation_id
        );
    end if;
    return pg_catalog.jsonb_build_object(
        'allowed', false, 'used', coalesce(current_count, 0), 'usage_date', today_utc
    );
end;
$function$;

create or replace function public.commit_ai_daily_usage(p_reservation_id uuid)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    reservation_date date;
    committed_reservation uuid;
    new_count integer;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_reservation_id is null then
        raise exception using errcode = '22023', message = 'invalid AI reservation';
    end if;
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext(caller_id::text));
    perform pg_catalog.set_config('kickoff.ai_write', 'on', true);
    update public.ai_usage_reservations as r
    set committed_at = pg_catalog.clock_timestamp()
    where r.id = p_reservation_id
      and r.user_id = caller_id
      and r.usage_date = (pg_catalog.clock_timestamp() at time zone 'UTC')::date
      and r.expires_at > pg_catalog.clock_timestamp()
      and r.committed_at is null
    returning r.id, r.usage_date into committed_reservation, reservation_date;
    if committed_reservation is not null then
        insert into public.ai_usage_daily (user_id, usage_date, generations)
        values (caller_id, reservation_date, 1)
        on conflict (user_id, usage_date) do update
            set generations = public.ai_usage_daily.generations + 1,
                updated_at = pg_catalog.clock_timestamp()
        returning generations into new_count;
    else
        select u.generations into new_count
        from public.ai_usage_daily as u
        where u.user_id = caller_id
          and u.usage_date = (pg_catalog.clock_timestamp() at time zone 'UTC')::date;
    end if;
    return pg_catalog.jsonb_build_object(
        'committed', committed_reservation is not null,
        'used', coalesce(new_count, 0),
        'usage_date', coalesce(reservation_date, (pg_catalog.clock_timestamp() at time zone 'UTC')::date)
    );
end;
$function$;

create or replace function public.reserve_ai_provider_budget(
    p_provider text,
    p_daily_limit integer,
    p_usage_date date
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    current_count integer;
begin
    if current_user <> 'service_role' then
        raise exception using errcode = '42501', message = 'server authorization required';
    end if;
    if p_provider is null or p_provider !~ '^[a-z0-9_-]{1,40}$'
       or p_daily_limit is null or p_daily_limit < 1 or p_daily_limit > 1000000
       or p_usage_date is distinct from (pg_catalog.clock_timestamp() at time zone 'UTC')::date then
        raise exception using errcode = '22023', message = 'invalid provider budget';
    end if;
    perform pg_catalog.set_config('kickoff.ai_budget_write', 'on', true);
    delete from public.ai_provider_usage_daily
    where provider = p_provider and usage_date < p_usage_date - 90;
    insert into public.ai_provider_usage_daily (provider, usage_date, requests)
    values (p_provider, p_usage_date, 1)
    on conflict (provider, usage_date) do update
        set requests = public.ai_provider_usage_daily.requests + 1,
            updated_at = pg_catalog.clock_timestamp()
        where public.ai_provider_usage_daily.requests < p_daily_limit
    returning requests into current_count;
    if found then
        return pg_catalog.jsonb_build_object('allowed', true, 'used', current_count);
    end if;
    select b.requests into current_count
    from public.ai_provider_usage_daily as b
    where b.provider = p_provider and b.usage_date = p_usage_date;
    return pg_catalog.jsonb_build_object('allowed', false, 'used', coalesce(current_count, 0));
end;
$function$;

revoke all on function public.get_ai_breakdown_cache(text, text) from public, anon;
revoke all on function public.save_ai_breakdown_cache(text, text, jsonb, integer) from public, anon;
revoke all on function public.read_ai_daily_usage() from public, anon;
revoke all on function public.reserve_ai_daily_usage(integer) from public, anon;
revoke all on function public.reserve_ai_provider_budget(text, integer, date) from public, anon, authenticated;
grant execute on function public.get_ai_breakdown_cache(text, text) to authenticated;
grant execute on function public.save_ai_breakdown_cache(text, text, jsonb, integer) to authenticated;
grant execute on function public.read_ai_daily_usage() to authenticated;
grant execute on function public.reserve_ai_daily_usage(integer) to authenticated;
revoke all on function public.commit_ai_daily_usage(uuid) from public, anon;
grant execute on function public.commit_ai_daily_usage(uuid) to authenticated;
grant execute on function public.reserve_ai_provider_budget(text, integer, date) to service_role;
