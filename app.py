import csv
import datetime as dt
import io
import math
import re

import pandas as pd
import requests
import streamlit as st

import bulk_store as store
import runner as rn
import supabase_client as db
import smartta_client as wa

st.set_page_config(page_title="WhatsApp Bulk Admin", layout="wide")
st.title("WhatsApp Bulk Messaging - Admin")


@st.cache_data(ttl=300)
def load_campaigns():
    return db.fetch_campaigns()


@st.cache_data(ttl=300)
def load_booths(campaign_id: str):
    return db.fetch_booths(campaign_id)


@st.cache_data(ttl=300)
def load_booth_names(campaign_id: str):
    return db.fetch_booth_names(campaign_id)


@st.cache_data(ttl=3600)
def load_religions():
    return db.fetch_religions()


@st.cache_data(ttl=3600)
def load_communities(religion_id):
    return db.fetch_communities(religion_id)


@st.cache_data(ttl=300, show_spinner="Counting matching voters...")
def load_count(campaign_id: str, filters: dict):
    return db.count_voters(campaign_id, filters)


@st.cache_data(ttl=300, show_spinner="Loading voters...")
def load_voter_page(campaign_id: str, filters: dict, offset: int, limit: int):
    return db.fetch_voters(campaign_id, filters, offset, limit)


@st.cache_data(ttl=600, show_spinner="Loading WhatsApp templates...")
def load_templates():
    return wa.list_templates()


def template_label(i, t):
    message = re.sub(r"([\\`*_~#\[\]<>$|])", r"\\\1", " ".join(t["body"].split()))
    header = "image header" if t["header_type"] == "image" else "no header"
    return (
        f"**[{i + 1:02d}] {t['send_name']}** · {t['language']} · "
        f"{t['body_variables']} variables · {header}  \n{message}"
    )


def named_select(label, rows, key):
    """Selectbox over [{'id','name'}] returning the chosen id or None."""
    names = {r["id"]: r["name"] for r in rows}
    return st.selectbox(
        label, options=list(names), index=None, format_func=names.get, placeholder="Any", key=key
    )


def show_error(exc):
    if db.BIRTHDAY_VIEW in str(exc):
        st.error("Birthday filter needs a one-time database setup: run the supabase/migrations files.")
    else:
        st.error(f"Query failed (the database may have timed out; try narrowing the filters): {exc}")


@st.cache_resource
def get_runner():
    runner = rn.Runner()
    runner.start()
    return runner


STATUS_LABEL = {
    "running": "Running", "paused": "Paused", "completed": "Completed",
    "completed_with_failures": "Completed with failures", "cancelled": "Cancelled",
}


def _act(fn, *args):
    try:
        fn(*args)
    except Exception as exc:
        st.session_state["job_error"] = str(exc)


def _save_limit():
    _act(store.update_settings, int(st.session_state["limit_input"]))


def render_jobs():
    runner = get_runner()
    try:
        settings = store.get_settings()
        used = store.sent_last_24h()
        jobs = store.list_jobs(5)
        stats = {j["id"]: store.job_stats(j["id"]) for j in jobs}
    except requests.RequestException:
        st.warning("Cannot reach the database right now; retrying automatically.")
        return
    limit = settings["daily_limit"]
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    c1.metric("Sent in last 24h", f"{used:,}")
    c2.metric("Daily limit", f"{limit:,}")
    c3.metric("Remaining", f"{max(0, limit - used):,}")
    with c4:
        st.number_input("Daily limit", min_value=1, value=limit, step=100, key="limit_input")
        st.button("Save limit", on_click=_save_limit)
    st.caption(f"Sender: {runner.status}")
    if st.session_state.get("job_error"):
        st.error(st.session_state.pop("job_error"))

    running = any(j["status"] == "running" for j in jobs)

    if not jobs:
        st.caption("No send jobs yet.")
    for job in jobs:
        s = stats[job["id"]]
        total = sum(s.values())
        pending = s.get("pending", 0) + s.get("sending", 0)
        done = total - pending
        sent = s.get("submitted", 0) + s.get("delivered", 0) + s.get("read", 0)
        delivered = s.get("delivered", 0) + s.get("read", 0)
        failed = sum(v for k, v in s.items() if k.startswith("failed:"))
        skipped = sum(v for k, v in s.items() if k.startswith("skipped:"))
        jid = job["id"]
        with st.container(border=True):
            st.markdown(f"**{job['name']}** - {STATUS_LABEL.get(job['status'], job['status'])}")
            st.progress(done / total if total else 0.0, text=f"{done:,} of {total:,} processed")
            m = st.columns(6)
            m[0].metric("Pending", f"{pending:,}")
            m[1].metric("Sent", f"{sent:,}")
            m[2].metric("Delivered", f"{delivered:,}")
            m[3].metric("Read", f"{s.get('read', 0):,}")
            m[4].metric("Failed", f"{failed:,}")
            m[5].metric("Skipped", f"{skipped:,}")
            if failed:
                st.caption(
                    f"Failed: daily limit {s.get('failed:daily_limit', 0):,} | "
                    f"rejected {s.get('failed:rejected', 0):,} | delivery failed {s.get('failed:delivery_failed', 0):,}"
                )
            if skipped:
                st.caption(
                    f"Skipped: invalid number {s.get('skipped:invalid_number', 0):,} | "
                    f"duplicate number {s.get('skipped:duplicate_number', 0):,} | "
                    f"missing variable {s.get('skipped:missing_variable', 0):,} | cancelled {s.get('skipped:cancelled', 0):,}"
                )
            if job["status"] == "running":
                st.caption(runner.status)
            if job.get("last_error"):
                st.warning(job["last_error"])
            b = st.columns(5)
            if job["status"] == "running":
                b[0].button("Pause", key=f"pause_{jid}", on_click=_act, args=(runner.pause, jid))
            if job["status"] == "paused":
                b[0].button("Resume", key=f"resume_{jid}", on_click=_act, args=(runner.resume, jid), disabled=running)
            if job["status"] in ("running", "paused"):
                b[1].button("Cancel", key=f"cancel_{jid}", on_click=_act, args=(runner.cancel, jid))
            if failed and job["status"] != "running" and job["status"] != "cancelled":
                b[2].button(f"Retry failed ({failed:,})", key=f"retry_{jid}", on_click=_act,
                            args=(runner.retry_failed, jid), disabled=running)
            if b[3].button("Prepare report", key=f"rep_{jid}"):
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(["name", "phone", "status", "reason", "error", "sent_at", "delivered_at", "read_at"])
                for r in store.report_rows(jid):
                    writer.writerow([r["voter_name"], r["phone"], r["status"], r["fail_reason"], r["error"],
                                     r["sent_at"], r["delivered_at"], r["read_at"]])
                st.session_state[f"report_{jid}"] = buf.getvalue().encode("utf-8-sig")
            if f"report_{jid}" in st.session_state:
                b[4].download_button("Download CSV", st.session_state[f"report_{jid}"], file_name=f"send_report_{jid[:8]}.csv",
                                     mime="text/csv", key=f"dl_{jid}")


def build_plan(campaign_id, filters, select_all, selected_ids, sources, booth_names, total, progress):
    """Turn the selection into per-recipient rows: ready, or skipped with a reason."""
    if select_all:
        voters = db.iter_voters_for_send(campaign_id, {**filters, "valid_phone": True})
    else:
        voters = db.fetch_voters_by_ids(list(selected_ids))
    rows, seen, examples = [], set(), []
    counts = {"ready": 0, "invalid_number": 0, "duplicate_number": 0, "missing_variable": 0}
    for n, v in enumerate(voters, start=1):
        raw = (v.get("phone") or "").strip()
        row = {"voter_id": v["id"], "voter_name": v.get("name"), "phone": raw, "variables": [], "status": "skipped",
               "fail_reason": None}
        if not db.is_valid_mobile(raw):  # same rule as the Valid mobile count
            reason = "invalid_number"
        else:
            phone = wa.national_number(raw)
            row["phone"] = phone
            if phone in seen:
                reason = "duplicate_number"
            else:
                v["booth_name"] = booth_names.get(v.get("booth_number"))
                values = wa.resolve_values(sources, v)
                if all(values):
                    seen.add(phone)
                    row.update(variables=values, status="pending")
                    reason = "ready"
                else:
                    reason = "missing_variable"
        counts[reason] += 1
        if reason != "ready":
            row["fail_reason"] = reason
            if len(examples) < 20:
                examples.append({"name": row["voter_name"], "phone": raw, "reason": reason})
        rows.append(row)
        if n % 500 == 0:
            progress.progress(min(n / max(total, 1), 1.0), text=f"Preparing recipients... {n:,} of {total:,}")
    return {"rows": rows, "counts": counts, "examples": examples}


get_runner()
try:
    jobs_active = store.active_job_exists()
except requests.RequestException:
    jobs_active = True  # keep refreshing until the database is reachable again
with st.expander("Send jobs and daily limit", expanded=True):
    st.fragment(render_jobs, run_every=2 if jobs_active else None)()

try:
    campaigns = load_campaigns()
except Exception as exc:
    st.error(f"Could not load campaigns: {exc}")
    st.stop()

by_id = {c["id"]: c for c in campaigns}
campaign_id = st.selectbox(
    "Campaign",
    options=list(by_id),
    index=None,
    placeholder="Select a campaign to load its voters",
    format_func=lambda cid: f"{by_id[cid]['name']} ({by_id[cid]['status']})",
)

if campaign_id is None:
    st.info("Select a campaign to load its voters.")
    st.stop()

k = campaign_id  # widget keys are per campaign so filters reset when it changes
min_date, max_date = dt.date(1900, 1, 1), dt.date.today()

with st.expander("Filters", expanded=True):
    c1, c2, c3, c4 = st.columns(4)
    gender = c1.selectbox("Gender", ["Any", "Male", "Female"], key=f"gen_{k}")
    with c2:
        religion_id = named_select("Religion", load_religions(), f"rel_{k}")
    with c3:
        community_id = named_select("Community", load_communities(religion_id), f"com_{k}")
    birthday = c4.date_input(
        "Birthday (day & month)", value=None, min_value=min_date, max_value=max_date,
        help="Matches everyone born on this day and month, any year.", key=f"bd_{k}"
    )

    c1, c2, c3, c4 = st.columns(4)
    age_min = c1.number_input("Age from", min_value=0, max_value=120, value=None, step=1, format="%d", key=f"amin_{k}")
    age_max = c2.number_input("Age to", min_value=0, max_value=120, value=None, step=1, format="%d", key=f"amax_{k}")
    booth_min = c3.number_input("Booth from", min_value=0, value=None, step=1, format="%d", key=f"bmin_{k}")
    booth_max = c4.number_input("Booth to", min_value=0, value=None, step=1, format="%d", key=f"bmax_{k}")

    apply_clicked = st.button("Apply filters", type="primary")

if apply_clicked:
    booth_list = []
    if booth_min is not None or booth_max is not None:
        lo = booth_min if booth_min is not None else 0
        hi = booth_max if booth_max is not None else float("inf")
        booth_list = [b for b in load_booths(campaign_id) if b.isdigit() and lo <= int(b) <= hi]
        if not booth_list:
            st.error("No booths fall in that range.")
            st.stop()
    if age_min is not None and age_max is not None and age_min > age_max:
        st.error("Age 'from' is greater than 'to'.")
        st.stop()
    st.session_state["selected"] = {}
    st.session_state["select_all"] = False
    st.session_state["apply_n"] = st.session_state.get("apply_n", 0) + 1
    st.session_state["applied"] = {
        "campaign_id": campaign_id,
        "filters": {
            "gender": {"Male": "M", "Female": "F"}.get(gender),
            "birth_month": birthday.month if birthday else None,
            "birth_day": birthday.day if birthday else None,
            "age_min": int(age_min) if age_min is not None else None,
            "age_max": int(age_max) if age_max is not None else None,
            "booths": booth_list,
            "religion_id": religion_id,
            "community_id": community_id,
        },
    }

applied = st.session_state.get("applied")
if not applied or applied["campaign_id"] != campaign_id:
    st.info("Set filters and click Apply filters to load voters.")
    st.stop()

filters = applied["filters"]
try:
    total = load_count(campaign_id, filters)
    with_phone = load_count(campaign_id, {**filters, "has_phone": True}) if total else 0
    valid_phone = load_count(campaign_id, {**filters, "valid_phone": True}) if with_phone else 0
except Exception as exc:
    show_error(exc)
    st.stop()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Matching voters", f"{total:,}")
m2.metric("With mobile", f"{with_phone:,}")
m3.metric("Without mobile", f"{total - with_phone:,}")
m4.metric("Valid mobile", f"{valid_phone:,}", help="10-digit mobile starting 6-9, optionally with +91 or 91.")
if total == 0:
    st.warning("No voters match these filters.")
    st.stop()

selected = st.session_state.setdefault("selected", {})  # voter id -> phone, kept across pages
select_all = st.toggle(f"Select all {total:,} matching voters", key="select_all")

c1, c2, _ = st.columns([1, 1, 3])
page_size = c1.selectbox("Rows per page", [100, 250, 500, 1000], index=0)
pages = max(1, math.ceil(total / page_size))
page = c2.number_input("Page", min_value=1, max_value=pages, value=1, step=1)
st.caption(f"Page {page} of {pages}")

try:
    rows = load_voter_page(campaign_id, filters, (page - 1) * page_size, page_size)
except Exception as exc:
    show_error(exc)
    st.stop()

booth_names = load_booth_names(campaign_id)
for r in rows:
    r["booth_name"] = booth_names.get(r["booth_number"])

if rows:
    df = pd.DataFrame(rows)
    df.insert(0, "Select", True if select_all else df["id"].isin(selected))
    editor_key = f"tbl_{campaign_id}_{st.session_state.get('apply_n', 0)}_{page}_{page_size}_{select_all}"
    edited = st.data_editor(
        df, key=editor_key, hide_index=True, width="stretch",
        disabled=True if select_all else [c for c in df.columns if c != "Select"],
        column_config={"id": None, "Select": st.column_config.CheckboxColumn("Select", width="small")},
    )
    if not select_all:
        for voter_id, phone, checked in zip(edited["id"], edited["phone"], edited["Select"]):
            if checked:
                selected[voter_id] = phone if isinstance(phone, str) else None
            else:
                selected.pop(voter_id, None)


def clear_selection():
    st.session_state["selected"].clear()
    st.session_state["apply_n"] = st.session_state.get("apply_n", 0) + 1


if select_all:
    n_selected, n_valid = total, valid_phone
else:
    n_selected = len(selected)
    n_valid = sum(db.is_valid_mobile(p) for p in selected.values())
s1, s2, s3 = st.columns([1, 1, 2])
s1.metric("Selected voters", f"{n_selected:,}")
s2.metric("Selected with valid mobile", f"{n_valid:,}")
if selected and not select_all:
    s3.button("Clear selection", on_click=clear_selection)

st.subheader("WhatsApp template")
try:
    templates = load_templates()
except Exception as exc:
    st.error(f"Could not load WhatsApp templates: {exc}")
    st.stop()

labels = [template_label(i, t) for i, t in enumerate(templates)]
st.caption(f"{len(templates)} approved templates. Scroll the list and click one to select it.")
with st.container(height=380, border=True):
    choice = st.radio("Template", range(len(templates)), index=None, format_func=labels.__getitem__,
                      label_visibility="collapsed")
if choice is not None:
    t = templates[choice]
    st.caption(
        f"{t['send_name']} | {t['category']} | language: {t['language']} | header: {t['header_type']} "
        f"| body variables: {t['body_variables']}"
    )

    sample = None
    if rows:
        sample_names = [f"{r.get('name') or '(no name)'} - {r.get('epic_id') or ''}" for r in rows]
        sample_idx = st.selectbox(
            "Preview and test with voter", range(len(rows)), format_func=sample_names.__getitem__,
            help="Variables set to a voter field take this voter's data. Pick from the voters on the current page.",
            key=f"sample_{campaign_id}",
        )
        sample = rows[sample_idx]

    sources = []
    left, right = st.columns(2)
    with left:
        st.markdown("**Template variables**")
        for i in range(t["body_variables"]):
            c_src, c_txt = st.columns([1, 1])
            source = c_src.selectbox(
                f"Variable {{{{{i + 1}}}}}", [wa.FIXED_TEXT, *wa.VOTER_FIELDS],
                index=1 if i == 0 else 0, key=f"src_{choice}_{i}",
            )
            if source == wa.FIXED_TEXT:
                text = c_txt.text_input("Text", placeholder="e.g. {booth no} / {booth name}", key=f"var_{choice}_{i}")
                bad = wa.unknown_placeholders(text)
                if bad:
                    c_txt.warning(f"Unknown field: {', '.join('{' + b + '}' for b in bad)}")
            else:
                text = ""
                c_txt.text_input("Value", value="(from each voter)", disabled=True, key=f"dyn_{choice}_{i}_{source}")
            sources.append((source, text))
        if not t["body_variables"]:
            st.caption("This template has no variables.")
        else:
            st.caption(f"In Custom text you can mix text and fields: {wa.PLACEHOLDER_HINT}")
        image = None
        if t["header_type"] == "image":
            image = st.file_uploader(
                "Header image (PNG or JPG, max 5 MB)", type=["png", "jpg", "jpeg"], key=f"img_{choice}"
            )

    values = wa.resolve_values(sources, sample or {})

    with right:
        st.markdown("**Preview**")
        with st.container(border=True):
            if image is not None:
                st.image(image.getvalue(), width=300)
            elif t["header_type"] == "image":
                st.caption("Header image: not uploaded yet")
            st.text(wa.render_preview(t["body"], values))

    st.markdown("**Send a test message**")
    st.caption("Sent only to the test number below, filled with the voter selected above.")
    test_phone = st.text_input("Test mobile number (10 digits)", key="test_phone")
    if t["button_variables"]:
        st.warning("This template has button variables, which are not supported yet.")
    if st.button("Send test message", disabled=bool(t["button_variables"])):
        try:
            image_url = None
            if image is not None:
                with st.spinner("Uploading header image..."):
                    image_url = wa.upload_header_image(image.getvalue(), image.name)
            with st.spinner("Sending..."):
                result = wa.send_test(t, values, test_phone, image_url)
            st.success("Test message submitted to the WhatsApp provider.")
            st.json(result)
        except Exception as exc:
            st.error(str(exc))

    st.divider()
    st.subheader("Send to selected voters")
    problems = []
    if not (select_all or selected):
        problems.append("Select voters first: tick rows in the table or use Select all.")
    for i, (source, text) in enumerate(sources, start=1):
        if source == wa.FIXED_TEXT:
            if not text.strip():
                problems.append(f"Variable {{{{{i}}}}} is empty. All variables are mandatory.")
            elif wa.unknown_placeholders(text):
                problems.append(f"Variable {{{{{i}}}}} has an unknown field.")
    if t["header_type"] == "image" and image is None:
        problems.append("Upload the header image above.")
    if t["button_variables"]:
        problems.append("This template has button variables, which are not supported.")
    for problem in problems:
        st.warning(problem)

    if not problems:
        fingerprint = hash((campaign_id, st.session_state.get("apply_n", 0), choice, tuple(sources), select_all,
                            frozenset(selected), image.name if image else None, image.size if image else 0))
        if st.button("Prepare send"):
            bar = st.progress(0.0, text="Preparing recipients...")
            try:
                plan = build_plan(campaign_id, filters, select_all, list(selected), sources, booth_names,
                                  valid_phone if select_all else len(selected), bar)
            except Exception as exc:
                bar.empty()
                st.error(f"Could not prepare the send list: {exc}")
                st.stop()
            bar.empty()
            plan["fingerprint"] = fingerprint
            plan["left_out"] = (total - valid_phone) if select_all else 0  # no valid mobile; never fetched
            st.session_state["send_plan"] = plan

        plan = st.session_state.get("send_plan")
        if plan and plan["fingerprint"] != fingerprint:
            st.info("The selection or template changed. Click Prepare send again.")
            plan = None
        if plan:
            counts = plan["counts"]
            skipped = counts["invalid_number"] + counts["duplicate_number"] + counts["missing_variable"]
            try:
                settings = store.get_settings()
                remaining = max(0, settings["daily_limit"] - store.sent_last_24h())
                busy = store.active_job_exists()
            except requests.RequestException:
                st.error("Cannot reach the database to check the daily limit. Try again.")
                st.stop()
            will_send = min(counts["ready"], remaining)
            will_fail = counts["ready"] - will_send
            no_valid = counts["invalid_number"] + plan["left_out"]
            st.markdown(
                f"**{len(plan['rows']) + plan['left_out']:,}** selected: {no_valid:,} without a valid mobile number, "
                f"{counts['duplicate_number']:,} sharing a number already in the list, "
                f"{counts['missing_variable']:,} missing a variable. "
                f"Messages go only to the **{counts['ready']:,}** valid numbers left."
            )
            p1, p2, p3, p4 = st.columns(4)
            p1.metric("Valid numbers to send", f"{counts['ready']:,}")
            p2.metric("Not sent (skipped)", f"{skipped + plan['left_out']:,}")
            p3.metric("Daily limit left", f"{remaining:,}")
            p4.metric("Will fail (daily limit)", f"{will_fail:,}")
            if skipped:
                st.caption(
                    f"Skipped: invalid number {counts['invalid_number']:,} | duplicate number {counts['duplicate_number']:,} "
                    f"| missing variable {counts['missing_variable']:,}"
                )
                with st.expander("Skipped examples"):
                    st.dataframe(pd.DataFrame(plan["examples"]), hide_index=True)
            if will_fail:
                st.warning(f"{will_send:,} will be sent now. The other {will_fail:,} will be marked failed (daily limit); "
                           "use Retry failed on another day.")
            if len(plan["rows"]) > 20000:
                st.warning("This is a very large list; saving it may take a few minutes.")
            if busy:
                st.info("Another send job is running. Wait for it to finish or pause it first.")
            if remaining == 0:
                st.error("The daily limit is already used up. Nothing can be sent now.")
            confirm = st.checkbox(f"Yes, send {will_send:,} messages now (valid mobile numbers only)", key="confirm_send")
            if st.button("Start sending", type="primary",
                         disabled=not confirm or counts["ready"] == 0 or remaining == 0 or busy):
                job = None
                bar = st.progress(0.0, text="Saving the send list...")
                try:
                    image_url = wa.upload_header_image(image.getvalue(), image.name) if image else None
                    snapshot = {k: t[k] for k in ("send_name", "name", "language", "header_type", "category",
                                                  "body_variables", "button_variables", "body")}
                    name = f"{by_id[campaign_id]['name']} - {t['send_name']} - {dt.datetime.now():%d %b %H:%M}"
                    job = store.create_job(campaign_id, name, snapshot, image_url, status="paused")
                    store.insert_recipients(
                        job["id"], plan["rows"],
                        on_progress=lambda n, total_rows: bar.progress(n / total_rows, text=f"Saving {n:,} of {total_rows:,}"),
                    )
                    get_runner().resume(job["id"])
                except Exception as exc:
                    if job:
                        store.set_job(job["id"], status="cancelled", last_error=f"Could not save the send list: {exc}")
                    bar.empty()
                    st.error(f"Could not start the send: {exc}")
                    st.stop()
                st.session_state.pop("send_plan", None)
                st.session_state.pop("confirm_send", None)
                st.toast("Sending started")
                st.rerun()
