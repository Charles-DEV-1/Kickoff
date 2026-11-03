-- Stage 3 task and step API. All RPCs run as the authenticated caller, derive
-- ownership from auth.uid(), and remain subject to the Stage 1/2 RLS policies.

create or replace function public.get_my_task(p_task_id uuid)
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
        'id', task.id,
        'title', task.title,
        'notes', task.notes,
        'source', task.source,
        'template_id', task.template_id,
        'status', task.status,
        'due_at', task.due_at,
        'version', task.version,
        'created_at', task.created_at,
        'updated_at', task.updated_at,
        'deleted_at', task.deleted_at,
        'steps', coalesce((
            select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'id', step.id,
                'task_id', step.task_id,
                'text', step.text,
                'planned_minutes', step.planned_minutes,
                'actual_minutes', step.actual_minutes,
                'position', step.position,
                'status', step.status,
                'started_at', step.started_at,
                'completed_at', step.completed_at,
                'version', step.version,
                'created_at', step.created_at,
                'updated_at', step.updated_at,
                'deleted_at', step.deleted_at
            ) order by step.position, step.id)
            from public.steps as step
            where step.task_id = task.id
              and step.user_id = caller_id
              and step.deleted_at is null
        ), '[]'::jsonb)
    )
    into result
    from public.tasks as task
    where task.id = p_task_id
      and task.user_id = caller_id
      and task.deleted_at is null;

    return result;
end;
$function$;

create or replace function public.get_my_step(p_step_id uuid)
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
        'id', step.id,
        'task_id', step.task_id,
        'text', step.text,
        'planned_minutes', step.planned_minutes,
        'actual_minutes', step.actual_minutes,
        'position', step.position,
        'status', step.status,
        'started_at', step.started_at,
        'completed_at', step.completed_at,
        'version', step.version,
        'created_at', step.created_at,
        'updated_at', step.updated_at,
        'deleted_at', step.deleted_at
    )
    into result
    from public.steps as step
    join public.tasks as task
      on task.id = step.task_id and task.user_id = step.user_id
    where step.id = p_step_id
      and step.user_id = caller_id
      and step.deleted_at is null
      and task.deleted_at is null;

    return result;
end;
$function$;

create or replace function public.list_my_tasks(
    p_status text,
    p_due_date date,
    p_updated_since timestamptz,
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
    result jsonb;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_status is null or p_status not in ('active', 'completed', 'archived', 'all')
       or p_limit is null or p_limit < 1 or p_limit > 50
       or ((p_cursor_updated_at is null) <> (p_cursor_id is null)) then
        raise exception using errcode = '22023', message = 'invalid task query';
    end if;

    with filtered as (
        select task.id, task.updated_at,
            pg_catalog.row_number() over (order by task.updated_at desc, task.id desc) as row_num
        from public.tasks as task
        where task.user_id = caller_id
          and task.deleted_at is null
          and (p_status = 'all' or task.status = p_status)
          and (p_due_date is null or (task.due_at at time zone 'UTC')::date = p_due_date)
          and (p_updated_since is null or task.updated_at >= p_updated_since)
          and (
              p_cursor_updated_at is null
              or (task.updated_at, task.id) < (p_cursor_updated_at, p_cursor_id)
          )
    ), page as (
        select filtered.id, filtered.updated_at, filtered.row_num
        from filtered
        where filtered.row_num <= p_limit
    )
    select pg_catalog.jsonb_build_object(
        'items', coalesce((
            select pg_catalog.jsonb_agg(
                public.get_my_task(page.id)
                order by page.updated_at desc, page.id desc
            )
            from page
        ), '[]'::jsonb),
        'has_more', exists(select 1 from filtered where filtered.row_num > p_limit)
    )
    into result;

    return result;
end;
$function$;

create or replace function public.create_my_task(p_task jsonb, p_steps jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    task_id uuid;
    source_value text;
    template_value uuid;
    active_count integer;
    step_count integer;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_task is null or pg_catalog.jsonb_typeof(p_task) <> 'object'
       or p_steps is null or pg_catalog.jsonb_typeof(p_steps) <> 'array'
       or pg_catalog.jsonb_array_length(p_steps) > 30 then
        raise exception using errcode = '22023', message = 'invalid task payload';
    end if;

    perform pg_catalog.pg_advisory_xact_lock(
        48107, pg_catalog.hashtext(caller_id::text)
    );
    select pg_catalog.count(*) into active_count
    from public.tasks as task
    where task.user_id = caller_id and task.status = 'active' and task.deleted_at is null;
    if active_count >= 500 then
        return pg_catalog.jsonb_build_object('state', 'task_limit');
    end if;

    source_value := coalesce(p_task ->> 'source', 'manual');
    template_value := nullif(p_task ->> 'template_id', '')::uuid;
    if (source_value = 'template') <> (template_value is not null) then
        return pg_catalog.jsonb_build_object('state', 'invalid_source_template');
    end if;
    if source_value = 'template' and not exists (
        select 1 from public.templates as template
        where template.id = template_value and template.is_active
    ) then
        return pg_catalog.jsonb_build_object('state', 'invalid_template');
    end if;

    perform pg_catalog.set_config('kickoff.task_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.step_api_write', 'on', true);
    insert into public.tasks (user_id, title, notes, source, template_id, due_at)
    values (
        caller_id,
        p_task ->> 'title',
        coalesce(p_task ->> 'notes', ''),
        source_value,
        template_value,
        nullif(p_task ->> 'due_at', '')::timestamptz
    ) returning id into task_id;

    select pg_catalog.count(*) into step_count
    from pg_catalog.jsonb_array_elements(p_steps);
    if step_count > 0 then
        insert into public.steps (task_id, user_id, text, planned_minutes, position)
        select
            task_id,
            caller_id,
            item.value ->> 'text',
            (item.value ->> 'planned_minutes')::smallint,
            (item.ordinality - 1)::smallint
        from pg_catalog.jsonb_array_elements(p_steps) with ordinality as item(value, ordinality);
    end if;

    return public.get_my_task(task_id);
end;
$function$;

create or replace function public.update_my_task(
    p_task_id uuid,
    p_expected_version integer,
    p_patch jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    current_version integer;
    current_source text;
    current_template uuid;
    next_source text;
    next_template uuid;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_expected_version is null or p_expected_version < 1
       or p_patch is null or pg_catalog.jsonb_typeof(p_patch) <> 'object' then
        raise exception using errcode = '22023', message = 'invalid task update';
    end if;

    select task.version, task.source, task.template_id
    into current_version, current_source, current_template
    from public.tasks as task
    where task.id = p_task_id and task.user_id = caller_id and task.deleted_at is null
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;
    if current_version <> p_expected_version then
        return pg_catalog.jsonb_build_object(
            'state', 'version_conflict', 'current', public.get_my_task(p_task_id)
        );
    end if;

    next_source := case when p_patch ? 'source' then p_patch ->> 'source' else current_source end;
    next_template := case
        when p_patch ? 'template_id' then nullif(p_patch ->> 'template_id', '')::uuid
        else current_template
    end;
    if next_source is null or (next_source = 'template') <> (next_template is not null) then
        return pg_catalog.jsonb_build_object('state', 'invalid_source_template');
    end if;
    if next_source = 'template' and not exists (
        select 1 from public.templates as template
        where template.id = next_template and template.is_active
    ) then
        return pg_catalog.jsonb_build_object('state', 'invalid_template');
    end if;

    perform pg_catalog.set_config('kickoff.task_api_write', 'on', true);
    update public.tasks as task
    set title = case when p_patch ? 'title' then p_patch ->> 'title' else task.title end,
        notes = case when p_patch ? 'notes' then p_patch ->> 'notes' else task.notes end,
        source = next_source,
        template_id = next_template,
        due_at = case
            when p_patch ? 'due_at' then nullif(p_patch ->> 'due_at', '')::timestamptz
            else task.due_at
        end,
        version = task.version + 1,
        updated_at = pg_catalog.clock_timestamp()
    where task.id = p_task_id and task.user_id = caller_id;

    return public.get_my_task(p_task_id);
end;
$function$;

create or replace function public.soft_delete_my_task(
    p_task_id uuid,
    p_expected_version integer
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    current_version integer;
    instant timestamptz := pg_catalog.clock_timestamp();
    new_version integer;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_expected_version is null or p_expected_version < 1 then
        raise exception using errcode = '22023', message = 'invalid task version';
    end if;
    select task.version into current_version
    from public.tasks as task
    where task.id = p_task_id and task.user_id = caller_id and task.deleted_at is null
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;
    if current_version <> p_expected_version then
        return pg_catalog.jsonb_build_object(
            'state', 'version_conflict', 'current', public.get_my_task(p_task_id)
        );
    end if;

    perform pg_catalog.set_config('kickoff.task_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.step_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.task_lifecycle_write', 'on', true);
    perform pg_catalog.set_config('kickoff.step_lifecycle_write', 'on', true);
    update public.tasks as task
    set deleted_at = instant, version = task.version + 1, updated_at = instant
    where task.id = p_task_id and task.user_id = caller_id;
    update public.steps as step
    set deleted_at = instant, version = step.version + 1, updated_at = instant
    where step.task_id = p_task_id and step.user_id = caller_id and step.deleted_at is null;
    new_version := current_version + 1;
    return pg_catalog.jsonb_build_object(
        'deleted', true, 'id', p_task_id, 'version', new_version, 'deleted_at', instant
    );
end;
$function$;

create or replace function public.create_my_step(p_task_id uuid, p_step jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    task_version integer;
    active_count integer;
    next_position integer;
    new_step_id uuid;
    new_task_version integer;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_step is null or pg_catalog.jsonb_typeof(p_step) <> 'object' then
        raise exception using errcode = '22023', message = 'invalid step payload';
    end if;
    select task.version into task_version
    from public.tasks as task
    where task.id = p_task_id and task.user_id = caller_id and task.deleted_at is null
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;

    select pg_catalog.count(*) into active_count
    from public.steps as step
    where step.task_id = p_task_id and step.user_id = caller_id and step.deleted_at is null;
    if active_count >= 30 then
        return pg_catalog.jsonb_build_object('state', 'step_limit');
    end if;
    select candidate.position into next_position
    from pg_catalog.generate_series(0, 29) as candidate(position)
    where not exists (
        select 1 from public.steps as step
        where step.task_id = p_task_id and step.user_id = caller_id
          and step.deleted_at is null and step.position = candidate.position
    )
    order by candidate.position
    limit 1;

    perform pg_catalog.set_config('kickoff.step_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.task_api_write', 'on', true);
    insert into public.steps (task_id, user_id, text, planned_minutes, position)
    values (
        p_task_id, caller_id,
        p_step ->> 'text',
        (p_step ->> 'planned_minutes')::smallint,
        next_position::smallint
    ) returning id into new_step_id;
    update public.tasks as task
    set version = task.version + 1, updated_at = pg_catalog.clock_timestamp()
    where task.id = p_task_id and task.user_id = caller_id
    returning task.version into new_task_version;
    return pg_catalog.jsonb_build_object(
        'step', public.get_my_step(new_step_id), 'task_version', new_task_version
    );
end;
$function$;

create or replace function public.update_my_step(
    p_step_id uuid,
    p_expected_version integer,
    p_patch jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    current_version integer;
    parent_task_id uuid;
    task_version integer;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_expected_version is null or p_expected_version < 1
       or p_patch is null or pg_catalog.jsonb_typeof(p_patch) <> 'object' then
        raise exception using errcode = '22023', message = 'invalid step update';
    end if;
    select step.version, step.task_id
    into current_version, parent_task_id
    from public.steps as step
    join public.tasks as task
      on task.id = step.task_id and task.user_id = step.user_id
    where step.id = p_step_id and step.user_id = caller_id
      and step.deleted_at is null and task.deleted_at is null
    for update of step, task;
    if not found then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;
    if current_version <> p_expected_version then
        return pg_catalog.jsonb_build_object(
            'state', 'version_conflict', 'current', public.get_my_step(p_step_id)
        );
    end if;

    perform pg_catalog.set_config('kickoff.step_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.task_api_write', 'on', true);
    update public.steps as step
    set text = case when p_patch ? 'text' then p_patch ->> 'text' else step.text end,
        planned_minutes = case
            when p_patch ? 'planned_minutes' then (p_patch ->> 'planned_minutes')::smallint
            else step.planned_minutes
        end,
        version = step.version + 1,
        updated_at = pg_catalog.clock_timestamp()
    where step.id = p_step_id and step.user_id = caller_id;
    update public.tasks as task
    set version = task.version + 1, updated_at = pg_catalog.clock_timestamp()
    where task.id = parent_task_id and task.user_id = caller_id
    returning task.version into task_version;

    return pg_catalog.jsonb_build_object(
        'step', public.get_my_step(p_step_id), 'task_version', task_version
    );
end;
$function$;

create or replace function public.soft_delete_my_step(
    p_step_id uuid,
    p_expected_version integer
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    current_version integer;
    parent_task_id uuid;
    instant timestamptz := pg_catalog.clock_timestamp();
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_expected_version is null or p_expected_version < 1 then
        raise exception using errcode = '22023', message = 'invalid step version';
    end if;
    select step.version, step.task_id
    into current_version, parent_task_id
    from public.steps as step
    join public.tasks as task
      on task.id = step.task_id and task.user_id = step.user_id
    where step.id = p_step_id and step.user_id = caller_id
      and step.deleted_at is null and task.deleted_at is null
    for update of step, task;
    if not found then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;
    if current_version <> p_expected_version then
        return pg_catalog.jsonb_build_object(
            'state', 'version_conflict', 'current', public.get_my_step(p_step_id)
        );
    end if;

    perform pg_catalog.set_config('kickoff.step_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.task_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.step_lifecycle_write', 'on', true);
    update public.steps as step
    set deleted_at = instant, version = step.version + 1, updated_at = instant
    where step.id = p_step_id and step.user_id = caller_id;
    update public.tasks as task
    set version = task.version + 1, updated_at = instant
    where task.id = parent_task_id and task.user_id = caller_id;
    return pg_catalog.jsonb_build_object(
        'deleted', true,
        'id', p_step_id,
        'version', current_version + 1,
        'deleted_at', instant
    );
end;
$function$;

create or replace function public.reorder_my_steps(
    p_task_id uuid,
    p_expected_version integer,
    p_step_ids uuid[]
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
set row_security = on
as $function$
declare
    caller_id uuid := auth.uid();
    current_version integer;
    active_count integer;
    supplied_count integer;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_expected_version is null or p_expected_version < 1 or p_step_ids is null
       or coalesce(pg_catalog.cardinality(p_step_ids), 0) > 30 then
        raise exception using errcode = '22023', message = 'invalid reorder request';
    end if;
    select task.version into current_version
    from public.tasks as task
    where task.id = p_task_id and task.user_id = caller_id and task.deleted_at is null
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;
    if current_version <> p_expected_version then
        return pg_catalog.jsonb_build_object(
            'state', 'version_conflict', 'current', public.get_my_task(p_task_id)
        );
    end if;

    select pg_catalog.count(*) into active_count
    from public.steps as step
    where step.task_id = p_task_id and step.user_id = caller_id and step.deleted_at is null;
    select pg_catalog.count(distinct supplied.step_id) into supplied_count
    from pg_catalog.unnest(p_step_ids) as supplied(step_id);
    if active_count <> coalesce(pg_catalog.cardinality(p_step_ids), 0)
       or supplied_count <> coalesce(pg_catalog.cardinality(p_step_ids), 0)
       or (
           select pg_catalog.count(*) from public.steps as step
           where step.task_id = p_task_id and step.user_id = caller_id
             and step.deleted_at is null and step.id = any(p_step_ids)
       ) <> active_count then
        return pg_catalog.jsonb_build_object('state', 'invalid_order');
    end if;

    perform pg_catalog.set_config('kickoff.step_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.task_api_write', 'on', true);
    update public.steps as step
    set position = (ordered.ordinality - 1)::smallint,
        version = step.version + 1,
        updated_at = pg_catalog.clock_timestamp()
    from pg_catalog.unnest(p_step_ids) with ordinality as ordered(step_id, ordinality)
    where step.id = ordered.step_id
      and step.task_id = p_task_id
      and step.user_id = caller_id
      and step.deleted_at is null;
    update public.tasks as task
    set version = task.version + 1, updated_at = pg_catalog.clock_timestamp()
    where task.id = p_task_id and task.user_id = caller_id;

    return public.get_my_task(p_task_id);
end;
$function$;

-- All task/step writes now pass through the versioned RPC API. Column grants
-- remain for SECURITY INVOKER execution, while these guards stop direct table
-- edits from skipping version checks and operation validation.
drop trigger if exists tasks_api_rpc_only on public.tasks;
create trigger tasks_api_rpc_only
    before insert or update or delete on public.tasks
    for each row execute function public.require_protection_rpc_mutation(
        'kickoff.task_api_write'
    );
drop trigger if exists steps_api_rpc_only on public.steps;
create trigger steps_api_rpc_only
    before insert or update or delete on public.steps
    for each row execute function public.require_protection_rpc_mutation(
        'kickoff.step_api_write'
    );

revoke all on function public.get_my_task(uuid) from public, anon;
revoke all on function public.get_my_step(uuid) from public, anon;
revoke all on function public.list_my_tasks(text, date, timestamptz, timestamptz, uuid, integer) from public, anon;
revoke all on function public.create_my_task(jsonb, jsonb) from public, anon;
revoke all on function public.update_my_task(uuid, integer, jsonb) from public, anon;
revoke all on function public.soft_delete_my_task(uuid, integer) from public, anon;
revoke all on function public.create_my_step(uuid, jsonb) from public, anon;
revoke all on function public.update_my_step(uuid, integer, jsonb) from public, anon;
revoke all on function public.soft_delete_my_step(uuid, integer) from public, anon;
revoke all on function public.reorder_my_steps(uuid, integer, uuid[]) from public, anon;

grant execute on function public.get_my_task(uuid) to authenticated;
grant execute on function public.get_my_step(uuid) to authenticated;
grant execute on function public.list_my_tasks(text, date, timestamptz, timestamptz, uuid, integer) to authenticated;
grant execute on function public.create_my_task(jsonb, jsonb) to authenticated;
grant execute on function public.update_my_task(uuid, integer, jsonb) to authenticated;
grant execute on function public.soft_delete_my_task(uuid, integer) to authenticated;
grant execute on function public.create_my_step(uuid, jsonb) to authenticated;
grant execute on function public.update_my_step(uuid, integer, jsonb) to authenticated;
grant execute on function public.soft_delete_my_step(uuid, integer) to authenticated;
grant execute on function public.reorder_my_steps(uuid, integer, uuid[]) to authenticated;
