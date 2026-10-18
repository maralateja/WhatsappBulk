"""Background sender: works through running jobs in batches and survives internet loss, rate limits and restarts."""

import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

import bulk_store as store
import smartta_client as wa

THREAD_NAME = "wa-runner"
TRACK_EVERY = 60  # seconds between delivery-report checks
MAX_CONSECUTIVE_REJECTS = 5  # isolated rejections with no success in between -> pause the job
AMBIGUOUS_WAIT = 15  # let the provider's report catch up before checking whether a batch went through
OWNER = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_RANK = {"submitted": 1, "delivered": 2, "read": 3}


class PauseJob(Exception):
    """Stop the job and show the reason; the admin fixes the cause and resumes."""


def parse_ts(value: str) -> datetime:
    value = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], value)
    return datetime.fromisoformat(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Runner:
    def __init__(self, sender=None):
        self.sender = sender  # defaults to the real provider; tests inject fakes
        self.status = "Idle"
        self._wake = threading.Event()
        self._stop: set[str] = set()
        self._last_track = 0.0
        self._rejects = (None, 0)

    # ---- control (called from the UI) ---------------------------------------------------------

    def start(self) -> None:
        if any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate()):
            return
        threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True).start()

    def wake(self) -> None:
        self._wake.set()

    def pause(self, job_id: str) -> None:
        self._stop.add(job_id)
        store.set_job(job_id, status="paused")

    def resume(self, job_id: str) -> None:
        self._stop.discard(job_id)
        store.set_job(job_id, status="running", last_error=None)
        self._wake.set()

    def cancel(self, job_id: str) -> None:
        self._stop.add(job_id)
        store.set_job(job_id, status="cancelled", finished_at=_now().isoformat())
        store.update_where(job_id, "pending", status="skipped", fail_reason="cancelled")

    def retry_failed(self, job_id: str) -> None:
        store.requeue_failed(job_id)
        self._stop.discard(job_id)
        store.set_job(job_id, status="running", finished_at=None, last_error=None)
        self._wake.set()

    # ---- main loop ----------------------------------------------------------------------------

    def _io(self, fn, *args, **kwargs):
        """Database call that waits out connectivity problems instead of failing."""
        return store.forever(lambda: fn(*args, **kwargs), on_wait=self._offline)

    def _offline(self, wait: int) -> None:
        self.status = f"Waiting for internet, retrying in {wait}s"

    def _loop(self) -> None:
        while True:
            try:
                if not store.acquire_lease(OWNER):
                    self.status = "Another sender is already running"
                    self._sleep(30)
                    continue
                job = store.next_running_job()
                if job:
                    self._run_job(job)
                else:
                    self.status = "Idle"
                    self._track_if_due()
                    self._sleep(15)
            except requests.RequestException:
                self.status = "Waiting for internet"
                time.sleep(10)
            except Exception as exc:  # the sender must never die silently
                self.status = f"Error: {exc}"
                time.sleep(10)

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _run_job(self, job: dict) -> None:
        jid = job["id"]
        self._rejects = (None, 0)
        try:
            sending = self._io(store.sending_rows, jid)
            if sending:  # left over from a crash or restart
                self._settle_unconfirmed(job, sending)
            while True:
                job = self._io(store.get_job, jid)
                if not job or job["status"] != "running" or jid in self._stop:
                    return
                if not self._io(store.acquire_lease, OWNER):
                    return
                settings = self._io(store.get_settings)
                remaining = settings["daily_limit"] - self._io(store.sent_last_24h)
                if remaining <= 0:
                    self._io(store.update_where, jid, "pending", status="failed", fail_reason="daily_limit",
                             error="Daily WhatsApp limit reached")
                    break
                rows = self._io(store.next_pending, jid, min(settings["batch_size"], remaining))
                if not rows:
                    break
                self._io(store.update_rows, [r["id"] for r in rows], status="sending")
                self.status = f"Sending {len(rows)} messages"
                self._send(job, rows, _now())
                self._track_if_due()
            self._finish(jid)
        except PauseJob as exc:
            self._io(store.update_where, jid, "sending", status="pending")
            self._io(store.set_job, jid, status="paused", last_error=str(exc))
        except wa.AuthFailed as exc:
            self._io(store.update_where, jid, "sending", status="pending")
            self._io(store.set_job, jid, status="paused", last_error=f"WhatsApp login failed: {exc}")

    def _finish(self, jid: str) -> None:
        job = self._io(store.get_job, jid)
        if not job or job["status"] != "running":
            return
        stats = self._io(store.job_stats, jid)
        if stats.get("pending") or stats.get("sending"):
            return
        failed = any(k.startswith("failed:") for k in stats)
        self._io(store.set_job, jid, status="completed_with_failures" if failed else "completed",
                 finished_at=_now().isoformat())

    # ---- sending ------------------------------------------------------------------------------

    def _send(self, job: dict, rows: list[dict], started: datetime) -> None:
        sender = self.sender or wa.send_batch
        ids = [r["id"] for r in rows]
        batch = [(r["phone"], r["variables"]) for r in rows]
        attempt = 0
        while True:
            try:
                result = sender(job["template"], batch, job.get("header_image_url"))
            except wa.Transient as exc:
                wait = exc.retry_after or min(5 * 2 ** attempt, 300)
                attempt += 1
                if not self._wait(job["id"], wait, str(exc)):
                    self._io(store.update_rows, ids, status="pending")
                    return
                continue
            except wa.Ambiguous as exc:
                self.status = f"Connection problem ({exc}); checking what was delivered"
                time.sleep(AMBIGUOUS_WAIT)
                self._settle_unconfirmed(job, rows, started)
                return
            except wa.Rejected as exc:
                self._rejected(job, rows, str(exc), started)
                return
            self._rejects = (None, 0)
            ref = None
            if isinstance(result, dict):
                ref = str(result.get("data") or result.get("id") or "")[:200] or None
            self._io(store.update_rows, ids, status="submitted", sent_at=_now().isoformat(), provider_ref=ref)
            return

    def _wait(self, job_id: str, seconds: int, reason: str) -> bool:
        """Sleep while reporting why; False if the job was paused or cancelled meanwhile."""
        for left in range(seconds, 0, -1):
            if job_id in self._stop:
                return False
            self.status = f"Waiting ({reason}), retrying in {left}s"
            time.sleep(1)
        return True

    def _rejected(self, job: dict, rows: list[dict], message: str, started: datetime) -> None:
        """Split a rejected batch until the offending recipient is isolated."""
        if len(rows) > 1:
            mid = len(rows) // 2
            for part in (rows[:mid], rows[mid:]):
                self._send(job, part, started)
            return
        row = rows[0]
        self._io(store.update_rows, [row["id"]], status="failed", fail_reason="rejected", error=message[:500])
        last, count = self._rejects
        self._rejects = (message, count + 1 if last == message else 1)
        if self._rejects[1] >= MAX_CONSECUTIVE_REJECTS:
            raise PauseJob(
                f"{MAX_CONSECUTIVE_REJECTS} recipients in a row were rejected with: {message}. "
                "Check the template and settings, then Resume (use Retry failed to retry the rejected ones)."
            )

    def _settle_unconfirmed(self, job: dict, rows: list[dict], started: datetime | None = None) -> None:
        """After an uncertain send, trust the delivery report: found -> submitted, otherwise send again."""
        if started is None:
            stamps = [parse_ts(r["updated_at"]) for r in rows if r.get("updated_at")]
            started = min(stamps) if stamps else _now() - timedelta(hours=1)
        reports = self._io(wa.fetch_delivery_reports, started - timedelta(minutes=5))
        names = {job["template"]["send_name"], job["template"]["name"]}
        seen = {
            str(it.get("userMSISDN"))
            for it in reports
            if it.get("templateId") in names and (wa.report_time(it) or started) >= started - timedelta(minutes=2)
        }
        found = [r["id"] for r in rows if f"91{r['phone']}" in seen]
        missing = [r["id"] for r in rows if r["id"] not in set(found)]
        if found:
            self._io(store.update_rows, found, status="submitted", sent_at=_now().isoformat())
        if missing:
            self._io(store.update_rows, missing, status="pending")

    # ---- delivery tracking --------------------------------------------------------------------

    def _track_if_due(self) -> None:
        if time.time() - self._last_track < TRACK_EVERY:
            return
        self._last_track = time.time()
        try:
            self.track()
        except (requests.RequestException, wa.AuthFailed):
            pass  # best effort; retried on the next cycle

    def track(self) -> None:
        rows = store.trackable_rows()
        cutoff = _now() - timedelta(hours=24)  # delivered rows are only watched for a day (to catch "read")
        rows = [r for r in rows if r["status"] == "submitted" or parse_ts(r["sent_at"]) >= cutoff]
        if not rows:
            return
        sent = {r["id"]: parse_ts(r["sent_at"]) for r in rows}
        reports = wa.fetch_delivery_reports(min(sent.values()))
        by_phone: dict[str, list[tuple[datetime, str]]] = {}
        for it in reports:
            when = wa.report_time(it)
            if when:
                by_phone.setdefault(str(it.get("userMSISDN")), []).append((when, wa.report_state(it)))
        updates: dict[str, list[int]] = {"delivered": [], "read": [], "failed": []}
        for r in rows:
            candidates = sorted(c for c in by_phone.get(f"91{r['phone']}", []) if c[0] >= sent[r["id"]] - timedelta(minutes=5))
            if not candidates:
                continue
            state = candidates[0][1]
            if state == "failed" and r["status"] == "submitted":
                updates["failed"].append(r["id"])
            elif _RANK.get(state, 0) > _RANK[r["status"]]:
                updates[state].append(r["id"])
        now = _now().isoformat()
        if updates["delivered"]:
            store.update_rows(updates["delivered"], status="delivered", delivered_at=now)
        if updates["read"]:
            store.update_rows(updates["read"], status="read", read_at=now)
        if updates["failed"]:
            store.update_rows(updates["failed"], status="failed", fail_reason="delivery_failed",
                              error="WhatsApp reported the message as failed")
