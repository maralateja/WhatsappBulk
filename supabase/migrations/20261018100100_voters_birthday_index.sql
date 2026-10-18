create index concurrently if not exists voters_campaign_birthday_idx
on public.voters (campaign_id, (extract(month from birth_date)::int), (extract(day from birth_date)::int))
where is_deleted = false;
