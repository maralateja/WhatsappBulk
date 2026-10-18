create index concurrently if not exists voters_campaign_valid_phone_idx
on public.voters (campaign_id)
where is_deleted = false and phone ~ '^(\+?91)?[6-9][0-9]{9}$';
