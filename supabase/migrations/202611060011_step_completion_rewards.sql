-- Stage 11: award points on each completed step.
-- Apply after 202611040010_task_completion_only_rewards.sql.
-- New default: 0 points for starting, 5 points for completing a step.
-- Existing reward totals/events are retained; this changes future first-time step completions.

begin;

update public.reward_config
set start_points = 0,
    completion_points = 5,
    updated_at = pg_catalog.now()
where config_key = 'default';

create or replace function public.act_on_my_step(
    p_step_id uuid,
    p_action text,
    p_expected_version integer,
    p_actual_minutes integer,
    p_idempotency_key text,
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
    step_row public.steps%rowtype;
    task_row public.tasks%rowtype;
    profile_timezone text;
    action_time timestamptz := pg_catalog.clock_timestamp();
    action_date date;
    new_status text;
    new_started timestamptz;
    new_completed timestamptz;
    new_actual integer;
    cfg_start integer;
    cfg_complete integer;
    thresholds integer[];
    point_total integer;
    previous_plant_stage smallint;
    stage smallint;
    streak integer;
    best integer;
    freezes smallint;
    streak_state text;
    last_date date;
    gap_days integer;
    covered integer;
    awarded integer := 0;
    key_list text[] := array[]::text[];
    type_list text[] := array[]::text[];
    point_list integer[] := array[]::integer[];
    step_list uuid[] := array[]::uuid[];
    date_list date[] := array[]::date[];
    is_done boolean;
    ix integer;
    event_json jsonb;
    step_json jsonb;
    task_json jsonb;
    state_json jsonb;
    response_json jsonb;
    parent_task_id uuid;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if p_action not in ('start', 'complete', 'reopen')
       or p_expected_version is null or p_expected_version < 1
       or (p_actual_minutes is not null and (p_actual_minutes < 0 or p_actual_minutes > 600)) then
        raise exception using errcode = '22023', message = 'invalid step action';
    end if;

    select profile.timezone into profile_timezone
    from public.users as profile
    where profile.id = caller_id and profile.deleted_at is null;
    if not found then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;
    action_date := (action_time at time zone profile_timezone)::date;

    select step.task_id into parent_task_id
    from public.steps as step
    join public.tasks as task on task.id = step.task_id and task.user_id = step.user_id
    where step.id = p_step_id and step.user_id = caller_id
      and step.deleted_at is null and task.deleted_at is null;
    if not found then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;
    select task.* into task_row from public.tasks as task
    where task.id = parent_task_id and task.user_id = caller_id
      and task.deleted_at is null
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;
    select step.* into step_row from public.steps as step
    where step.id = p_step_id and step.task_id = parent_task_id
      and step.user_id = caller_id and step.deleted_at is null
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('state', 'not_found');
    end if;
    if step_row.version <> p_expected_version then
        return pg_catalog.jsonb_build_object(
            'state', 'version_conflict', 'current', public.get_my_step(p_step_id)
        );
    end if;

    new_status := step_row.status;
    new_started := step_row.started_at;
    new_completed := step_row.completed_at;
    new_actual := step_row.actual_minutes;
    if p_action = 'start' and step_row.status = 'pending' then
        new_status := 'active'; new_started := action_time; new_completed := null; new_actual := null;
    elsif p_action = 'complete' and step_row.status in ('pending', 'active') then
        if step_row.status = 'pending' then new_started := action_time; end if;
        new_status := 'completed'; new_completed := action_time;
        if p_actual_minutes is not null then
            new_actual := p_actual_minutes;
        else
            new_actual := least(600, greatest(0,
                floor(extract(epoch from (action_time - new_started)) / 60)::integer));
        end if;
    elsif p_action = 'reopen' and step_row.status = 'completed' then
        new_status := 'active'; new_completed := null; new_actual := null;
    elsif p_action = 'reopen' and step_row.status = 'active' then
        -- The FRD's started -> not_started undo transition.
        new_status := 'pending'; new_started := null; new_completed := null; new_actual := null;
    else
        return pg_catalog.jsonb_build_object(
            'state', 'invalid_transition', 'current', public.get_my_step(p_step_id)
        );
    end if;

    perform pg_catalog.set_config('kickoff.step_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.step_lifecycle_write', 'on', true);
    perform pg_catalog.set_config('kickoff.task_api_write', 'on', true);
    perform pg_catalog.set_config('kickoff.task_lifecycle_write', 'on', true);
    update public.steps set status = new_status, started_at = new_started,
        completed_at = new_completed, actual_minutes = new_actual,
        version = version + 1, updated_at = action_time
    where id = p_step_id and user_id = caller_id;

    select state.points_total, state.plant_stage, state.current_streak,
           state.best_streak, state.freezes_held, state.streak_status,
           state.last_active_local_date
    into point_total, stage, streak, best, freezes, streak_state, last_date
    from public.reward_state as state where state.user_id = caller_id for update;
    previous_plant_stage := stage;
    select config.start_points, config.completion_points, config.plant_thresholds
    into cfg_start, cfg_complete, thresholds from public.reward_config as config
    where config.config_key = 'default';

    -- Append only first-start and first-completion point awards. Active-day
    -- events are separately deduped by user/date, so a second task on the same
    -- day cannot inflate the streak.
    if p_action = 'start' or (p_action = 'complete' and step_row.status = 'pending') then
        if not exists (select 1 from public.reward_events e where e.user_id = caller_id
                       and e.dedupe_key = 'step_started:' || step_row.id::text) then
            point_total := point_total + cfg_start; awarded := awarded + cfg_start;
            key_list := array_append(key_list, 'step_started:' || step_row.id::text);
            type_list := array_append(type_list, 'step_started');
            point_list := array_append(point_list, cfg_start);
            step_list := array_append(step_list, step_row.id);
            date_list := array_append(date_list, action_date);
        end if;
    end if;
    if p_action = 'complete' and not exists (
        select 1 from public.reward_events e where e.user_id = caller_id
        and e.dedupe_key = 'step_completed:' || step_row.id::text
    ) then
        point_total := point_total + cfg_complete; awarded := awarded + cfg_complete;
        -- Replace the zero-valued marker queued above with the configured award.
        key_list := array_append(key_list, 'step_completed:' || step_row.id::text);
        type_list := array_append(type_list, 'step_completed');
        point_list := array_append(point_list, cfg_complete);
        step_list := array_append(step_list, step_row.id);
        date_list := array_append(date_list, action_date);
    end if;

    -- Evaluate streak only once for each active local date. A date marker is
    -- an append-only step_started event and has no points.
    if (p_action = 'start' or (p_action = 'complete' and step_row.status = 'pending'))
       and not exists (select 1 from public.reward_events e where e.user_id = caller_id
                       and e.dedupe_key = 'active_day:' || action_date::text) then
        -- Timezone changes can make the newly computed local date earlier
        -- than a date stored under the previous timezone. Keep historical
        -- streak state monotonic in that case.
        if last_date is null or action_date > last_date then
          if last_date is not null then
            gap_days := greatest(0, action_date - last_date - 1);
            if gap_days > 0 then
                covered := least(gap_days, freezes);
                for ix in 1..covered loop
                    freezes := freezes - 1;
                    key_list := array_append(key_list,
                        'freeze_used:' || (last_date + ix)::text);
                    type_list := array_append(type_list, 'freeze_used');
                    point_list := array_append(point_list, 0);
                    step_list := array_append(step_list, step_row.id);
                    date_list := array_append(date_list, last_date + ix);
                end loop;
                if gap_days > covered then
                    best := greatest(best, streak); streak := 0; streak_state := 'ended';
                    key_list := array_append(key_list, 'streak_ended:' || action_date::text);
                    type_list := array_append(type_list, 'streak_ended');
                    point_list := array_append(point_list, 0);
                    step_list := array_append(step_list, step_row.id);
                    date_list := array_append(date_list, action_date);
                else
                    streak_state := 'frozen';
                end if;
            end if;
          end if;
          if last_date is null or streak_state in ('none', 'ended') then
              streak := 1;
          else
              streak := streak + 1;
          end if;
          best := greatest(best, streak); streak_state := 'active'; last_date := action_date;
          if streak > 0 and streak % 7 = 0 and freezes < 3 then
              freezes := freezes + 1;
              key_list := array_append(key_list, 'freeze_earned:' || action_date::text);
              type_list := array_append(type_list, 'freeze_earned');
              point_list := array_append(point_list, 0);
              step_list := array_append(step_list, step_row.id);
              date_list := array_append(date_list, action_date);
          end if;
        end if;
        key_list := array_append(key_list, 'active_day:' || action_date::text);
        type_list := array_append(type_list, 'step_started');
        point_list := array_append(point_list, 0);
        step_list := array_append(step_list, step_row.id);
        date_list := array_append(date_list, action_date);
    end if;

    -- Keep task completion derived from all non-deleted steps. Empty tasks are
    -- not auto-completed. Reopening any step returns a completed task to active.
    select count(*) > 0 and bool_and(status = 'completed') into is_done
    from public.steps where task_id = step_row.task_id and user_id = caller_id
      and deleted_at is null;
    if is_done and task_row.status <> 'completed' then
        update public.tasks set status = 'completed', completed_at = action_time,
            version = version + 1, updated_at = action_time
        where id = step_row.task_id and user_id = caller_id;
        key_list := array_append(key_list, 'task_completed:' || task_row.id::text);
        type_list := array_append(type_list, 'task_completed');
        point_list := array_append(point_list, 0);
        step_list := array_append(step_list, step_row.id);
        date_list := array_append(date_list, action_date);
    elsif not is_done and task_row.status = 'completed' then
        update public.tasks set status = 'active', completed_at = null,
            version = version + 1, updated_at = action_time
        where id = step_row.task_id and user_id = caller_id;
    else
        update public.tasks set version = version + 1, updated_at = action_time
        where id = step_row.task_id and user_id = caller_id;
    end if;

    stage := 0;
    foreach ix in array thresholds loop
        if point_total >= ix then stage := stage + 1; end if;
    end loop;
    stage := greatest(0, stage - 1);

    perform pg_catalog.set_config('kickoff.reward_event_write', 'on', true);
    for ix in 1..coalesce(array_length(key_list, 1), 0) loop
        event_json := pg_catalog.jsonb_build_object(
            'rule_version', 1, 'freezes_after', freezes, 'streak_after', streak,
            'points_after', point_total, 'plant_stage_after', stage,
            'current_streak_after', streak, 'best_streak_after', best,
            'streak_status_after', streak_state,
            'last_active_local_date_after', last_date
        );
        insert into public.reward_events(
            user_id, task_id, step_id, event_type, points_delta, streak_delta,
            event_data, dedupe_key, local_date, created_at
        ) values (
            caller_id, task_row.id, step_list[ix], type_list[ix], point_list[ix],
            0, event_json, key_list[ix], date_list[ix], action_time
        ) on conflict (user_id, dedupe_key) do nothing;
    end loop;
    perform pg_catalog.set_config('kickoff.reward_state_write', 'on', true);
    update public.reward_state set points_total = point_total, plant_stage = stage,
        current_streak = streak, best_streak = best, freezes_held = freezes,
        streak_status = streak_state, last_active_local_date = last_date,
        updated_at = action_time where user_id = caller_id;

    step_json := public.get_my_step(p_step_id);
    select pg_catalog.jsonb_build_object(
        'id', task.id, 'status', task.status, 'completed_at', task.completed_at,
        'version', task.version, 'updated_at', task.updated_at
    ) into task_json from public.tasks task
    where task.id = task_row.id and task.user_id = caller_id;
    select pg_catalog.jsonb_build_object(
        'points_total', state.points_total, 'plant_stage', state.plant_stage,
        'current_streak', state.current_streak, 'best_streak', state.best_streak,
        'freezes_held', state.freezes_held, 'streak_status', state.streak_status,
        'last_active_local_date', state.last_active_local_date
    ) into state_json from public.reward_state state where state.user_id = caller_id;
    response_json := pg_catalog.jsonb_build_object(
        'step', step_json, 'task', task_json,
        'reward_delta', pg_catalog.jsonb_build_object(
            'points_awarded', awarded, 'points_total', point_total,
            'plant_stage', stage, 'previous_plant_stage', previous_plant_stage,
            'plant_stage_changed', stage <> previous_plant_stage,
            'current_streak', streak,
            'best_streak', best, 'freezes_held', freezes,
            'streak_status', streak_state, 'last_active_local_date', last_date
        ),
        'reward_state', state_json
    );
    -- Store the original success body before this RPC transaction commits.
    -- A lost HTTP response can therefore be safely replayed after a crash.
    perform public.complete_api_idempotency(
        p_idempotency_key, p_request_hash, p_lease_token, 200, response_json
    );
    return response_json;
end;
$function$;
revoke all on function public.act_on_my_step(uuid, text, integer, integer, text, text, uuid) from public, anon;
grant execute on function public.act_on_my_step(uuid, text, integer, integer, text, text, uuid) to authenticated;

commit;
