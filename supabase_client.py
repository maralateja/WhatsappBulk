"""Thin read-only Supabase (PostgREST) client for the admin tool."""

import os
import re
import time

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

VOTER_COLUMNS = (
    "id,epic_id,serial_number,name,name_local,age,gender,birth_date,phone,booth_number,section_name,"
    "house_no,address,relation_name,religion_id,community_id"
)

# View adding birth_month/birth_day to voters; see supabase/migrations.
BIRTHDAY_VIEW = "voters_with_birthday"

# 10-digit Indian mobile starting 6-9, optionally prefixed with +91 / 91; spaces are ignored.
# Must match the partial index in supabase/migrations/*_voters_valid_phone_index_spaces.sql.
VALID_MOBILE_REGEX = r"^\s*(\+?\s*91\s*)?[6-9](\s*[0-9]){9}\s*$"


def is_valid_mobile(phone) -> bool:
    return bool(phone) and re.fullmatch(VALID_MOBILE_REGEX, str(phone).strip()) is not None


def _base_url() -> str:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL is not set in .env")
    return f"{url}/rest/v1"


def _headers(count: bool = False) -> dict:
    key = os.getenv("SUPABASE_SECRET_KEY", "")
    if not key:
        raise RuntimeError("SUPABASE_SECRET_KEY is not set in .env")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept-Profile": os.getenv("SUPABASE_SCHEMA", "public"),
    }
    if count:
        headers["Prefer"] = "count=exact"
    return headers


def _get(table: str, params, count: bool = False, retries: int = 2):
    """params may be a dict or a list of (key, value) tuples (repeated keys AND together)."""
    for attempt in range(retries + 1):
        resp = requests.get(
            f"{_base_url()}/{table}", params=params, headers=_headers(count), timeout=90
        )
        # Large filtered scans intermittently hit the DB statement timeout.
        if resp.status_code >= 500 and attempt < retries:
            time.sleep(1.5)
            continue
        resp.raise_for_status()
        break
    total = None
    if count:
        total = int(resp.headers.get("Content-Range", "*/0").split("/")[-1])
    return resp.json(), total


def fetch_campaigns() -> list[dict]:
    rows, _ = _get(
        "campaigns",
        {
            "select": "id,name,status",
            "is_deleted": "eq.false",
            "order": "created_at.desc",
        },
    )
    for r in rows:
        # Some campaign names contain line breaks and padding.
        r["name"] = " ".join((r.get("name") or "").split())
    return rows


def fetch_booths(campaign_id: str) -> list[str]:
    rows, _ = _get(
        "geo_units",
        {
            "select": "booth_number",
            "campaign_id": f"eq.{campaign_id}",
            "is_booth": "eq.true",
            "is_deleted": "eq.false",
            "limit": 5000,
        },
    )
    booths = {r["booth_number"] for r in rows if r.get("booth_number")}
    return sorted(booths, key=lambda b: (0, int(b), b) if b.isdigit() else (1, 0, b))


def fetch_booth_names(campaign_id: str) -> dict[str, str]:
    """Booth number -> booth name for a campaign."""
    rows, _ = _get(
        "geo_units",
        {
            "select": "booth_number,name",
            "campaign_id": f"eq.{campaign_id}",
            "is_booth": "eq.true",
            "is_deleted": "eq.false",
            "limit": 5000,
        },
    )
    return {r["booth_number"]: (r.get("name") or "").strip() for r in rows if r.get("booth_number")}


def fetch_religions() -> list[dict]:
    rows, _ = _get("religion", {"select": "id,name", "is_active": "eq.true", "order": "display_order"})
    return [{"id": r["id"], "name": r["name"].strip()} for r in rows]


def fetch_communities(religion_id: int | None = None) -> list[dict]:
    params = {"select": "id,name", "is_active": "eq.true", "order": "display_order"}
    if religion_id:
        params["religion_id"] = f"eq.{religion_id}"
    rows, _ = _get("community", params)
    return rows


def _voter_table(f: dict | None) -> str:
    return BIRTHDAY_VIEW if f and f.get("birth_month") else "voters"


def voter_params(campaign_id: str, f: dict | None = None) -> list[tuple]:
    """Translate UI filters into PostgREST query params."""
    f = f or {}
    p = [("campaign_id", f"eq.{campaign_id}"), ("is_deleted", "eq.false")]
    if f.get("gender"):
        p.append(("gender", f"eq.{f['gender']}"))
    if f.get("age_min") is not None:
        p.append(("age", f"gte.{f['age_min']}"))
    if f.get("age_max") is not None:
        p.append(("age", f"lte.{f['age_max']}"))
    if f.get("birth_month"):
        p.append(("birth_month", f"eq.{f['birth_month']}"))
        p.append(("birth_day", f"eq.{f['birth_day']}"))
    if f.get("booths"):
        p.append(("booth_number", f"in.({','.join(f['booths'])})"))
    if f.get("religion_id"):
        p.append(("religion_id", f"eq.{f['religion_id']}"))
    if f.get("community_id"):
        p.append(("community_id", f"eq.{f['community_id']}"))
    if f.get("has_phone"):
        p.append(("phone", "not.is.null"))
        p.append(("phone", "neq."))
    if f.get("valid_phone"):
        # 10-digit Indian mobile starting 6-9, optionally prefixed with +91 / 91.
        p.append(("phone", f"match.{VALID_MOBILE_REGEX}"))
    return p


def count_voters(campaign_id: str, f: dict | None = None) -> int:
    params = voter_params(campaign_id, f) + [("select", "id"), ("limit", 1)]
    _, total = _get(_voter_table(f), params, count=True)
    return total


def fetch_voters(campaign_id: str, f: dict | None, offset: int, limit: int) -> list[dict]:
    params = voter_params(campaign_id, f) + [
        ("select", VOTER_COLUMNS),
        ("order", "serial_number.asc.nullslast,id.asc"),
        ("offset", offset),
        ("limit", limit),
    ]
    rows, _ = _get(_voter_table(f), params)
    return rows


SEND_COLUMNS = "id,name,name_local,epic_id,serial_number,age,booth_number,section_name,phone"


def iter_voters_for_send(campaign_id: str, f: dict | None, page_size: int = 1000):
    """Yield every matching voter in id order (keyset pagination, stable for large result sets)."""
    last = None
    while True:
        params = voter_params(campaign_id, f) + [("select", SEND_COLUMNS), ("order", "id.asc"), ("limit", page_size)]
        if last:
            params.append(("id", f"gt.{last}"))
        rows, _ = _get(_voter_table(f), params)
        yield from rows
        if len(rows) < page_size:
            return
        last = rows[-1]["id"]


def fetch_voters_by_ids(ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 100):
        part = ",".join(ids[i : i + 100])
        rows, _ = _get("voters", {"select": SEND_COLUMNS, "id": f"in.({part})", "limit": 100})
        out += rows
    return out
