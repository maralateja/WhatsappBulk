-- View exposing birth month/day so the app can filter birthdays regardless of year.
-- Read-only; does not modify public.voters.
create or replace view public.voters_with_birthday
with (security_invoker = true) as
select
    v.*,
    extract(month from v.birth_date)::int as birth_month,
    extract(day from v.birth_date)::int as birth_day
from public.voters v;

grant select on public.voters_with_birthday to service_role;

notify pgrst, 'reload schema';
