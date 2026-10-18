"""SmartTA (Sinch) WhatsApp client: authentication, template listing, header image upload, batch sends and delivery reports."""

import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

IST = timezone(timedelta(hours=5, minutes=30))


class Rejected(Exception):
    """The provider refused the request (4xx); resending the same payload will not help."""


class Transient(Exception):
    """Nothing was processed (never reached the provider, or throttled); safe to retry."""

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


class Ambiguous(Exception):
    """The request may have been processed; check delivery reports before retrying."""


class AuthFailed(RuntimeError):
    """WhatsApp credentials were refused."""

_token = {"value": None, "refresh": "", "expiry": 0.0}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # WhatsApp header image limit
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}

# Voter fields a template variable can be filled from (label -> voters column).
VOTER_FIELDS = {
    "Voter name": "name",
    "Voter name (local language)": "name_local",
    "EPIC ID": "epic_id",
    "Serial number (Sr no)": "serial_number",
    "Age": "age",
    "Booth number": "booth_number",
    "Booth name": "booth_name",
    "Section name": "section_name",
}
FIXED_TEXT = "Custom text"

# {placeholder} names usable inside custom text (spaces, _ and - are ignored, case-insensitive).
PLACEHOLDERS = {
    "name": ["name", "votername"],
    "name_local": ["namelocal", "localname"],
    "epic_id": ["epic", "epicid"],
    "serial_number": ["srno", "sno", "serialno", "serialnumber", "serial"],
    "age": ["age"],
    "booth_number": ["boothno", "boothnumber", "booth"],
    "booth_name": ["boothname"],
    "section_name": ["section", "sectionname"],
}
_PLACEHOLDER_LOOKUP = {a: col for col, aliases in PLACEHOLDERS.items() for a in aliases}
PLACEHOLDER_HINT = "{name}, {name local}, {epic}, {sr no}, {age}, {booth no}, {booth name}, {section}"


def _placeholder_column(raw: str) -> str | None:
    return _PLACEHOLDER_LOOKUP.get(re.sub(r"[\s_\-]+", "", raw.lower()))


def unknown_placeholders(text: str) -> list[str]:
    return [n for n in re.findall(r"\{([^{}]+)\}", text) if _placeholder_column(n) is None]


def _voter_value(voter: dict, column: str) -> str:
    value = voter.get(column)
    if value in (None, "") and column == "name_local":
        value = voter.get("name")  # fall back when no local-language name exists
    return "" if value is None else str(value).strip()


def resolve_values(sources: list[tuple[str, str]], voter: dict) -> list[str]:
    """sources: (source label, custom text) per variable. Returns the value for this voter."""
    values = []
    for source, text in sources:
        if source == FIXED_TEXT:
            filled = re.sub(
                r"\{([^{}]+)\}",
                lambda m: _voter_value(voter, _placeholder_column(m.group(1))) if _placeholder_column(m.group(1)) else m.group(0),
                text,
            )
            values.append(filled.strip())
        else:
            values.append(_voter_value(voter, VOTER_FIELDS[source]))
    return values


def _env(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set in .env")
    return value


def _login() -> None:
    if _token["value"] and time.time() < _token["expiry"] - 60:
        return
    resp = requests.post(
        _env("SMARTTA_AUTH_URL"),
        data={
            "grant_type": "password",
            "client_id": _env("SMARTTA_CLIENT_ID"),
            "username": _env("SMARTTA_USERNAME"),
            "password": _env("SMARTTA_PASSWORD"),
            "scope": "openid profile email",
        },
        timeout=30,
    )
    if not resp.ok:
        raise AuthFailed(f"WhatsApp authentication failed ({resp.status_code})")
    body = resp.json()
    _token.update(
        value=body["access_token"],
        refresh=body.get("refresh_token", ""),
        expiry=time.time() + body.get("expires_in", 300),
    )


def _headers() -> dict:
    _login()
    return {"Authorization": f"Bearer {_token['value']}", "refreshToken": _token["refresh"]}


def _send_name(container: str, body: str) -> str:
    """Name the send API expects; the listing groups many templates under one container name."""
    if "EPIC ID" in body:
        return "epic_id"
    if "family" in body or "కుటుంబ" in body:
        return "festival_wishes"
    if "invited to attend" in body or "హాజరు కావాలని" in body or "participate" in body:
        return "metting_invite"
    if container == "welcome_text":
        return "welcome_text"
    return "bharat_text_1"


def list_templates(approved_only: bool = True) -> list[dict]:
    """Return one entry per template language variant."""
    resp = requests.post(
        f"{_env('SMARTTA_API_BASE')}/funnel-service-api/v1/wa/template/listitems",
        json={
            "waApprovedTemplate": False,
            "filter": {"enterpriseId": _env("SMARTTA_ENTERPRISE_ID")},
            "pagination": {"currPageNumber": 1, "pageSize": 100},
            "sort": {},
        },
        headers=_headers(),
        timeout=60,
    )
    resp.raise_for_status()
    templates = []
    for it in resp.json().get("data", {}).get("dataList", []):
        for variant in it.get("items", []):
            if approved_only and variant.get("waStatus") != "approved":
                continue
            components = variant.get("components", [])
            body = next((c.get("text", "") for c in components if c.get("type") == "BODY"), "").strip()
            container = it.get("name") or ""
            templates.append(
                {
                    "name": container,
                    "send_name": _send_name(container, body),
                    "category": it.get("categoryCode") or it.get("category") or "MARKETING",
                    "language": variant.get("languageCode"),
                    "header_type": variant.get("headerType") or "none",
                    "body_variables": variant.get("bodyVariableCount", 0),
                    "button_variables": variant.get("buttonVariableCount", 0),
                    "body": body,
                }
            )
    # Same order as the reference script: API order, welcome_text last.
    return sorted(templates, key=lambda t: t["name"] == "welcome_text")


def render_preview(body: str, values: list[str]) -> str:
    """Replace {{n}} placeholders with the supplied values (kept as {{n}} while empty)."""

    def sub(match):
        idx = int(match.group(1)) - 1
        return values[idx] if 0 <= idx < len(values) and values[idx] else match.group(0)

    return re.sub(r"\{\{(\d+)\}\}", sub, body)


def national_number(raw: str) -> str:
    """10-digit number from a phone that already passed the valid-mobile rule (drops spaces, + and 91)."""
    digits = re.sub(r"[\s+]", "", raw)
    return digits[2:] if len(digits) == 12 and digits.startswith("91") else digits


def normalize_phone(raw: str) -> tuple[str, str]:
    """Return (country_code, national_number) for an Indian mobile number; raise ValueError if invalid."""
    digits = re.sub(r"[\s\-+]", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise ValueError("Enter a valid 10-digit Indian mobile number.")
    return "+91", digits


def upload_header_image(data: bytes, filename: str) -> str:
    """Host the header image at a public HTTPS URL (the reference script uses catbox.moe)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in IMAGE_TYPES:
        raise ValueError("Header image must be a PNG or JPG file.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Header image must be 5 MB or smaller.")
    resp = requests.post(
        "https://catbox.moe/user/api.php",
        data={"reqtype": "fileupload"},
        files={"fileToUpload": (f"header.{ext}", data, IMAGE_TYPES[ext])},
        timeout=60,
    )
    url = resp.text.strip()
    if not resp.ok or not url.startswith("https://"):
        raise RuntimeError(f"Image upload failed ({resp.status_code}).")
    return url


def _post_batch(url: str, payload: dict) -> requests.Response:
    """POST with one automatic re-login; maps failures to Transient / Ambiguous / AuthFailed."""
    for attempt in range(2):
        try:
            headers = _headers()
        except requests.RequestException as exc:
            raise Transient(f"login unreachable: {exc.__class__.__name__}")
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=(10, 60))
        except requests.ConnectTimeout as exc:
            raise Transient(f"cannot connect: {exc.__class__.__name__}")
        except requests.RequestException as exc:
            raise Ambiguous(f"connection lost: {exc.__class__.__name__}")
        if resp.status_code == 401 and attempt == 0:
            _token["value"] = None  # token expired mid-run: log in again once
            continue
        return resp
    return resp


def send_batch(template: dict, rows: list[tuple[str, list[str]]], image_url: str | None = None) -> dict:
    """Send one template to many recipients in a single call. rows: (10-digit number, variable values)."""
    n_vars = template["body_variables"]
    payload = {
        "enterpriseId": _env("SMARTTA_ENTERPRISE_ID"),
        "appId": _env("SMARTTA_APP_ID"),
        "campaignAudienceUpload": {
            "headerConfig": [
                {"dbName": "msisdnCountryCode", "userFieldName": "Country Code"},
                {"dbName": "msisdn", "userFieldName": "MSISDN"},
                *[{"dbName": f"var{i}", "userFieldName": f"Variable {i}"} for i in range(1, n_vars + 1)],
            ],
            "rowItems": [["+91", phone, *values] for phone, values in rows],
        },
        "templateName": template["send_name"],
        "selectedLangCodes": [template["language"]],
        "templateHeaderVariableMapping": {"1": image_url} if template["header_type"] == "image" else {},
        "templateBodyVariableMapping": {str(i): f"var{i}" for i in range(1, n_vars + 1)},
        "templateButtonVariableMapping": {},
        "templateQrButtonVariableMapping": {},
        "templateUrlButtonVariableMapping": {},
        "templateCopyCodeButtonVariableMapping": {},
        "templateHeaderMediaCommon": False,
        "templateHeaderMediaMimeType": None,
        "templateHeaderMediaCommonMediaId": None,
        "templateHeaderMediaName": None,
        "templateLocationNameVariableMapping": {},
        "templateLocationAddressVariableMapping": {},
        "templateLocationLatitudeVariableMapping": {},
        "templateLocationLongitudeVariableMapping": {},
        "categoryCode": template["category"],
        "subCategory": "Custom",
        "overallAudienceCount": len(rows),
        "audienceEntityRef": "",
        "campaignType": "quick_campaign",
        "channel": "wa",
    }
    url = f"{_env('SMARTTA_API_BASE')}/binnacle-service-api/v1/wa/campaign/standard/test"

    resp = _post_batch(url, payload)
    # The listing may register the template under its container name instead.
    if resp.status_code == 400 and "Template is not approved" in resp.text and template["name"] != template["send_name"]:
        payload["templateName"] = template["name"]
        resp = _post_batch(url, payload)

    if resp.status_code == 429:
        raise Transient("rate limited by provider", retry_after=int(resp.headers.get("Retry-After", 30) or 30))
    if resp.status_code == 401:
        raise AuthFailed("WhatsApp credentials were refused")
    if resp.status_code >= 500:
        raise Ambiguous(f"provider error {resp.status_code}")
    if not resp.ok:
        raise Rejected(f"{resp.status_code}: {resp.text[:300]}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if isinstance(body, dict) and body.get("success") is False:
        raise Rejected(str(body.get("globalErrors") or body.get("errors") or body)[:300])
    return body if isinstance(body, dict) else {}


def fetch_delivery_reports(since: datetime) -> list[dict]:
    """Delivery records from `since` until now (provider groups them by IST day)."""
    since_ist = since.astimezone(IST)
    base = {
        "enterpriseIds": [_env("SMARTTA_ENTERPRISE_ID")],
        "appIds": [_env("SMARTTA_APP_ID")],
        "partnerIds": ["all"],
        "senders": [_env("SMARTTA_SENDER")],
        "templateIds": ["all"],
        "startDate": since_ist.strftime("%Y-%m-%d 00:00:00"),
        "endDate": datetime.now(IST).strftime("%Y-%m-%d 23:59:59"),
        "timeZone": "Asia/Kolkata",
        "channel": "wa",
    }
    url = f"{_env('SMARTTA_API_BASE')}/loran-service-api/v1/whatsapp/overall/detailed/messages"
    items, page = [], 1
    while page <= 200:
        resp = requests.post(
            url,
            json={"filter": base, "pagination": {"currPageNumber": page, "pageSize": 200},
                  "sort": {"ascending": False, "sortField": "createdOn"}},
            headers=_headers(), timeout=60,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        items += data.get("dataList", [])
        if not data.get("pagination", {}).get("hasNext"):
            break
        page += 1
    return items


def report_state(item: dict) -> str:
    status = str(item.get("status", "")).lower()
    if "read" in status:
        return "read"
    if "deliver" in status:
        return "delivered"
    if "fail" in status:
        return "failed"
    return "submitted"


def report_time(item: dict) -> datetime | None:
    try:
        return datetime.strptime(item["requestTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except (KeyError, ValueError, TypeError):
        return None


def send_test(template: dict, values: list[str], phone: str, image_url: str | None = None) -> dict:
    """Send the template to ONE test number."""
    _, number = normalize_phone(phone)
    if template["button_variables"]:
        raise ValueError("Templates with button variables are not supported yet.")
    if len(values) != template["body_variables"] or not all(v.strip() for v in values):
        raise ValueError("Fill in every template variable.")
    if template["header_type"] == "image" and not image_url:
        raise ValueError("This template needs a header image.")
    try:
        return send_batch(template, [(number, [v.strip() for v in values])], image_url)
    except (Rejected, Transient, Ambiguous, AuthFailed) as exc:
        raise RuntimeError(f"Send failed: {exc}")
