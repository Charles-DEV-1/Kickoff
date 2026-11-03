-- Stage 5 template metadata and variant schema.
-- Apply after 202611020004_step_actions_rewards.sql. This migration adds no
-- student-facing template copy; reviewed content must be supplied separately.

create or replace function public.valid_template_variants(p_variants jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = ''
as $function$
declare
    variant_name text;
    required_count integer;
    variant_items jsonb;
    item jsonb;
    item_text text;
    minutes_text text;
    minutes_value integer;
    item_index integer;
begin
    if p_variants is null or pg_catalog.jsonb_typeof(p_variants) <> 'object'
       or not (p_variants ?& array['short', 'standard', 'detailed'])
       or p_variants - array['short', 'standard', 'detailed'] <> '{}'::jsonb then
        return false;
    end if;

    foreach variant_name in array array['short', 'standard', 'detailed'] loop
        variant_items := p_variants -> variant_name;
        if pg_catalog.jsonb_typeof(variant_items) <> 'array' then return false; end if;
        required_count := case variant_name
            when 'short' then 5
            when 'standard' then 8
            else 18
        end;
        if pg_catalog.jsonb_array_length(variant_items) < required_count
           or pg_catalog.jsonb_array_length(variant_items) > 30 then
            return false;
        end if;

        item_index := 0;
        for item in select value from pg_catalog.jsonb_array_elements(variant_items) as rows(value)
        loop
            if pg_catalog.jsonb_typeof(item) <> 'object'
               or not (item ?& array['text', 'planned_minutes'])
               or item - array['text', 'planned_minutes'] <> '{}'::jsonb
               or pg_catalog.jsonb_typeof(item -> 'text') <> 'string'
               or pg_catalog.jsonb_typeof(item -> 'planned_minutes') <> 'number' then
                return false;
            end if;
            item_text := item ->> 'text';
            minutes_text := item ->> 'planned_minutes';
            if item_text is null or pg_catalog.btrim(item_text) = ''
               or pg_catalog.char_length(item_text) not between 1 and 280
               or minutes_text !~ '^[0-9]+$' then
                return false;
            end if;
            minutes_value := minutes_text::integer;
            if minutes_value < 1 or minutes_value > 120
               or (item_index = 0 and minutes_value > 10) then
                return false;
            end if;
            item_index := item_index + 1;
        end loop;
    end loop;
    return true;
end;
$function$;

revoke all on function public.valid_template_variants(jsonb) from public, anon, authenticated;
grant execute on function public.valid_template_variants(jsonb) to anon, authenticated;

create or replace function public.valid_template_keywords(p_keywords text[])
returns boolean
language plpgsql
immutable
security invoker
set search_path = ''
as $function$
declare
    keyword text;
begin
    if p_keywords is null or pg_catalog.cardinality(p_keywords) > 50
       or pg_catalog.array_position(p_keywords, null) is not null then
        return false;
    end if;
    foreach keyword in array p_keywords loop
        if pg_catalog.btrim(keyword) = '' or pg_catalog.char_length(keyword) > 80 then
            return false;
        end if;
    end loop;
    return true;
end;
$function$;
revoke all on function public.valid_template_keywords(text[]) from public, anon, authenticated;
grant execute on function public.valid_template_keywords(text[]) to anon, authenticated;

alter table public.templates
    add column if not exists keywords text[] not null default '{}'::text[],
    add column if not exists variants jsonb not null
        default '{"short":[],"standard":[],"detailed":[]}'::jsonb;

alter table public.templates drop constraint if exists templates_task_type_check;
alter table public.templates add constraint templates_task_type_check check (
    task_type in (
        'essay', 'lab_report', 'exam_revision', 'group_project', 'reading',
        'presentation', 'assignment', 'generic'
    )
) not valid;

alter table public.templates drop constraint if exists templates_keywords_valid;
alter table public.templates add constraint templates_keywords_valid check (
    public.valid_template_keywords(keywords)
) not valid;

alter table public.templates drop constraint if exists templates_variants_valid;
alter table public.templates add constraint templates_variants_valid check (
    public.valid_template_variants(variants)
) not valid;

create index if not exists templates_active_keywords_idx
    on public.templates using gin (keywords)
    where is_active;

create or replace function public.bump_template_version()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    if new.template_key is distinct from old.template_key
       or new.task_type is distinct from old.task_type
       or new.language is distinct from old.language
       or new.title is distinct from old.title
       or new.description is distinct from old.description
       or new.keywords is distinct from old.keywords
       or new.variants is distinct from old.variants
       or new.suggested_steps is distinct from old.suggested_steps then
        new.version := old.version + 1;
        new.updated_at := pg_catalog.clock_timestamp();
    end if;
    return new;
end;
$function$;
revoke all on function public.bump_template_version() from public, anon, authenticated;
drop trigger if exists templates_version_bump on public.templates;
create trigger templates_version_bump
    before update on public.templates
    for each row execute function public.bump_template_version();

-- Public catalog rows are read-only to anon/authenticated clients. Existing
-- Stage 2 RLS and grants expose only active template records.
alter table public.templates enable row level security;

