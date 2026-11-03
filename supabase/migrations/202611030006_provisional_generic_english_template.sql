-- PROVISIONAL STUDENT-FACING COPY: requested for MVP testing, pending team review.
-- Apply once after 202611030005_public_templates.sql. Use a reviewed forward
-- migration to replace this copy later; the template version trigger bumps the
-- version when content is updated.

do $seed$
declare
    existing_id uuid;
    existing_type text;
    existing_title text;
    existing_description text;
    existing_keywords text[];
    existing_variants jsonb;
    provisional_variants jsonb := '{
      "short": [
        {"text":"Describe the finished result in one sentence.","planned_minutes":5},
        {"text":"Gather the instructions and materials you need.","planned_minutes":10},
        {"text":"Choose the smallest part you can do first.","planned_minutes":5},
        {"text":"Work on that part until it is finished.","planned_minutes":20},
        {"text":"Check what is done and write down the next action.","planned_minutes":5}
      ],
      "standard": [
        {"text":"Write one sentence describing what finished looks like.","planned_minutes":5},
        {"text":"Find the instructions, notes, or reference materials.","planned_minutes":10},
        {"text":"Gather the tools and materials you will need.","planned_minutes":10},
        {"text":"Split the task into a few smaller parts.","planned_minutes":10},
        {"text":"Choose the smallest part with a clear next action.","planned_minutes":5},
        {"text":"Work on that part for one focused block.","planned_minutes":25},
        {"text":"Check it against the instructions or intended result.","planned_minutes":10},
        {"text":"Save your progress and write down the next action.","planned_minutes":5}
      ],
      "detailed": [
        {"text":"Write one sentence describing what finished looks like.","planned_minutes":5},
        {"text":"Find and read the instructions or task details.","planned_minutes":10},
        {"text":"List the main things the finished result needs to include.","planned_minutes":10},
        {"text":"Gather the tools, notes, and materials you will need.","planned_minutes":10},
        {"text":"Open or prepare the place where you will work.","planned_minutes":5},
        {"text":"Divide the task into three to five smaller parts.","planned_minutes":10},
        {"text":"Put the parts in an order that makes sense.","planned_minutes":10},
        {"text":"Choose the smallest part with a clear first action.","planned_minutes":5},
        {"text":"Decide what will count as finishing that first part.","planned_minutes":5},
        {"text":"Do the first action for that part.","planned_minutes":15},
        {"text":"Continue working on the first part.","planned_minutes":20},
        {"text":"Pause and check what is complete so far.","planned_minutes":5},
        {"text":"Finish the current part and save it.","planned_minutes":15},
        {"text":"Choose the next part and identify its first action.","planned_minutes":10},
        {"text":"Work through the remaining parts one at a time.","planned_minutes":25},
        {"text":"Put the finished parts together or organize them.","planned_minutes":10},
        {"text":"Check the result against the instructions or intended outcome.","planned_minutes":10},
        {"text":"Make any needed changes, save your work, and note what is next.","planned_minutes":15}
      ]
    }'::jsonb;
    provisional_keywords text[] := array['task', 'assignment', 'project', 'study', 'school', 'work', 'general'];
begin
    select template.id, template.task_type, template.title, template.description,
           template.keywords, template.variants
    into existing_id, existing_type, existing_title, existing_description,
         existing_keywords, existing_variants
    from public.templates as template
    where template.template_key = 'generic_task_en'
      and template.language = 'en'
    order by template.version desc
    limit 1
    for update;

    if existing_id is null then
        insert into public.templates (
            template_key, task_type, language, version, title, description,
            keywords, variants, is_active
        ) values (
            'generic_task_en', 'generic', 'en', 1,
            'A general task',
            'A flexible starting plan for breaking a task into smaller actions.',
            provisional_keywords, provisional_variants, true
        );
    elsif existing_type is distinct from 'generic'
       or existing_title is distinct from 'A general task'
       or existing_description is distinct from 'A flexible starting plan for breaking a task into smaller actions.'
       or existing_keywords is distinct from provisional_keywords
       or existing_variants is distinct from provisional_variants then
        update public.templates
        set task_type = 'generic',
            title = 'A general task',
            description = 'A flexible starting plan for breaking a task into smaller actions.',
            keywords = provisional_keywords,
            variants = provisional_variants,
            is_active = true
        where id = existing_id;
    else
        update public.templates
        set is_active = true
        where id = existing_id and not is_active;
    end if;
end;
$seed$;
