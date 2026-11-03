-- Stage 8: data export, immediate account lockout, and retryable deletion/purge.
-- The Auth account is soft-deleted immediately so app data can be retained for 30 days.

create table if not exists public.account_deletions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null unique,
    requested_at timestamptz not null default pg_catalog.clock_timestamp(),
    purge_after timestamptz not null,
    status text not null default 'pending' check (status in ('pending', 'purged')),
    auth_soft_deleted_at timestamptz null,
    app_data_purged_at timestamptz null,
    auth_hard_deleted_at timestamptz null,
    purged_at timestamptz null,
    soft_delete_attempts integer not null default 0 check (soft_delete_attempts >= 0),
    hard_delete_attempts integer not null default 0 check (hard_delete_attempts >= 0),
    last_soft_delete_attempt_at timestamptz null,
    last_hard_delete_attempt_at timestamptz null,
    last_auth_delete_result text null check (
        last_auth_delete_result is null
        or last_auth_delete_result in ('soft_deleted', 'hard_deleted', 'retryable_failure')
    ),
    constraint account_deletion_purge_after check (purge_after >= requested_at + interval '30 days'),
    constraint account_deletion_completion check (
        (status = 'pending' and purged_at is null)
        or (status = 'purged' and app_data_purged_at is not null
            and auth_hard_deleted_at is not null and purged_at is not null)
    )
);

create index if not exists account_deletions_soft_retry_idx
    on public.account_deletions (last_soft_delete_attempt_at nulls first, requested_at, id)
    where status = 'pending' and auth_soft_deleted_at is null;
create index if not exists account_deletions_hard_retry_idx
    on public.account_deletions (last_hard_delete_attempt_at nulls first, requested_at, id)
    where status = 'pending' and app_data_purged_at is not null and auth_hard_deleted_at is null;
create index if not exists account_deletions_due_idx
    on public.account_deletions (purge_after, id)
    where status = 'pending' and app_data_purged_at is null;

alter table public.account_deletions enable row level security;
drop policy if exists account_deletions_select_self on public.account_deletions;
create policy account_deletions_select_self on public.account_deletions
    for select to authenticated using (user_id = (select auth.uid()));
drop policy if exists account_deletions_insert_self on public.account_deletions;
create policy account_deletions_insert_self on public.account_deletions
    for insert to authenticated with check (user_id = (select auth.uid()));
drop policy if exists account_deletions_update_self on public.account_deletions;
create policy account_deletions_update_self on public.account_deletions
    for update to authenticated using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));

revoke all on table public.account_deletions from public, anon, authenticated;
grant select (id, user_id, requested_at, purge_after, status)
    on table public.account_deletions to authenticated;
grant insert (user_id, requested_at, purge_after, status)
    on table public.account_deletions to authenticated;
grant update (requested_at, purge_after, status)
    on table public.account_deletions to authenticated;
grant usage on schema public to service_role;
-- SECURITY INVOKER maintenance RPCs need explicit table privileges. Keep this
-- grant limited to rows the retention worker purges or its own queue updates.
grant select, update on table public.account_deletions to service_role;
grant select, delete on table public.users, public.reward_state, public.user_consents,
    public.tasks, public.steps, public.reward_events, public.ai_breakdown_cache,
    public.ai_usage_daily, public.ai_usage_reservations, public.api_idempotency,
    public.api_rate_limit_windows, public.ai_provider_usage_daily to service_role;

-- Narrow definer helper: it can inspect only the caller's lock status and
-- exposes no user data. A missing profile is active only before any deletion.
create or replace function public.current_user_account_active()
returns boolean language sql stable security definer set search_path = ''
as $function$
    select auth.uid() is not null
       and not exists (select 1 from public.account_deletions d where d.user_id = auth.uid())
       and coalesce((select u.deleted_at is null from public.users u where u.id = auth.uid()), true)
$function$;
revoke all on function public.current_user_account_active() from public, anon;
grant execute on function public.current_user_account_active() to authenticated;

-- Hide the profile and personal product rows after lockout. The dedicated
-- deletion-status RPC reads the tombstone to distinguish deletion from revocation.
drop policy if exists users_insert_self on public.users;
create policy users_insert_self on public.users
    for insert to authenticated
    with check (id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists users_select_self on public.users;
create policy users_select_self on public.users
    for select to authenticated
    using (id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists users_update_self on public.users;
create policy users_update_self on public.users
    for update to authenticated
    using (id = (select auth.uid()) and public.current_user_account_active())
    with check (id = (select auth.uid()));

drop policy if exists user_consents_select_self on public.user_consents;
create policy user_consents_select_self on public.user_consents
    for select to authenticated using (user_id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists user_consents_insert_self on public.user_consents;
create policy user_consents_insert_self on public.user_consents
    for insert to authenticated with check (user_id = (select auth.uid()) and public.current_user_account_active());

drop policy if exists tasks_all_self on public.tasks;
create policy tasks_all_self on public.tasks for all to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active())
    with check (user_id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists steps_all_self on public.steps;
create policy steps_all_self on public.steps for all to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active())
    with check (user_id = (select auth.uid()) and public.current_user_account_active()
        and exists (select 1 from public.tasks p where p.id = task_id and p.user_id = (select auth.uid())));

drop policy if exists reward_events_select_self on public.reward_events;
create policy reward_events_select_self on public.reward_events for select to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists reward_events_insert_self on public.reward_events;
create policy reward_events_insert_self on public.reward_events for insert to authenticated
    with check (user_id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists reward_state_select_self on public.reward_state;
create policy reward_state_select_self on public.reward_state for select to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists reward_state_insert_self on public.reward_state;
create policy reward_state_insert_self on public.reward_state for insert to authenticated
    with check (user_id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists reward_state_update_self on public.reward_state;
create policy reward_state_update_self on public.reward_state for update to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active())
    with check (user_id = (select auth.uid()) and public.current_user_account_active());

-- Drop and replace each permissive policy, because policies combine with OR.
drop policy if exists ai_breakdown_cache_all_self on public.ai_breakdown_cache;
drop policy if exists ai_cache_insert_self on public.ai_breakdown_cache;
drop policy if exists ai_cache_update_self on public.ai_breakdown_cache;
drop policy if exists ai_cache_delete_self on public.ai_breakdown_cache;
create policy ai_breakdown_cache_all_self on public.ai_breakdown_cache for all to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active())
    with check (user_id = (select auth.uid()) and public.current_user_account_active());
create policy ai_cache_insert_self on public.ai_breakdown_cache for insert to authenticated
    with check (user_id = (select auth.uid()) and public.current_user_account_active());
create policy ai_cache_update_self on public.ai_breakdown_cache for update to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active())
    with check (user_id = (select auth.uid()) and public.current_user_account_active());
create policy ai_cache_delete_self on public.ai_breakdown_cache for delete to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active());

drop policy if exists ai_usage_daily_select_self on public.ai_usage_daily;
drop policy if exists ai_usage_insert_self on public.ai_usage_daily;
drop policy if exists ai_usage_update_self on public.ai_usage_daily;
create policy ai_usage_daily_select_self on public.ai_usage_daily for select to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active());
create policy ai_usage_insert_self on public.ai_usage_daily for insert to authenticated
    with check (user_id = (select auth.uid()) and public.current_user_account_active());
create policy ai_usage_update_self on public.ai_usage_daily for update to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active())
    with check (user_id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists ai_usage_reservation_self on public.ai_usage_reservations;
create policy ai_usage_reservation_self on public.ai_usage_reservations for all to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active())
    with check (user_id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists api_idempotency_self on public.api_idempotency;
create policy api_idempotency_self on public.api_idempotency for all to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active())
    with check (user_id = (select auth.uid()) and public.current_user_account_active());
drop policy if exists api_rate_limit_self on public.api_rate_limit_windows;
create policy api_rate_limit_self on public.api_rate_limit_windows for all to authenticated
    using (user_id = (select auth.uid()) and public.current_user_account_active())
    with check (user_id = (select auth.uid()) and public.current_user_account_active());

create or replace function public.require_account_deletion_rpc()
returns trigger language plpgsql security invoker set search_path = ''
as $function$
begin
    if pg_catalog.current_setting('kickoff.account_deletion_write', true) is distinct from 'on' then
        raise exception using errcode = '42501', message = 'use the account deletion RPC';
    end if;
    if tg_op = 'DELETE' then return old; end if;
    return new;
end;
$function$;
drop trigger if exists account_deletions_rpc_only on public.account_deletions;
create trigger account_deletions_rpc_only before insert or update or delete on public.account_deletions
    for each row execute function public.require_account_deletion_rpc();
drop trigger if exists users_deletion_rpc_only on public.users;
create trigger users_deletion_rpc_only before update of deleted_at on public.users
    for each row execute function public.require_account_deletion_rpc();
revoke all on function public.require_account_deletion_rpc() from public, anon, authenticated;
grant update (deleted_at) on table public.users to authenticated;

-- Do not recreate a public user row if an Auth token remains valid after the
-- local profile has been purged. The no-FK deletion tombstone is authoritative.
create or replace function public.ensure_user_profile()
returns jsonb language plpgsql security invoker set search_path = '' set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    deletion_time timestamptz;
    result jsonb;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    select d.requested_at into deletion_time from public.account_deletions d where d.user_id = caller_id;
    if found then
        select pg_catalog.jsonb_build_object(
            'id', caller_id, 'deleted_at', coalesce(profile.deleted_at, deletion_time),
            'tokens_valid_after', profile.tokens_valid_after
        ) into result
        from (select 1) singleton left join public.users profile on profile.id = caller_id;
        return result;
    end if;
    insert into public.users (id) values (caller_id) on conflict (id) do nothing;
    insert into public.reward_state (user_id) values (caller_id) on conflict (user_id) do nothing;
    select pg_catalog.jsonb_build_object(
        'id', profile.id, 'display_name', profile.display_name,
        'language', profile.language, 'timezone', profile.timezone,
        'settings', profile.settings, 'created_at', profile.created_at,
        'updated_at', profile.updated_at, 'deleted_at', profile.deleted_at,
        'tokens_valid_after', profile.tokens_valid_after
    ) into result from public.users profile where profile.id = caller_id;
    return result;
end;
$function$;

create or replace function public.get_my_account_deletion_status()
returns jsonb language plpgsql security invoker set search_path = '' set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    result jsonb;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    select pg_catalog.jsonb_build_object('status', d.status, 'requested_at', d.requested_at)
    into result from public.account_deletions d where d.user_id = caller_id;
    return result;
end;
$function$;

create or replace function public.export_my_data()
returns jsonb language plpgsql security invoker set search_path = '' set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    exported_at timestamptz := pg_catalog.clock_timestamp();
    result jsonb;
begin
    if caller_id is null or not public.current_user_account_active() then
        raise exception using errcode = '42501', message = 'active account required';
    end if;
    select pg_catalog.jsonb_build_object(
        'export_version', 1,
        'exported_at', exported_at,
        'profile', pg_catalog.jsonb_build_object(
            'id', u.id, 'display_name', u.display_name, 'language', u.language,
            'timezone', u.timezone, 'settings', u.settings,
            'created_at', u.created_at, 'updated_at', u.updated_at
        ),
        'consents', coalesce((
            select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'id', c.id, 'consent_type', c.consent_type, 'granted', c.granted,
                'policy_version', c.policy_version, 'created_at', c.created_at
            ) order by c.created_at, c.id)
            from public.user_consents c where c.user_id = caller_id
        ), '[]'::jsonb),
        'tasks', coalesce((
            select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'id', t.id, 'title', t.title, 'notes', t.notes, 'source', t.source,
                'template_id', t.template_id, 'status', t.status, 'due_at', t.due_at,
                'completed_at', t.completed_at, 'version', t.version,
                'created_at', t.created_at, 'updated_at', t.updated_at, 'deleted_at', t.deleted_at
            ) order by t.created_at, t.id)
            from public.tasks t where t.user_id = caller_id
        ), '[]'::jsonb),
        'steps', coalesce((
            select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'id', s.id, 'task_id', s.task_id, 'text', s.text,
                'planned_minutes', s.planned_minutes, 'actual_minutes', s.actual_minutes,
                'position', s.position, 'status', s.status, 'started_at', s.started_at,
                'completed_at', s.completed_at, 'version', s.version,
                'created_at', s.created_at, 'updated_at', s.updated_at, 'deleted_at', s.deleted_at
            ) order by s.created_at, s.id)
            from public.steps s where s.user_id = caller_id
        ), '[]'::jsonb),
        'reward_events', coalesce((
            select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'id', e.id, 'task_id', e.task_id, 'step_id', e.step_id,
                'event_type', e.event_type, 'points_delta', e.points_delta,
                'streak_delta', e.streak_delta, 'dedupe_key', e.dedupe_key,
                'local_date', e.local_date, 'event_data', e.event_data,
                'created_at', e.created_at, 'event_order', e.event_order
            ) order by e.event_order, e.id)
            from public.reward_events e where e.user_id = caller_id
        ), '[]'::jsonb),
        'reward_state', (
            select pg_catalog.jsonb_build_object(
                'points_total', r.points_total, 'plant_stage', r.plant_stage,
                'current_streak', r.current_streak, 'best_streak', r.best_streak,
                'freezes_held', r.freezes_held, 'streak_status', r.streak_status,
                'last_active_local_date', r.last_active_local_date, 'updated_at', r.updated_at
            ) from public.reward_state r where r.user_id = caller_id
        ),
        'ai_breakdowns', coalesce((
            select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'prompt_version', c.prompt_version, 'result', c.result,
                'created_at', c.created_at, 'expires_at', c.expires_at
            ) order by c.created_at, c.id)
            from public.ai_breakdown_cache c where c.user_id = caller_id
        ), '[]'::jsonb),
        'ai_usage', coalesce((
            select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'usage_date', a.usage_date, 'generations', a.generations, 'updated_at', a.updated_at
            ) order by a.usage_date)
            from public.ai_usage_daily a where a.user_id = caller_id
        ), '[]'::jsonb)
    ) into result from public.users u where u.id = caller_id and u.deleted_at is null;
    return result;
end;
$function$;

create or replace function public.request_my_account_deletion(p_confirmation text)
returns jsonb language plpgsql security invoker set search_path = '' set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    instant timestamptz := pg_catalog.clock_timestamp();
    result jsonb;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_confirmation is distinct from 'DELETE' then
        raise exception using errcode = '22023', message = 'confirmation required';
    end if;
    perform pg_catalog.pg_advisory_xact_lock(48109, pg_catalog.hashtext(caller_id::text));
    if not exists (select 1 from public.users u where u.id = caller_id) then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;

    perform pg_catalog.set_config('kickoff.account_deletion_write', 'on', true);
    update public.users u
    set deleted_at = coalesce(u.deleted_at, instant),
        tokens_valid_after = coalesce(u.tokens_valid_after, instant), updated_at = instant
    where u.id = caller_id;
    insert into public.account_deletions (user_id, requested_at, purge_after, status)
    values (caller_id, instant, instant + interval '30 days', 'pending')
    on conflict (user_id) do nothing;
    select pg_catalog.jsonb_build_object(
        'id', d.id, 'requested_at', d.requested_at, 'purge_after', d.purge_after, 'status', d.status
    ) into result from public.account_deletions d where d.user_id = caller_id;
    return result;
end;
$function$;

-- Retry workers are bounded so one scheduled invocation fits the function budget.
create or replace function public.list_pending_auth_soft_deletions()
returns jsonb language plpgsql security invoker set search_path = '' set row_security = on
as $function$
begin
    if current_user <> 'service_role' then
        raise exception using errcode = '42501', message = 'server authorization required';
    end if;
    return coalesce((
        select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('id', batch.id, 'user_id', batch.user_id)
            order by batch.last_attempt nulls first, batch.requested_at, batch.id)
        from (
            select d.id, d.user_id, d.last_soft_delete_attempt_at as last_attempt, d.requested_at
            from public.account_deletions d
            where d.status = 'pending' and d.auth_soft_deleted_at is null
            order by d.last_soft_delete_attempt_at nulls first, d.requested_at, d.id
            limit 3
        ) batch
    ), '[]'::jsonb);
end;
$function$;

create or replace function public.list_pending_auth_hard_deletions()
returns jsonb language plpgsql security invoker set search_path = '' set row_security = on
as $function$
begin
    if current_user <> 'service_role' then
        raise exception using errcode = '42501', message = 'server authorization required';
    end if;
    return coalesce((
        select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('id', batch.id, 'user_id', batch.user_id)
            order by batch.last_attempt nulls first, batch.requested_at, batch.id)
        from (
            select d.id, d.user_id, d.last_hard_delete_attempt_at as last_attempt, d.requested_at
            from public.account_deletions d
            where d.status = 'pending' and d.app_data_purged_at is not null
              and d.auth_hard_deleted_at is null
            order by d.last_hard_delete_attempt_at nulls first, d.requested_at, d.id
            limit 3
        ) batch
    ), '[]'::jsonb);
end;
$function$;

create or replace function public.record_account_auth_delete_attempt(
    p_deletion_id uuid, p_phase text, p_succeeded boolean
)
returns jsonb language plpgsql security invoker set search_path = '' set row_security = on
as $function$
declare
    instant timestamptz := pg_catalog.clock_timestamp();
    changed integer;
begin
    if current_user <> 'service_role' then
        raise exception using errcode = '42501', message = 'server authorization required';
    end if;
    if p_deletion_id is null or p_phase is null or p_phase not in ('soft', 'hard')
       or p_succeeded is null then
        raise exception using errcode = '22023', message = 'invalid deletion attempt';
    end if;
    perform pg_catalog.set_config('kickoff.account_deletion_write', 'on', true);
    if p_phase = 'soft' then
        update public.account_deletions d
        set auth_soft_deleted_at = case when p_succeeded then coalesce(d.auth_soft_deleted_at, instant) else d.auth_soft_deleted_at end,
            soft_delete_attempts = d.soft_delete_attempts + 1,
            last_soft_delete_attempt_at = instant,
            last_auth_delete_result = case when p_succeeded then 'soft_deleted' else 'retryable_failure' end
        where d.id = p_deletion_id and d.status = 'pending';
    else
        update public.account_deletions d
        set auth_hard_deleted_at = case when p_succeeded then coalesce(d.auth_hard_deleted_at, instant) else d.auth_hard_deleted_at end,
            hard_delete_attempts = d.hard_delete_attempts + 1,
            last_hard_delete_attempt_at = instant,
            last_auth_delete_result = case when p_succeeded then 'hard_deleted' else 'retryable_failure' end,
            status = case when p_succeeded then 'purged' else d.status end,
            purged_at = case when p_succeeded then coalesce(d.purged_at, instant) else d.purged_at end
        where d.id = p_deletion_id and d.status = 'pending' and d.app_data_purged_at is not null;
    end if;
    get diagnostics changed = row_count;
    return pg_catalog.jsonb_build_object('updated', changed = 1);
end;
$function$;

-- Append-only reward history may be removed only by this bounded retention job.
create or replace function public.reject_reward_event_mutation()
returns trigger language plpgsql security invoker set search_path = ''
as $function$
begin
    if pg_catalog.pg_trigger_depth() > 1 then return old; end if;
    if current_user = 'service_role'
       and pg_catalog.current_setting('kickoff.account_deletion_purge', true) = 'on' then
        return old;
    end if;
    raise exception using errcode = '42501', message = 'reward events are append only';
end;
$function$;

create or replace function public.run_data_retention_purge()
returns jsonb language plpgsql security invoker set search_path = '' set row_security = on
as $function$
declare
    instant timestamptz := pg_catalog.clock_timestamp();
    due_deletion record;
    deletion_count integer := 0;
    soft_task_count integer := 0;
    soft_step_count integer := 0;
    event_count integer := 0;
    expired_cache_count integer := 0;
    expired_idempotency_count integer := 0;
    expired_rate_count integer := 0;
    expired_reservation_count integer := 0;
    expired_provider_count integer := 0;
begin
    if current_user <> 'service_role' then
        raise exception using errcode = '42501', message = 'server authorization required';
    end if;
    perform pg_catalog.set_config('kickoff.account_deletion_write', 'on', true);
    perform pg_catalog.set_config('kickoff.account_deletion_purge', 'on', true);
    perform pg_catalog.set_config('kickoff.idempotency_write', 'on', true);
    perform pg_catalog.set_config('kickoff.rate_limit_write', 'on', true);
    perform pg_catalog.set_config('kickoff.task_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.step_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.ai_write', 'on', true);
    perform pg_catalog.set_config('kickoff.ai_budget_write', 'on', true);

    -- Reward-event references must be removed before an expired soft-delete row.
    delete from public.reward_events e
    where e.step_id in (select s.id from public.steps s
                        where s.deleted_at is not null and s.deleted_at <= instant - interval '30 days')
       or e.task_id in (select t.id from public.tasks t
                        where t.deleted_at is not null and t.deleted_at <= instant - interval '30 days')
       or e.step_id in (
            select s.id from public.steps s join public.tasks t on t.id = s.task_id
            where t.deleted_at is not null and t.deleted_at <= instant - interval '30 days'
       );
    get diagnostics event_count = row_count;
    delete from public.steps s
    where s.deleted_at is not null and s.deleted_at <= instant - interval '30 days'
      and not exists (select 1 from public.tasks t where t.id = s.task_id
                      and t.deleted_at is not null and t.deleted_at <= instant - interval '30 days');
    get diagnostics soft_step_count = row_count;
    delete from public.tasks t
    where t.deleted_at is not null and t.deleted_at <= instant - interval '30 days';
    get diagnostics soft_task_count = row_count;

    for due_deletion in
        select d.id, d.user_id from public.account_deletions d
        where d.status = 'pending' and d.purge_after <= instant and d.app_data_purged_at is null
        order by d.purge_after, d.id limit 100 for update skip locked
    loop
        delete from public.reward_events where user_id = due_deletion.user_id;
        delete from public.steps where user_id = due_deletion.user_id;
        delete from public.tasks where user_id = due_deletion.user_id;
        delete from public.user_consents where user_id = due_deletion.user_id;
        delete from public.ai_breakdown_cache where user_id = due_deletion.user_id;
        delete from public.ai_usage_daily where user_id = due_deletion.user_id;
        delete from public.ai_usage_reservations where user_id = due_deletion.user_id;
        delete from public.api_idempotency where user_id = due_deletion.user_id;
        delete from public.api_rate_limit_windows where user_id = due_deletion.user_id;
        delete from public.reward_state where user_id = due_deletion.user_id;
        delete from public.users where id = due_deletion.user_id;
        update public.account_deletions d set app_data_purged_at = instant
        where d.id = due_deletion.id and d.app_data_purged_at is null;
        deletion_count := deletion_count + 1;
    end loop;

    delete from public.ai_breakdown_cache where expires_at <= instant;
    get diagnostics expired_cache_count = row_count;
    delete from public.api_idempotency where expires_at <= instant;
    get diagnostics expired_idempotency_count = row_count;
    delete from public.api_rate_limit_windows where expires_at <= instant;
    get diagnostics expired_rate_count = row_count;
    delete from public.ai_usage_reservations where expires_at <= instant;
    get diagnostics expired_reservation_count = row_count;
    delete from public.ai_provider_usage_daily where usage_date < (instant at time zone 'UTC')::date - 90;
    get diagnostics expired_provider_count = row_count;

    return pg_catalog.jsonb_build_object(
        'accounts_data_purged', deletion_count,
        'soft_deleted_tasks_purged', soft_task_count,
        'soft_deleted_steps_purged', soft_step_count,
        'reward_events_purged', event_count,
        'expired_ai_cache_rows_purged', expired_cache_count,
        'expired_idempotency_rows_purged', expired_idempotency_count,
        'expired_rate_limit_rows_purged', expired_rate_count,
        'expired_ai_reservations_purged', expired_reservation_count,
        'expired_provider_budget_rows_purged', expired_provider_count
    );
end;
$function$;

revoke all on function public.get_my_account_deletion_status() from public, anon;
revoke all on function public.export_my_data() from public, anon;
revoke all on function public.request_my_account_deletion(text) from public, anon;
revoke all on function public.list_pending_auth_soft_deletions() from public, anon, authenticated;
revoke all on function public.list_pending_auth_hard_deletions() from public, anon, authenticated;
revoke all on function public.record_account_auth_delete_attempt(uuid, text, boolean) from public, anon, authenticated;
revoke all on function public.run_data_retention_purge() from public, anon, authenticated;
grant execute on function public.get_my_account_deletion_status() to authenticated;
grant execute on function public.export_my_data() to authenticated;
grant execute on function public.request_my_account_deletion(text) to authenticated;
grant execute on function public.list_pending_auth_soft_deletions() to service_role;
grant execute on function public.list_pending_auth_hard_deletions() to service_role;
grant execute on function public.record_account_auth_delete_attempt(uuid, text, boolean) to service_role;
grant execute on function public.run_data_retention_purge() to service_role;
