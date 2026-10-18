create index concurrently if not exists voters_campaign_booth_idx
on public.voters (campaign_id, booth_number)
where is_deleted = false;
