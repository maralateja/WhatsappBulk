create index concurrently if not exists voters_campaign_has_phone_idx
on public.voters (campaign_id)
where is_deleted = false and phone is not null and phone <> '';
