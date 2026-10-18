create index concurrently if not exists voters_campaign_valid_phone_idx
on public.voters (campaign_id)
where is_deleted = false and phone ~ '^\s*(\+?\s*91\s*)?[6-9](\s*[0-9]){9}\s*$';
