create table if not exists public.users (
    id uuid primary key references auth.users(id) on delete cascade,
    display_name text null check (display_name is null or char_length(display_name) between 1 and 60),
    language text not null default 'en' check (char_length(language) between 2 and 12),
    timezone text not null default 'Africa/Lagos' check (char_length(timezone) between 1 and 100),
    settings jsonb not null default '{}'::jsonb,
    last_seen_at timestamptz null,
    tokens_valid_after timestamptz null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz null,
    constraint users_settings_object check (jsonb_typeof(settings) = 'object'),
    constraint users_settings_keys check (
        settings - array[
            'calm_mode',
            'sound_enabled',
            'streak_visible',
            'reminders_enabled',
            'daily_reminder_cap',
            'quiet_hours_start',
            'quiet_hours_end',
            'show_task_in_notification',
            'detail_level_default'
        ] = '{}'::jsonb
    ),
    constraint users_settings_values check (
        (not (settings ? 'calm_mode') or jsonb_typeof(settings -> 'calm_mode') = 'boolean')
        and (not (settings ? 'sound_enabled') or jsonb_typeof(settings -> 'sound_enabled') = 'boolean')
        and (not (settings ? 'streak_visible') or jsonb_typeof(settings -> 'streak_visible') = 'boolean')
        and (not (settings ? 'reminders_enabled') or jsonb_typeof(settings -> 'reminders_enabled') = 'boolean')
        and (not (settings ? 'show_task_in_notification') or jsonb_typeof(settings -> 'show_task_in_notification') = 'boolean')
        and (not (settings ? 'daily_reminder_cap') or (settings ->> 'daily_reminder_cap') ~ '^(0|[1-6])$')
        and (not (settings ? 'detail_level_default') or (settings ->> 'detail_level_default') ~ '^[1-5]$')
        and (not (settings ? 'quiet_hours_start') or (settings ->> 'quiet_hours_start') ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$')
        and (not (settings ? 'quiet_hours_end') or (settings ->> 'quiet_hours_end') ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$')
    )
);

create table if not exists public.reward_state (
    user_id uuid primary key references public.users(id) on delete cascade,
    points_total integer not null default 0 check (points_total >= 0),
    plant_stage smallint not null default 0 check (plant_stage >= 0),
    current_streak integer not null default 0 check (current_streak >= 0),
    best_streak integer not null default 0 check (best_streak >= 0),
    freezes_held smallint not null default 0 check (freezes_held between 0 and 3),
    streak_status text not null default 'none'
        check (streak_status in ('none', 'active', 'frozen', 'ended')),
    last_active_local_date date null,
    updated_at timestamptz not null default now()
);

alter table public.users enable row level security;
alter table public.reward_state enable row level security;

grant usage on schema public to authenticated;

drop policy if exists users_select_self on public.users;
create policy users_select_self on public.users
    for select to authenticated
    using (id = (select auth.uid()));

drop policy if exists users_insert_self on public.users;
create policy users_insert_self on public.users
    for insert to authenticated
    with check (id = (select auth.uid()));

drop policy if exists users_update_self on public.users;
create policy users_update_self on public.users
    for update to authenticated
    using (id = (select auth.uid()))
    with check (id = (select auth.uid()));

drop policy if exists reward_state_select_self on public.reward_state;
create policy reward_state_select_self on public.reward_state
    for select to authenticated
    using (user_id = (select auth.uid()));

drop policy if exists reward_state_insert_self on public.reward_state;
create policy reward_state_insert_self on public.reward_state
    for insert to authenticated
    with check (user_id = (select auth.uid()));

revoke all on table public.users from anon, authenticated;
revoke all on table public.reward_state from anon, authenticated;

grant select (
    id, display_name, language, timezone, settings,
    last_seen_at, tokens_valid_after, created_at, updated_at, deleted_at
) on table public.users to authenticated;
grant insert (id) on table public.users to authenticated;
grant update (
    display_name, language, timezone, settings, updated_at
) on table public.users to authenticated;

grant select (
    user_id, points_total, plant_stage, current_streak,
    best_streak, freezes_held, streak_status,
    last_active_local_date, updated_at
) on table public.reward_state to authenticated;
grant insert (user_id) on table public.reward_state to authenticated;

create or replace function public.ensure_user_profile()
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    caller_id uuid := auth.uid();
    result jsonb;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;

    insert into public.users (id)
    values (caller_id)
    on conflict (id) do nothing;

    insert into public.reward_state (user_id)
    values (caller_id)
    on conflict (user_id) do nothing;

    select jsonb_build_object(
        'id', u.id,
        'display_name', u.display_name,
        'language', u.language,
        'timezone', u.timezone,
        'settings', u.settings,
        'created_at', u.created_at,
        'updated_at', u.updated_at,
        'deleted_at', u.deleted_at,
        'tokens_valid_after', u.tokens_valid_after
    )
    into result
    from public.users as u
    where u.id = caller_id;

    return result;
end;
$function$;

create or replace function public.patch_my_profile(p_patch jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    caller_id uuid := auth.uid();
    result jsonb;
begin
    if caller_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;

    if p_patch is null or jsonb_typeof(p_patch) <> 'object' then
        raise exception using errcode = '22023', message = 'profile patch must be an object';
    end if;

    update public.users as u
    set
        display_name = case
            when p_patch ? 'display_name' then p_patch ->> 'display_name'
            else u.display_name
        end,
        language = case
            when p_patch ? 'language' then p_patch ->> 'language'
            else u.language
        end,
        timezone = case
            when p_patch ? 'timezone' then p_patch ->> 'timezone'
            else u.timezone
        end,
        settings = u.settings || coalesce(p_patch -> 'settings', '{}'::jsonb),
        updated_at = now()
    where u.id = caller_id
      and u.deleted_at is null
    returning jsonb_build_object(
        'id', u.id,
        'display_name', u.display_name,
        'language', u.language,
        'timezone', u.timezone,
        'settings', u.settings,
        'created_at', u.created_at,
        'updated_at', u.updated_at,
        'deleted_at', u.deleted_at,
        'tokens_valid_after', u.tokens_valid_after
    )
    into result;

    return result;
end;
$function$;

revoke all on function public.ensure_user_profile() from public, anon;
revoke all on function public.patch_my_profile(jsonb) from public, anon;
grant execute on function public.ensure_user_profile() to authenticated;
grant execute on function public.patch_my_profile(jsonb) to authenticated;
