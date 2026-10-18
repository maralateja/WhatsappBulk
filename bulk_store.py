"""Supabase writes/reads for bulk-send jobs (wa_* tables). Calls retry until the network is back."""

import json
import time
from datetime import datetime, timedelta, timezone

import requests

import supabase_client as db

CHUNK = 1000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request(method: str, table: str, *, params=None, body=None, prefer: str | None = None, retries: int = 3):
    """One PostgREST call. Raises requests.RequestException after `retries` network/5xx failures."""
    headers = {**db._headers(), "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    last = None
    for attempt in range(retries):
        try:
            resp = requests.request(
                method, f"{db._base_url()}/{table}", params=params, data=json.dumps(body) if body is not None else None,
                headers=headers, timeout=60,
            )
        except requests.RequestException as exc:
            last = exc
        else:
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
            last = requests.HTTPError(f"{resp.status_code} {resp.text[:200]}", response=resp)
        time.sleep(2 * (attempt + 1))
    raise last


def forever(fn, *, on_wait=None, stop=lambda: False, max_wait: int = 300):
    """Run fn until it succeeds, backing off while the network/database is unreachable."""
    wait = 5
    while True:
        try:
            return fn()
        except requests.RequestException as exc:
            if getattr(exc, "response", None) is not None and exc.response.status_code < 500:
                raise  # a real rejection, not connectivity
            if on_wait:
                on_wait(wait)
            for _ in range(wait):
                if stop():
                    raise
                time.sleep(1)
            wait = min(wait * 2, max_wait)


# ---- settings / lease -------------------------------------------------------------------------

def get_settings() -> dict:
    rows = _request("GET", "wa_settings", params={"select": "daily_limit,batch_size", "id": "eq.1"}).json()
    return rows[0]


def update_settings(daily_limit: int | None = None, batch_size: int | None = None, **fields) -> None:
    if daily_limit is not None:
        fields["daily_limit"] = daily_limit
    if batch_size is not None:
        fields["batch_size"] = batch_size
    _request("PATCH", "wa_settings", params={"id": "eq.1"}, body={**fields, "updated_at": _now()})


def acquire_lease(owner: str, seconds: int = 60) -> bool:
    """Only one sender process may run at a time; the lease makes a second one idle."""
    now = _now()
    until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    resp = _request(
        "PATCH", "wa_settings",
        params={"id": "eq.1", "or": f"(lease_owner.eq.{owner},lease_until.is.null,lease_until.lt.{now})"},
        body={"lease_owner": owner, "lease_until": until}, prefer="return=representation",
    )
    return bool(resp.json())


# ---- jobs -------------------------------------------------------------------------------------

def create_job(campaign_id: str, name: str, template: dict, header_image_url: str | None, status: str = "running") -> dict:
    resp = _request(
        "POST", "wa_jobs",
        body={"campaign_id": campaign_id, "name": name, "template": template, "header_image_url": header_image_url,
              "status": status},
        prefer="return=representation",
    )
    return resp.json()[0]


def get_job(job_id: str) -> dict | None:
    rows = _request("GET", "wa_jobs", params={"id": f"eq.{job_id}"}).json()
    return rows[0] if rows else None


def list_jobs(limit: int = 10) -> list[dict]:
    return _request("GET", "wa_jobs", params={"order": "created_at.desc", "limit": limit}).json()


def next_running_job() -> dict | None:
    rows = _request("GET", "wa_jobs", params={"status": "eq.running", "order": "created_at.asc", "limit": 1}).json()
    return rows[0] if rows else None


def active_job_exists() -> bool:
    _, total = db._get("wa_jobs", {"select": "id", "status": "eq.running", "limit": 1}, count=True)
    return total > 0


def set_job(job_id: str, **fields) -> None:
    _request("PATCH", "wa_jobs", params={"id": f"eq.{job_id}"}, body={**fields, "updated_at": _now()})


def job_stats(job_id: str) -> dict:
    resp = _request("POST", "rpc/wa_job_stats", body={"p_job": job_id})
    return resp.json() or {}


# ---- recipients -------------------------------------------------------------------------------

def insert_recipients(job_id: str, rows: list[dict], on_progress=None) -> None:
    for i in range(0, len(rows), CHUNK):
        chunk = [{**r, "job_id": job_id} for r in rows[i : i + CHUNK]]
        _request("POST", "wa_job_recipients", params={"on_conflict": "job_id,voter_id"}, body=chunk,
                 prefer="resolution=ignore-duplicates,return=minimal")
        if on_progress:
            on_progress(min(i + CHUNK, len(rows)), len(rows))


def next_pending(job_id: str, limit: int) -> list[dict]:
    return _request(
        "GET", "wa_job_recipients",
        params={"job_id": f"eq.{job_id}", "status": "eq.pending", "order": "id.asc", "limit": limit,
                "select": "id,phone,variables"},
    ).json()


def sending_rows(job_id: str) -> list[dict]:
    return _request(
        "GET", "wa_job_recipients",
        params={"job_id": f"eq.{job_id}", "status": "eq.sending", "select": "id,phone,variables,updated_at", "limit": 5000},
    ).json()


def update_rows(ids: list[int], **fields) -> None:
    for i in range(0, len(ids), 100):
        part = ",".join(str(x) for x in ids[i : i + 100])
        _request("PATCH", "wa_job_recipients", params={"id": f"in.({part})"}, body={**fields, "updated_at": _now()})


def update_where(job_id: str, from_status: str, **fields) -> None:
    _request("PATCH", "wa_job_recipients", params={"job_id": f"eq.{job_id}", "status": f"eq.{from_status}"},
             body={**fields, "updated_at": _now()})


def requeue_failed(job_id: str) -> None:
    _request("PATCH", "wa_job_recipients", params={"job_id": f"eq.{job_id}", "status": "eq.failed"},
             body={"status": "pending", "fail_reason": None, "error": None, "updated_at": _now()})


def sent_last_24h() -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    _, total = db._get("wa_job_recipients", {"select": "id", "sent_at": f"gte.{since}", "limit": 1}, count=True)
    return total


def trackable_rows(since_hours: int = 72) -> list[dict]:
    """Submitted/delivered rows still awaiting a final delivery state."""
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    out, offset = [], 0
    while True:
        rows = _request(
            "GET", "wa_job_recipients",
            params={"status": "in.(submitted,delivered)", "sent_at": f"gte.{since}", "select": "id,phone,status,sent_at",
                    "order": "id.asc", "limit": 1000, "offset": offset},
        ).json()
        out += rows
        if len(rows) < 1000:
            return out
        offset += 1000


def report_rows(job_id: str):
    """Yield every recipient of a job (for the CSV report)."""
    offset = 0
    while True:
        rows = _request(
            "GET", "wa_job_recipients",
            params={"job_id": f"eq.{job_id}", "order": "id.asc", "limit": 1000, "offset": offset,
                    "select": "voter_name,phone,status,fail_reason,error,sent_at,delivered_at,read_at"},
        ).json()
        yield from rows
        if len(rows) < 1000:
            return
        offset += 1000
