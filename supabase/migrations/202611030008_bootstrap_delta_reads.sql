-- Stage 7: bounded, user-scoped online bootstrap and delta reads.
-- All functions run as the signed-in caller and remain subject to RLS.

create index if not exists tasks_user_updated_delta_idx
    on public.tasks (user_id, updated_at desc, id desc);
create index if not exists steps_task_owner_delta_idx
    on public.steps (task_id, user_id, position, id);

create or replace function public.kickoff_task_json(p_task_id uuid, p_include_deleted_steps boolean default false)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    result jsonb;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    select pg_catalog.jsonb_build_object(
        'id', task.id, 'title', task.title, 'notes', task.notes,
        'source', task.source, 'template_id', task.template_id,
        'status', task.status, 'due_at', task.due_at,
        'version', task.version, 'created_at', task.created_at,
        'updated_at', task.updated_at, 'deleted_at', task.deleted_at,
        'steps', coalesce((
            select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'id', step.id, 'task_id', step.task_id, 'text', step.text,
                'planned_minutes', step.planned_minutes, 'actual_minutes', step.actual_minutes,
                'position', step.position, 'status', step.status,
                'started_at', step.started_at, 'completed_at', step.completed_at,
                'version', step.version, 'created_at', step.created_at,
                'updated_at', step.updated_at, 'deleted_at', step.deleted_at
            ) order by step.position, step.id)
            from public.steps as step
            where step.task_id = task.id and step.user_id = caller_id
              and (p_include_deleted_steps or step.deleted_at is null)
        ), '[]'::jsonb)
    ) into result
    from public.tasks as task
    where task.id = p_task_id and task.user_id = caller_id;
    return result;
end;
$function$;

create or replace function public.get_my_bootstrap()
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    instant timestamptz := pg_catalog.clock_timestamp();
    result jsonb;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    select pg_catalog.jsonb_build_object(
        'profile', pg_catalog.jsonb_build_object(
            'id', user_row.id, 'display_name', user_row.display_name,
            'language', user_row.language, 'timezone', user_row.timezone,
            'settings', user_row.settings, 'created_at', user_row.created_at,
            'updated_at', user_row.updated_at
        ),
        'reward_state', pg_catalog.jsonb_build_object(
            'points_total', reward.points_total, 'plant_stage', reward.plant_stage,
            'current_streak', reward.current_streak, 'best_streak', reward.best_streak,
            'freezes_held', reward.freezes_held, 'streak_status', reward.streak_status,
            'last_active_local_date', reward.last_active_local_date,
            'updated_at', reward.updated_at
        ),
        'tasks', coalesce((
            select pg_catalog.jsonb_agg(public.kickoff_task_json(page.id, false)
                    order by page.updated_at desc, page.id desc)
            from (
                select task.id, task.updated_at
                from public.tasks as task
                where task.user_id = caller_id and task.status = 'active'
                  and task.deleted_at is null
                order by task.updated_at desc, task.id desc
                limit 50
            ) as page
        ), '[]'::jsonb),
        'active_task_count', (
            select pg_catalog.count(*)::integer from public.tasks as task
            where task.user_id = caller_id and task.status = 'active' and task.deleted_at is null
        ),
        'server_time', instant
    ) into result
    from public.users as user_row
    join public.reward_state as reward on reward.user_id = user_row.id
    where user_row.id = caller_id and user_row.deleted_at is null;
    return result;
end;
$function$;

create or replace function public.list_my_task_delta(
    p_updated_since timestamptz,
    p_snapshot_at timestamptz,
    p_status text,
    p_due_date date,
    p_cursor_updated_at timestamptz,
    p_cursor_id uuid,
    p_limit integer
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
    snapshot_instant timestamptz;
    result jsonb;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_updated_since is null or p_status is null
       or p_status not in ('active', 'completed', 'archived', 'all')
       or p_limit is null or p_limit < 1 or p_limit > 50
       or ((p_cursor_updated_at is null) <> (p_cursor_id is null))
       or (p_snapshot_at is not null and (p_snapshot_at > instant or p_snapshot_at < p_updated_since)) then
        raise exception using errcode = '22023', message = 'invalid task delta query';
    end if;
    snapshot_instant := coalesce(p_snapshot_at, instant);

    if p_updated_since < instant - interval '30 days' then
        return pg_catalog.jsonb_build_object(
            'items', '[]'::jsonb, 'has_more', false,
            'full_refresh_required', true, 'server_time', instant, 'next_since', null
        );
    end if;

    with changed as (
        select task.id, task.updated_at,
               (task.deleted_at is null and (p_status = 'all' or task.status = p_status)
                and (p_due_date is null or (task.due_at at time zone 'UTC')::date = p_due_date)
               ) as matches_filter,
               pg_catalog.row_number() over (order by task.updated_at desc, task.id desc) as row_num
        from public.tasks as task
        where task.user_id = caller_id
          and task.updated_at >= p_updated_since and task.updated_at <= snapshot_instant
          and (p_cursor_updated_at is null or (task.updated_at, task.id) < (p_cursor_updated_at, p_cursor_id))
    ), page as (
        select changed.id, changed.updated_at, changed.matches_filter, changed.row_num
        from changed where changed.row_num <= p_limit
    )
    select pg_catalog.jsonb_build_object(
        'items', coalesce((
            select pg_catalog.jsonb_agg(
                public.kickoff_task_json(page.id, true) || pg_catalog.jsonb_build_object('matches_filter', page.matches_filter)
                order by page.updated_at desc, page.id desc
            ) from page
        ), '[]'::jsonb),
        'has_more', exists(select 1 from changed where changed.row_num > p_limit),
        'full_refresh_required', false,
        'server_time', instant,
        'snapshot_at', snapshot_instant,
        'next_since', snapshot_instant
    ) into result;
    return result;
end;
$function$;

revoke all on function public.kickoff_task_json(uuid, boolean) from public, anon;
revoke all on function public.get_my_bootstrap() from public, anon;
revoke all on function public.list_my_task_delta(timestamptz, timestamptz, text, date, timestamptz, uuid, integer) from public, anon;
grant execute on function public.kickoff_task_json(uuid, boolean) to authenticated;
grant execute on function public.get_my_bootstrap() to authenticated;
grant execute on function public.list_my_task_delta(timestamptz, timestamptz, text, date, timestamptz, uuid, integer) to authenticated;
