create index concurrently if not exists voters_campaign_religion_idx
on public.voters (campaign_id, religion_id, community_id)
where religion_id is not null and is_deleted = false;
