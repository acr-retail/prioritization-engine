"""
ACR Prioritization Engine — Standalone Web App
Talks to Odoo via JSON-RPC. Auth via Odoo API keys.
"""
import asyncio
import json
import logging
import re
import xmlrpc.client
from datetime import date
from html import escape as html_escape
from pathlib import Path

import bleach

import time as _time
from collections import defaultdict
from datetime import datetime as dt

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
import base64
import hashlib
import json as _json
import os
from http import cookies as _cookies
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from fastapi.responses import JSONResponse

_DEV_SECRET = "acr-priority-dev-secret-change-in-prod"
SECRET_KEY = (os.environ.get("SECRET_KEY") or "").strip()
if not SECRET_KEY or SECRET_KEY == _DEV_SECRET:
    raise RuntimeError(
        "SECRET_KEY env var is missing or set to the legacy dev default. "
        "Generate a secure value and set it in the environment:\n"
        "    python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )

# Origins permitted to issue state-changing requests (CSRF defense).
# Comma-separated. Production must set this explicitly via env.
ALLOWED_ORIGINS = {
    o.strip().rstrip("/")
    for o in os.environ.get(
        "ACR_ALLOWED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if o.strip()
}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Cookie hardening: require HTTPS in production (auto-detected when an
# https origin is in the allowlist).
_HTTPS_ONLY = any(o.startswith("https://") for o in ALLOWED_ORIGINS)

# ---------------------------------------------------------------------------
# Encrypted session cookie
# ---------------------------------------------------------------------------
# The session payload (uid + login + Odoo API key) is AES-encrypted before
# being placed in the cookie. Anyone reading the raw cookie value — browser
# dev tools, malware scraping cookie stores, a misconfigured proxy log —
# sees opaque bytes, not the API key. Starlette's bundled SessionMiddleware
# only *signs* (tamper-evident); it does not encrypt.
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))
_SESSION_COOKIE = "acr_session"
_SESSION_MAX_AGE = 14 * 24 * 3600  # 14 days


class EncryptedSessionMiddleware:
    """ASGI middleware that stores session state in a Fernet-encrypted cookie.

    Preserves Starlette's request.session contract (a dict on scope["session"]).
    Cookie is only re-issued when the session actually changes, so most
    request paths add zero crypto overhead.
    """

    def __init__(self, app, fernet, cookie_name, max_age, https_only, same_site="lax"):
        self.app = app
        self.fernet = fernet
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.https_only = https_only
        self.same_site = same_site

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Load existing session from the encrypted cookie (if any)
        session = {}
        cookie_header = b""
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                cookie_header = value
                break
        if cookie_header:
            jar = _cookies.SimpleCookie()
            jar.load(cookie_header.decode("latin-1"))
            morsel = jar.get(self.cookie_name)
            if morsel:
                try:
                    raw = self.fernet.decrypt(morsel.value.encode(), ttl=self.max_age)
                    session = _json.loads(raw)
                    if not isinstance(session, dict):
                        session = {}
                except (InvalidToken, ValueError, _json.JSONDecodeError):
                    session = {}

        scope["session"] = session
        # Snapshot for change detection so we only set-cookie when needed.
        initial = _json.dumps(session, sort_keys=True, separators=(",", ":"))

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                current = _json.dumps(
                    scope.get("session") or {}, sort_keys=True, separators=(",", ":")
                )
                if current != initial:
                    headers = list(message.get("headers", []))
                    if scope.get("session"):
                        token = self.fernet.encrypt(current.encode()).decode()
                        cookie = (
                            f"{self.cookie_name}={token}; Path=/; HttpOnly; "
                            f"Max-Age={self.max_age}; SameSite={self.same_site}"
                        )
                    else:
                        cookie = (
                            f"{self.cookie_name}=; Path=/; HttpOnly; Max-Age=0; "
                            f"SameSite={self.same_site}"
                        )
                    if self.https_only:
                        cookie += "; Secure"
                    headers.append((b"set-cookie", cookie.encode("latin-1")))
                    message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


app = FastAPI(title="ACR Prioritization Engine")
app.add_middleware(
    EncryptedSessionMiddleware,
    fernet=_fernet,
    cookie_name=_SESSION_COOKIE,
    max_age=_SESSION_MAX_AGE,
    https_only=_HTTPS_ONLY,
    same_site="lax",
)


@app.middleware("http")
async def enforce_csrf_origin(request: Request, call_next):
    """Reject state-changing requests whose Origin/Referer isn't allowlisted.

    Sufficient CSRF defense for a session-cookie auth model: browsers
    always attach Origin (and usually Referer) to cross-origin POSTs,
    and an attacker site cannot forge those headers from JS.
    """
    if request.method in _UNSAFE_METHODS:
        source = (request.headers.get("origin") or "").rstrip("/")
        if not source:
            ref = request.headers.get("referer", "")
            if ref:
                p = urlparse(ref)
                source = f"{p.scheme}://{p.netloc}".rstrip("/")
        if source not in ALLOWED_ORIGINS:
            # Log so we can spot probes / misconfigured clients
            ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                  or (request.client.host if request.client else "unknown"))
            try:
                _security_logger.warning(
                    f"csrf.rejected ip={ip} path={request.url.path} "
                    f"method={request.method} source={source!r}"
                )
            except NameError:
                pass  # logger not yet initialized at import time
            return JSONResponse(
                {"detail": "Origin not allowed"},
                status_code=403,
            )
    return await call_next(request)


@app.middleware("http")
async def no_cache_api(request: Request, call_next):
    """Prevent browser caching of /api/* responses.

    Without this, some browsers heuristically cache GET JSON responses.
    When a user saves a panel edit and reopens the panel, the second
    /api/task/{id} fetch can serve stale data — making the change look
    like it reverted.
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# ---------------------------------------------------------------------------
# SINGLE-WORKER ASSUMPTION
# ---------------------------------------------------------------------------
# The three module-level dicts below (active_users, _data_cache, and
# _selection_labels_cache further down) are in-process state. They work
# correctly only under a single worker process. The Procfile and
# render.yaml pin --workers 1 for this reason. Before raising worker
# count, move this state to shared storage (Redis, SQLite, etc.) or each
# worker will:
#   • show a different subset of online users (presence will flap),
#   • miss caches that another worker just populated,
#   • invalidate caches only in its own process on writes,
# producing surprising "sometimes stale, sometimes fresh" symptoms.
# Sized for the current internal use (6 users); revisit if that changes.

# Active user presence: login → last_seen timestamp
active_users: dict[str, float] = {}
PRESENCE_TIMEOUT = 120  # seconds

# ---------------------------------------------------------------------------
# Server-side data cache (per-user, 15 min TTL)
# ---------------------------------------------------------------------------
DATA_CACHE_TTL = 15 * 60  # seconds
_data_cache: dict[int, dict] = {}  # uid → {key: {data, ts}}


def cache_get(uid: int, key: str):
    """Get cached data for a user. Returns None if expired or missing."""
    user_cache = _data_cache.get(uid)
    if not user_cache:
        return None
    entry = user_cache.get(key)
    if not entry:
        return None
    if _time.time() - entry["ts"] > DATA_CACHE_TTL:
        del user_cache[key]
        return None
    return entry["data"]


def cache_set(uid: int, key: str, data):
    """Store data in the cache for a user."""
    if uid not in _data_cache:
        _data_cache[uid] = {}
    _data_cache[uid][key] = {"data": data, "ts": _time.time()}


def cache_clear(uid: int, key: str = None):
    """Clear cache for a user. If key is None, clear all."""
    if key:
        if uid in _data_cache and key in _data_cache[uid]:
            del _data_cache[uid][key]
    elif uid in _data_cache:
        del _data_cache[uid]


def get_open_tasks_cached(uid: int, api_key: str, extra_fields: list = None):
    """Get open tasks with caching."""
    cache_key = "open_tasks" + ("_gantt" if extra_fields else "")
    cached = cache_get(uid, cache_key)
    if cached is not None:
        return cached

    fields = TASK_FIELDS + (extra_fields or [])
    tasks = odoo_search_read(uid, api_key, "project.task",
                             [("stage_id.name", "not in", list(EXCLUDED_STAGES))],
                             fields)
    cache_set(uid, cache_key, tasks)
    return tasks


def get_weight_map_cached(uid: int, api_key: str):
    """Get weight map with caching."""
    cached = cache_get(uid, "weight_map")
    if cached is not None:
        return cached
    wm = load_weight_map(uid, api_key)
    cache_set(uid, "weight_map", wm)
    return wm


def get_sel_labels_cached(uid: int, api_key: str):
    """Get selection labels with caching."""
    cached = cache_get(uid, "sel_labels")
    if cached is not None:
        return cached
    sl = get_selection_labels(uid, api_key)
    cache_set(uid, "sel_labels", sl)
    return sl

ODOO_URL = (os.environ.get("ODOO_URL") or "").strip()
ODOO_DB = (os.environ.get("ODOO_DB") or "").strip()
if not ODOO_URL or not ODOO_DB:
    raise RuntimeError(
        "ODOO_URL and ODOO_DB env vars are required and must be set explicitly. "
        "Previously these silently fell back to a sandbox URL, which could "
        "cause the app to talk to the wrong Odoo if the env wasn't configured."
    )
if not ODOO_URL.startswith("https://"):
    raise RuntimeError(
        f"ODOO_URL must use https:// (got {ODOO_URL!r}). XML-RPC over plain "
        "HTTP would expose API keys on the wire."
    )

# Odoo models for weight storage
ATTR_MODEL = "x_acr_priority_attribute"
WEIGHT_MODEL = "x_acr_priority_weight"

# Stages to exclude from the backlog
EXCLUDED_STAGES = {"Complete", "Complete_1", "Cancelled"}

# Fields to pull from project.task
TASK_FIELDS = [
    "id", "name", "stage_id", "create_date", "user_ids", "project_id",
    "tag_ids",                                  # project.tags many2many
    "x_studio_customer",
    "x_studio_issue_type",
    "x_studio_level_of_effort",
    "x_studio_road_map_flag",
    "x_studio_related_field_5vi_1jnfmj9cf",   # escalated
    "x_studio_related_field_gd_1jnftb4gl",     # customer funded
    "x_studio_related_field_27d_1jnftbs3p",    # paid prioritization
]

# Map Odoo field names to friendly display names
FIELD_LABELS = {
    "x_studio_customer": "Customer",
    "x_studio_issue_type": "Issue Type",
    "x_studio_level_of_effort": "Level of Effort",
    "x_studio_road_map_flag": "Roadmap",
    "x_studio_related_field_5vi_1jnfmj9cf": "Escalated",
    "x_studio_related_field_gd_1jnftb4gl": "Customer Funded",
    "x_studio_related_field_27d_1jnftbs3p": "Paid Priority",
}

# ---------------------------------------------------------------------------
# HTML sanitization
# ---------------------------------------------------------------------------
# Odoo chatter messages and Studio descriptions are arbitrary HTML — and
# customer-submitted helpdesk tickets can include script tags. Anything
# rendered via innerHTML on the client must pass through here first.
_HTML_ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "h1", "h2",
    "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre",
    "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "u", "ul",
}
_HTML_ALLOWED_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
    "*": ["class"],
}
_HTML_ALLOWED_PROTOCOLS = {"http", "https", "mailto", "tel"}

# Bleach strips tags but leaves their text content — `<script>alert(1)</script>`
# becomes the visible string "alert(1)". Safe (no execution) but ugly. Pre-strip
# the dangerous tags *with* their contents for clean output.
_DANGEROUS_TAGS_RE = re.compile(
    r"<(script|style|iframe|object|embed|noscript|template|form)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def sanitize_html(value):
    """Clean Odoo-sourced HTML before sending to the browser.

    Returns the original value untouched for non-strings (False, None, etc.)
    so callers can pipe Odoo field reads through without type-checking.
    """
    if not isinstance(value, str) or not value:
        return value
    cleaned = _DANGEROUS_TAGS_RE.sub("", value)
    return bleach.clean(
        cleaned,
        tags=_HTML_ALLOWED_TAGS,
        attributes=_HTML_ALLOWED_ATTRS,
        protocols=_HTML_ALLOWED_PROTOCOLS,
        strip=True,
    )


# ---------------------------------------------------------------------------
# Weight storage (Odoo)
# ---------------------------------------------------------------------------
def _weight_attr_id(weight_row) -> int:
    """x_attribute_id reads as [id, name] for m2o; normalize to the id."""
    aid = weight_row.get("x_attribute_id")
    if isinstance(aid, (list, tuple)):
        return aid[0] if aid else 0
    return aid or 0


def load_weight_map(uid: int, api_key: str) -> dict:
    """Load all weights from Odoo into a dict for scoring.

    Uses two queries total (attributes + weights-in-bulk) rather than the
    1 + N pattern of fetching weights per attribute. With 8 attributes
    that is 9 round trips → 2.
    """
    attrs = odoo_search_read(
        uid, api_key, ATTR_MODEL, [],
        ["x_name", "x_task_field", "x_field_type", "x_sequence"],
    )
    if not attrs:
        return {}

    attr_ids = [a["id"] for a in attrs]
    weights = odoo_search_read(
        uid, api_key, WEIGHT_MODEL,
        [("x_attribute_id", "in", attr_ids)],
        ["x_value", "x_weight", "x_attribute_id"],
    )
    by_attr = defaultdict(list)
    for w in weights:
        by_attr[_weight_attr_id(w)].append(w)

    result = {}
    for attr in attrs:
        result[attr["x_task_field"]] = {
            "name": attr["x_name"],
            "field_type": attr["x_field_type"],
            "values": {w["x_value"]: w["x_weight"] for w in by_attr[attr["id"]]},
        }
    return result


def load_attributes_for_config(uid: int, api_key: str) -> list:
    """Load all attributes with their weights for the config page.

    Two-query batched read (see load_weight_map for rationale).
    """
    attrs = odoo_search_read(
        uid, api_key, ATTR_MODEL, [],
        ["x_name", "x_task_field", "x_field_type", "x_sequence"],
    )
    attrs.sort(key=lambda a: a.get("x_sequence", 0))
    if not attrs:
        return []

    attr_ids = [a["id"] for a in attrs]
    weights = odoo_search_read(
        uid, api_key, WEIGHT_MODEL,
        [("x_attribute_id", "in", attr_ids)],
        ["x_value", "x_weight", "x_description", "x_attribute_id"],
    )
    by_attr = defaultdict(list)
    for w in weights:
        by_attr[_weight_attr_id(w)].append(w)
    for bucket in by_attr.values():
        bucket.sort(key=lambda w: w.get("x_weight", 0))

    result = []
    for attr in attrs:
        result.append({
            "attr": {
                "id": attr["id"],
                "name": attr["x_name"],
                "task_field": attr["x_task_field"],
                "field_type": attr["x_field_type"],
            },
            "weights": [
                {"id": w["id"], "value": w["x_value"], "weight": w["x_weight"],
                 "description": w.get("x_description", "")}
                for w in by_attr[attr["id"]]
            ],
        })
    return result


# ---------------------------------------------------------------------------
# Odoo RPC helpers
# ---------------------------------------------------------------------------
# These remain synchronous so they can be called from each other and from
# the pytest suite directly. Routes wrap them via `_odoo()` to keep the
# event loop free during the XML-RPC round trip.
def odoo_authenticate(login: str, api_key: str) -> int:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, login, api_key, {})
    if not uid:
        raise ValueError("Authentication failed")
    return uid


def odoo_search_read(uid: int, api_key: str, model: str, domain: list,
                     fields: list, limit: int = 0) -> list:
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models.execute_kw(
        ODOO_DB, uid, api_key, model, "search_read",
        [domain], {"fields": fields, "limit": limit},
    )


def odoo_write(uid: int, api_key: str, model: str, ids: list, values: dict):
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models.execute_kw(
        ODOO_DB, uid, api_key, model, "write", [ids, values],
    )


def odoo_message_post(uid: int, api_key: str, model: str, record_id: int, body: str):
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models.execute_kw(
        ODOO_DB, uid, api_key, model, "message_post", [record_id],
        {"body": body, "message_type": "comment", "subtype_xmlid": "mail.mt_comment"},
    )


def odoo_fields_get(uid: int, api_key: str, model: str, field_names: list, attributes: list):
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models.execute_kw(
        ODOO_DB, uid, api_key, model, "fields_get",
        [field_names], {"attributes": attributes},
    )


async def _odoo(fn, *args, **kwargs):
    """Run a blocking Odoo helper in the thread pool so the event loop
    stays free during the XML-RPC round trip."""
    return await asyncio.to_thread(fn, *args, **kwargs)


import uuid as _uuid

# Set up the security logger once. Render captures stdout, so a plain
# logger is sufficient — we don't need structlog or external sinks for 6 users.
_security_logger = logging.getLogger("acr.security")
_security_logger.setLevel(logging.INFO)
if not _security_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _security_logger.addHandler(_h)


def _odoo_error_response(operation: str, exc: Exception, status: int = 400):
    """Return an HTTPException that hides Odoo's internal error string from
    the client. The raw fault is logged server-side with an error_id so
    support can correlate a user-reported "request failed (id=...)" with
    the actual exception."""
    error_id = _uuid.uuid4().hex[:12]
    _security_logger.error(
        f"odoo_call_failed operation={operation} error_id={error_id}",
        exc_info=exc,
    )
    return HTTPException(
        status_code=status,
        detail=f"{operation} failed. Reference: {error_id}",
    )


# ---------------------------------------------------------------------------
# /login rate limiter (IP-keyed, in-memory)
# ---------------------------------------------------------------------------
# Internal app, single worker — a module-level dict is sufficient.
# Limit: 5 attempts per IP per 15-minute sliding window.
_LOGIN_WINDOW_SEC = 15 * 60
_LOGIN_MAX_ATTEMPTS = 10
_login_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Render terminates TLS at its proxy so the
    real client IP is in X-Forwarded-For; the first entry is what we want."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _login_rate_check(ip: str) -> bool:
    """Returns True if the IP is allowed another login attempt. Side
    effect: prunes expired entries."""
    now = _time.time()
    history = _login_attempts.setdefault(ip, [])
    cutoff = now - _LOGIN_WINDOW_SEC
    # In place: drop expired timestamps
    history[:] = [t for t in history if t > cutoff]
    if len(history) >= _LOGIN_MAX_ATTEMPTS:
        return False
    history.append(now)
    return True


def _login_rate_reset(ip: str) -> None:
    """Clear an IP's attempt history. Called on successful login so a
    legitimate user who fat-fingered a couple of API keys isn't locked
    out for 15 minutes after they finally succeed."""
    _login_attempts.pop(ip, None)


# Sentinel: convert_value returns this when the value should not be sent
# to Odoo. The caller filters these out with `if result is not _SKIP`.
_SKIP = object()


def convert_value(val, ftype):
    """Map a form value (string, bool, or empty) to its Odoo write shape.

    Critical rule: an empty string means "the user picked the — option
    and wants to clear this field." Each type decides what "clear" means
    in Odoo terms:

      • many2many_single → [(5, 0, 0)]  (unset all relations)
      • int_or_false     → False        (unset many2one)
      • date_or_false    → False        (unset date)
      • string_or_false  → False        (unset selection / text)
      • float            → 0.0
      • bool             → False
      • int / string     → _SKIP        (no Odoo-side notion of "no value")
    """
    is_empty = val is None or val == ""

    if ftype == "bool":
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)
    if ftype == "int":
        return _SKIP if is_empty else int(val)
    if ftype == "int_or_false":
        return False if is_empty else int(val)
    if ftype == "float":
        return 0.0 if is_empty else float(val)
    if ftype == "date_or_false":
        return False if is_empty else val
    if ftype == "string_or_false":
        return False if is_empty else val
    if ftype == "string":
        return _SKIP if is_empty else val
    if ftype == "many2many_single":
        return [(5, 0, 0)] if is_empty else [(6, 0, [int(val)])]
    if ftype == "many2many_replace":
        # Accepts a comma-separated list of ids ("1,3,7") or an already-
        # parsed list. Replaces the full set in one write.
        if is_empty:
            return [(5, 0, 0)]
        if isinstance(val, str):
            ids = [int(x) for x in val.split(",") if x.strip()]
        else:
            ids = [int(x) for x in val if str(x).strip()]
        return [(6, 0, ids)] if ids else [(5, 0, 0)]
    return val


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def compute_age_bracket(create_date_str: str) -> str:
    if not create_date_str:
        return ""
    created = date.fromisoformat(create_date_str[:10])
    days = (date.today() - created).days
    if days < 30:
        return "<30"
    elif days <= 60:
        return "30-60"
    elif days <= 90:
        return "60-90"
    return ">90"


# Cache for selection field key→label mappings.
# Module-level state — see the SINGLE-WORKER comment near `active_users`.
_selection_labels_cache: dict | None = None


def get_selection_labels(uid: int, api_key: str) -> dict:
    """Load selection field key→label mappings from Odoo. Cached per process."""
    global _selection_labels_cache
    if _selection_labels_cache is not None:
        return _selection_labels_cache

    field_defs = odoo_fields_get(
        uid, api_key, "project.task",
        ["x_studio_issue_type", "x_studio_level_of_effort",
         "x_studio_related_field_gd_1jnftb4gl"],
        ["selection"],
    )

    result = {}
    for fname, fdef in field_defs.items():
        result[fname] = {key: label for key, label in fdef.get("selection", [])}

    _selection_labels_cache = result
    return result


def get_field_display(task: dict, field_name: str, field_type: str,
                      sel_labels: dict = None) -> str:
    """Extract the display value from an Odoo task record."""
    if field_type == "computed":
        return compute_age_bracket(task.get("create_date", ""))

    raw = task.get(field_name)

    # Boolean fields: False is a valid value meaning "No"
    if field_type == "boolean":
        if raw is None:
            return ""
        return "True" if raw else "False"

    # For non-boolean fields, False/None means unset
    if raw is False or raw is None:
        return ""

    if field_type == "many2one" and isinstance(raw, (list, tuple)):
        return raw[1] if len(raw) > 1 else str(raw[0])

    if field_type == "selection" and sel_labels:
        # Convert selection key to display label for weight matching
        field_map = sel_labels.get(field_name, {})
        if raw in field_map:
            return field_map[raw]

    return str(raw) if raw else ""


def score_task(task: dict, weight_map: dict, sel_labels: dict = None) -> int:
    score = 0
    for field_name, config in weight_map.items():
        display_val = get_field_display(task, field_name, config["field_type"],
                                        sel_labels)
        if display_val and display_val in config["values"]:
            score += config["values"][display_val]
        elif not display_val and "Not Set" in config["values"]:
            # Empty/unset field — use the "Not Set" weight
            score += config["values"]["Not Set"]
    return score


def enrich_tasks(tasks: list, weight_map: dict, sel_labels: dict = None) -> list:
    """Add score, age bracket, and friendly field values to each task."""
    for task in tasks:
        task["_score"] = score_task(task, weight_map, sel_labels)
        task["_age"] = compute_age_bracket(task.get("create_date", ""))
        task["_grooming"] = compute_grooming(task)
        task["_stage"] = task["stage_id"][1] if isinstance(task.get("stage_id"), (list, tuple)) else ""
        task["_customer"] = ""
        cust = task.get("x_studio_customer")
        if isinstance(cust, (list, tuple)) and len(cust) > 1:
            task["_customer"] = cust[1]
        elif cust and cust is not False:
            task["_customer"] = str(cust)
        proj = task.get("project_id")
        task["_project"] = proj[1] if isinstance(proj, (list, tuple)) and len(proj) > 1 else "No Project"
        task["_project_id"] = proj[0] if isinstance(proj, (list, tuple)) and len(proj) > 0 else 0
        # Odoo Studio selection fields can have key != label (e.g. "Minor" key → "Non-critical Workflow Bug" label).
        # Show the label, falling back to the raw key when no mapping exists.
        issue_key = task.get("x_studio_issue_type")
        task["_issue_type_label"] = (sel_labels or {}).get("x_studio_issue_type", {}).get(issue_key, issue_key) if issue_key else ""
    tasks.sort(key=lambda t: t["_score"])
    return tasks


def resolve_user_names(tasks: list, uid: int, api_key: str):
    """Add _assignee and _assignee_id to tasks by resolving user_ids."""
    # Collect all user IDs
    all_user_ids = set()
    for t in tasks:
        uids = t.get("user_ids", [])
        if isinstance(uids, list):
            all_user_ids.update(uids)

    # Fetch user names (cached)
    user_names = cache_get(uid, "user_names")
    if user_names is None:
        user_names = {}
        if all_user_ids:
            users = odoo_search_read(uid, api_key, "res.users",
                                     [("id", "in", list(all_user_ids))], ["name"])
            user_names = {u["id"]: u["name"] for u in users}
        cache_set(uid, "user_names", user_names)

    # Add to tasks
    for t in tasks:
        uids = t.get("user_ids", [])
        if uids and isinstance(uids, list) and uids[0] in user_names:
            t["_assignee"] = user_names[uids[0]]
            t["_assignee_id"] = uids[0]
        else:
            t["_assignee"] = "Unassigned"
            t["_assignee_id"] = 0


def resolve_tag_names(items: list, uid: int, api_key: str,
                      tag_model: str = "project.tags",
                      cache_key: str = "tag_names"):
    """Add _tags (list of {id,name}) to each item by resolving tag_ids
    via the given Odoo tag model. Fetches the full tag catalog (small
    — 4 tags in practice for project.tags) once per user and caches it,
    so a tag freshly assigned via the panel doesn't render as its raw
    ID until cache TTL.

    Default args preserve the original task-side behavior. Helpdesk
    callers pass tag_model="helpdesk.tag", cache_key="helpdesk_tag_names".
    """
    tag_names = cache_get(uid, cache_key)
    if tag_names is None:
        rows = odoo_search_read(uid, api_key, tag_model, [], ["name"])
        tag_names = {r["id"]: r["name"] for r in rows}
        cache_set(uid, cache_key, tag_names)

    for it in items:
        ids = it.get("tag_ids", []) or []
        it["_tags"] = [
            {"id": tid, "name": tag_names.get(tid, str(tid))}
            for tid in ids if isinstance(tid, int)
        ]


# Fields required for a task to be considered "groomed"
GROOMING_FIELDS = {
    "x_studio_issue_type": "Issue Type",
    "x_studio_level_of_effort": "Level of Effort",
    "x_studio_customer": "Customer",
}


def compute_grooming(task: dict) -> dict:
    """Check which grooming fields are missing on a task."""
    missing = []
    for field, label in GROOMING_FIELDS.items():
        val = task.get(field)
        if val is False or val is None or val == "":
            missing.append(label)
    return {
        "groomed": len(missing) == 0,
        "missing": missing,
        "missing_count": len(missing),
    }


def compute_score_thresholds(tasks: list) -> dict:
    """Compute Critical/High/Medium/Low thresholds from percentiles of actual scores."""
    scores = sorted(t.get("_score", 0) for t in tasks)
    n = len(scores)
    if n == 0:
        return {"critical": 0, "high": 0, "medium": 0}

    def percentile(pct):
        idx = int(n * pct / 100)
        return scores[min(idx, n - 1)]

    return {
        "critical": percentile(25),  # bottom 25%
        "high": percentile(50),      # 25-50%
        "medium": percentile(75),    # 50-75%
        # 75%+ = low
    }


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def get_session_creds(request: Request):
    uid = request.session.get("uid")
    api_key = request.session.get("api_key")
    login = request.session.get("login")
    if not uid or not api_key:
        return None
    return {"uid": uid, "api_key": api_key, "login": login}


def require_auth(request: Request):
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return creds


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    creds = get_session_creds(request)
    if not creds:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/backlog", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "error": request.query_params.get("error", ""),
    })


@app.post("/login")
async def login_submit(request: Request, login: str = Form(...), api_key: str = Form(...)):
    ip = _client_ip(request)
    if not _login_rate_check(ip):
        _security_logger.warning(
            f"login.rate_limited ip={ip} login={login}"
        )
        return RedirectResponse(
            "/login?error=Too+many+attempts.+Wait+15+minutes+and+try+again.",
            status_code=303,
        )
    try:
        uid = await _odoo(odoo_authenticate, login, api_key)
    except Exception:
        _security_logger.warning(
            f"login.failed ip={ip} login={login}"
        )
        return RedirectResponse(
            "/login?error=Authentication+failed.+Check+your+email+and+API+key.",
            status_code=303,
        )
    # Successful login — clear the IP's attempt history so a user who
    # finally got their key right isn't penalized for earlier typos.
    _login_rate_reset(ip)
    _security_logger.info(f"login.ok ip={ip} login={login} uid={uid}")
    request.session["uid"] = uid
    request.session["api_key"] = api_key
    request.session["login"] = login
    return RedirectResponse("/backlog", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


TICKET_FIELDS = [
    "id", "name", "stage_id", "create_date", "user_id",
    "partner_id", "ticket_ref",
    "tag_ids",                            # helpdesk.tag many2many
    "priority",
    "x_studio_customer_impact",
    "x_studio_customer_funded",
    "x_studio_escalated",
    "x_studio_paid_prioritization",
]

TICKET_EXCLUDED_STAGES = {"Solved", "Cancelled", "Closed", "CLOSED", "RESOLVED", "VERIFIED"}


def get_open_tickets_cached(uid: int, api_key: str):
    """Get open helpdesk tickets with caching."""
    cached = cache_get(uid, "open_tickets")
    if cached is not None:
        return cached

    tickets = odoo_search_read(uid, api_key, "helpdesk.ticket",
                               [("stage_id.name", "not in", list(TICKET_EXCLUDED_STAGES))],
                               TICKET_FIELDS)
    cache_set(uid, "open_tickets", tickets)
    return tickets


def enrich_tickets(tickets: list, weight_map: dict, sel_labels: dict = None) -> list:
    """Enrich helpdesk tickets with score and display values."""
    for t in tickets:
        # Map ticket fields to the same names the scoring expects
        t["_item_type"] = "ticket"
        t["x_studio_issue_type"] = t.get("x_studio_customer_impact", False)
        t["x_studio_related_field_gd_1jnftb4gl"] = t.get("x_studio_customer_funded", False)
        t["x_studio_related_field_5vi_1jnfmj9cf"] = t.get("x_studio_escalated", False)
        t["x_studio_related_field_27d_1jnftbs3p"] = t.get("x_studio_paid_prioritization", False)
        t["x_studio_customer"] = t.get("partner_id", False)
        t["x_studio_level_of_effort"] = False
        t["x_studio_road_map_flag"] = False

        t["_score"] = score_task(t, weight_map, sel_labels)
        t["_age"] = compute_age_bracket(t.get("create_date", ""))
        t["_grooming"] = compute_grooming(t)
        # helpdesk.ticket.x_studio_customer_impact uses the same key→label selection set
        # as project.task.x_studio_issue_type — Bugzilla severity keys, descriptive labels.
        issue_key = t.get("x_studio_issue_type")
        t["_issue_type_label"] = (sel_labels or {}).get("x_studio_issue_type", {}).get(issue_key, issue_key) if issue_key else ""
        t["_stage"] = t["stage_id"][1] if isinstance(t.get("stage_id"), (list, tuple)) else ""
        t["_customer"] = ""
        cust = t.get("partner_id")
        if isinstance(cust, (list, tuple)) and len(cust) > 1:
            t["_customer"] = cust[1]

        # Assignee
        uid_val = t.get("user_id")
        if isinstance(uid_val, (list, tuple)) and len(uid_val) > 1:
            t["_assignee"] = uid_val[1]
            t["_assignee_id"] = uid_val[0]
            t["user_ids"] = [uid_val[0]]
        else:
            t["_assignee"] = "Unassigned"
            t["_assignee_id"] = 0
            t["user_ids"] = []

    tickets.sort(key=lambda t: t["_score"])
    return tickets


@app.get("/backlog", response_class=HTMLResponse)
async def backlog(request: Request):
    creds = get_session_creds(request)
    if not creds:
        return RedirectResponse("/login", status_code=303)

    view_mode = request.query_params.get("view", "tasks")

    weight_map = await _odoo(get_weight_map_cached, creds["uid"], creds["api_key"])
    sel_labels = await _odoo(get_sel_labels_cached, creds["uid"], creds["api_key"])

    items = []

    if view_mode == "tasks":
        tasks = await _odoo(get_open_tasks_cached, creds["uid"], creds["api_key"])
        tasks = [dict(t) for t in tasks]
        tasks = enrich_tasks(tasks, weight_map, sel_labels)
        await _odoo(resolve_user_names, tasks, creds["uid"], creds["api_key"])
        await _odoo(resolve_tag_names, tasks, creds["uid"], creds["api_key"])
        for t in tasks:
            t["_item_type"] = "task"
        items = tasks
    else:
        tickets = await _odoo(get_open_tickets_cached, creds["uid"], creds["api_key"])
        tickets = [dict(t) for t in tickets]
        tickets = enrich_tickets(tickets, weight_map, sel_labels)
        await _odoo(resolve_tag_names, tickets, creds["uid"], creds["api_key"],
                    "helpdesk.tag", "helpdesk_tag_names")
        items = tickets

    items.sort(key=lambda t: t.get("_score", 0))

    task_json = json.dumps(items, default=str)

    thresholds = compute_score_thresholds(items)

    return templates.TemplateResponse(request, "backlog.html", {
        "tasks": items,
        "task_json": task_json,
        "login": creds["login"],
        "field_labels": FIELD_LABELS,
        "task_count": len(items),
        "thresholds": thresholds,
        "view_mode": view_mode,
    })


@app.get("/gantt", response_class=HTMLResponse)
async def gantt_page(request: Request):
    creds = get_session_creds(request)
    if not creds:
        return RedirectResponse("/login", status_code=303)

    # Pull open tasks with date fields (cached)
    gantt_extra = ["planned_date_begin", "date_end", "date_deadline", "user_ids", "project_id", "parent_id"]
    tasks = await _odoo(get_open_tasks_cached, creds["uid"], creds["api_key"], extra_fields=gantt_extra)
    weight_map = await _odoo(get_weight_map_cached, creds["uid"], creds["api_key"])
    sel_labels = await _odoo(get_sel_labels_cached, creds["uid"], creds["api_key"])
    tasks = [dict(t) for t in tasks]
    tasks = enrich_tasks(tasks, weight_map, sel_labels)
    await _odoo(resolve_user_names, tasks, creds["uid"], creds["api_key"])
    await _odoo(resolve_tag_names, tasks, creds["uid"], creds["api_key"])

    # Default dates for gantt
    today = date.today().isoformat()
    for t in tasks:
        # Minimum duration from Level of Effort
        effort = t.get("x_studio_level_of_effort", "")
        effort_days_map = {"<10 Hrs": 1, "10-40 Hrs": 5, "41-100 Hrs": 12, ">100 Hrs": 25}
        t["_min_duration"] = effort_days_map.get(effort, 1)

        # Start date
        start = t.get("planned_date_begin")
        t["_start"] = start[:10] if start else today

        # End date (defines bar length)
        end = t.get("date_end")
        if end:
            t["_end"] = end[:10]
        else:
            # Fall back to start + effort duration
            from datetime import timedelta
            start_dt = date.fromisoformat(t["_start"])
            t["_end"] = (start_dt + timedelta(days=t["_min_duration"])).isoformat()

        # Deadline (hard constraint)
        deadline = t.get("date_deadline")
        t["_deadline"] = deadline[:10] if deadline else None

        # Project name
        proj = t.get("project_id")
        t["_project"] = proj[1] if isinstance(proj, (list, tuple)) and len(proj) > 1 else "No Project"

        # Parent task
        parent = t.get("parent_id")
        t["_parent"] = parent[1] if isinstance(parent, (list, tuple)) and len(parent) > 1 else None
        t["_parent_id"] = parent[0] if isinstance(parent, (list, tuple)) and len(parent) > 0 else None

    task_json = json.dumps(tasks, default=str)

    return templates.TemplateResponse(request, "gantt.html", {
        "tasks": tasks,
        "task_json": task_json,
        "login": creds["login"],
        "task_count": len(tasks),
    })


@app.post("/api/recalculate-scores")
async def api_recalculate_scores(request: Request):
    """Recalculate priority scores for all open tasks using current weights."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Clear the selection labels cache so fresh data is used
    global _selection_labels_cache
    _selection_labels_cache = None

    # Clear all caches to force fresh data
    cache_clear(creds["uid"])

    tasks = await _odoo(get_open_tasks_cached, creds["uid"], creds["api_key"])
    weight_map = await _odoo(get_weight_map_cached, creds["uid"], creds["api_key"])
    sel_labels = await _odoo(get_sel_labels_cached, creds["uid"], creds["api_key"])

    scored_tasks = []
    scores = {}
    for task in tasks:
        s = score_task(task, weight_map, sel_labels)
        scores[task["id"]] = s
        scored_tasks.append({"_score": s})

    thresholds = compute_score_thresholds(scored_tasks)

    return {"ok": True, "scores": scores, "count": len(scores), "thresholds": thresholds}


@app.post("/api/task/{task_id}/dates")
async def api_update_task_dates(request: Request, task_id: int):
    """Update a task's start and end dates."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    values = {}

    if data.get("start"):
        values["planned_date_begin"] = data["start"]
    if data.get("end"):
        values["date_end"] = data["end"]
    if data.get("user_ids") is not None:
        user_list = data["user_ids"]
        if isinstance(user_list, list):
            values["user_ids"] = [(6, 0, [int(u) for u in user_list])]
        elif user_list:
            values["user_ids"] = [(6, 0, [int(user_list)])]

    if not values:
        return {"ok": True}

    try:
        await _odoo(odoo_write, creds["uid"], creds["api_key"], "project.task", [task_id], values)
    except Exception as e:
        raise _odoo_error_response("Update task dates", e)

    cache_clear(creds["uid"], "open_tasks")
    cache_clear(creds["uid"], "open_tasks_gantt")
    cache_clear(creds["uid"], "open_tickets")
    return {"ok": True}


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    creds = get_session_creds(request)
    if not creds:
        return RedirectResponse("/login", status_code=303)

    attributes = await _odoo(load_attributes_for_config, creds["uid"], creds["api_key"])

    return templates.TemplateResponse(request, "config.html", {
        "attributes": attributes,
        "login": creds["login"],
    })


@app.post("/config/weight/{weight_id}")
async def update_weight(request: Request, weight_id: int, weight: int = Form(...)):
    creds = get_session_creds(request)
    if not creds:
        return RedirectResponse("/login", status_code=303)

    try:
        await _odoo(odoo_write, creds["uid"], creds["api_key"], WEIGHT_MODEL,
                    [weight_id], {"x_weight": weight})
    except Exception as e:
        return RedirectResponse(f"/config?error={str(e)}", status_code=303)

    # Invalidate weight cache for all users
    for uid_key in list(_data_cache.keys()):
        cache_clear(uid_key, "weight_map")

    return RedirectResponse("/config", status_code=303)


@app.get("/api/options")
async def api_options(request: Request):
    """Fetch dropdown options for editable fields from Odoo."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Fetch stages
    stages = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "project.task.type",
        [], ["name"], 100,
    )

    # Fetch customers (partners used as x_studio_customer)
    customers = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "res.partner",
        [("customer_rank", ">", 0)], ["name"], 200,
    )
    # If no customer_rank results, try getting all companies
    if not customers:
        customers = await _odoo(
            odoo_search_read,
            creds["uid"], creds["api_key"], "res.partner",
            [("is_company", "=", True)], ["name"], 200,
        )

    # Selection field values from the Odoo field definitions
    field_defs = await _odoo(
        odoo_fields_get,
        creds["uid"], creds["api_key"], "project.task",
        ["x_studio_issue_type", "x_studio_level_of_effort",
         "x_studio_related_field_gd_1jnftb4gl", "priority"],
        ["selection"],
    )

    # Fetch users (assignees)
    users = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "res.users",
        [("share", "=", False)], ["name"], 200,
    )

    # Fetch project tags (project.tags many2many on project.task.tag_ids)
    tags = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "project.tags",
        [], ["name"], 100,
    )

    # Fetch helpdesk tags — separate model from project.tags, populated
    # independently. Empty today in this Odoo, but plumbing is in place
    # so the moment tickets get tagged the filter picks it up.
    helpdesk_tags = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "helpdesk.tag",
        [], ["name"], 100,
    )

    return {
        "stages": [{"id": s["id"], "name": s["name"]} for s in stages],
        "customers": [{"id": c["id"], "name": c["name"]} for c in customers],
        "users": [{"id": u["id"], "name": u["name"]} for u in users],
        "tags": [{"id": t["id"], "name": t["name"]} for t in tags],
        "helpdesk_tags": [{"id": t["id"], "name": t["name"]} for t in helpdesk_tags],
        "issue_types": [
            {"value": s[0], "label": s[1]}
            for s in field_defs.get("x_studio_issue_type", {}).get("selection", [])
        ],
        "effort_levels": [
            {"value": s[0], "label": s[1]}
            for s in field_defs.get("x_studio_level_of_effort", {}).get("selection", [])
        ],
        "customer_funded": [
            {"value": s[0], "label": s[1]}
            for s in field_defs.get("x_studio_related_field_gd_1jnftb4gl", {}).get("selection", [])
        ],
        "priorities": [
            {"value": s[0], "label": s[1]}
            for s in field_defs.get("priority", {}).get("selection", [])
        ],
    }


@app.post("/api/task/{task_id}/update")
async def update_task(request: Request, task_id: int):
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    values = {}

    # Fields that live on project.task directly
    task_field_map = {
        "name": ("name", "string"),
        "stage_id": ("stage_id", "int"),
        "x_studio_level_of_effort": ("x_studio_level_of_effort", "string_or_false"),
        "x_studio_road_map_flag": ("x_studio_road_map_flag", "bool"),
        "priority": ("priority", "string"),
        "planned_date_begin": ("planned_date_begin", "date_or_false"),
        "date_end": ("date_end", "date_or_false"),
        "date_deadline": ("date_deadline", "date_or_false"),
        "date_assign": ("date_assign", "date_or_false"),
        "allocated_hours": ("allocated_hours", "float"),
        "user_id": ("user_ids", "many2many_single"),
        "tag_ids": ("tag_ids", "many2many_replace"),
    }

    # Fields that live on the linked helpdesk.ticket (related fields)
    ticket_field_map = {
        "x_studio_customer": ("partner_id", "int_or_false"),
        "x_studio_issue_type": ("x_studio_customer_impact", "string_or_false"),
        "x_studio_related_field_5vi_1jnfmj9cf": ("x_studio_escalated", "bool"),
        "x_studio_related_field_gd_1jnftb4gl": ("x_studio_customer_funded", "string_or_false"),
        "x_studio_related_field_27d_1jnftbs3p": ("x_studio_paid_prioritization", "bool"),
    }

    # Process task fields. convert_value returns _SKIP for values that
    # shouldn't be sent (empty title, empty stage_id, etc.). Any other
    # return — including False, 0, [], "" — means "write this value to
    # Odoo". That's how clearing fields works.
    task_values = {}
    for key, (odoo_field, ftype) in task_field_map.items():
        if key not in data:
            continue
        result = convert_value(data[key], ftype)
        if result is not _SKIP:
            task_values[odoo_field] = result

    ticket_values = {}
    for key, (odoo_field, ftype) in ticket_field_map.items():
        if key not in data:
            continue
        result = convert_value(data[key], ftype)
        if result is not _SKIP:
            ticket_values[odoo_field] = result

    updated = 0

    # Write task fields
    if task_values:
        try:
            await _odoo(odoo_write, creds["uid"], creds["api_key"], "project.task",
                        [task_id], task_values)
            updated += len(task_values)
        except Exception as e:
            raise _odoo_error_response("Task update", e)

    # Write ticket fields (if there's a linked ticket)
    if ticket_values:
        # Look up the linked helpdesk ticket
        task_rec = await _odoo(
            odoo_search_read,
            creds["uid"], creds["api_key"], "project.task",
            [("id", "=", task_id)],
            ["helpdesk_ticket_id"], 1,
        )
        ticket_id_link = None
        if task_rec and task_rec[0].get("helpdesk_ticket_id"):
            ht = task_rec[0]["helpdesk_ticket_id"]
            ticket_id_link = ht[0] if isinstance(ht, (list, tuple)) else ht

        if ticket_id_link:
            try:
                await _odoo(odoo_write, creds["uid"], creds["api_key"], "helpdesk.ticket",
                            [ticket_id_link], ticket_values)
                updated += len(ticket_values)
            except Exception as e:
                raise _odoo_error_response("Ticket update", e)
        else:
            logging.warning(f"Task {task_id} has no linked helpdesk ticket — "
                            f"skipping ticket fields: {list(ticket_values.keys())}")

    # Invalidate task cache since data changed
    cache_clear(creds["uid"], "open_tasks")
    cache_clear(creds["uid"], "open_tasks_gantt")
    cache_clear(creds["uid"], "open_tickets")

    return {"ok": True, "updated": updated}


@app.get("/api/task/{task_id}")
async def api_task_detail(request: Request, task_id: int):
    """Fetch full task details + chatter messages from Odoo."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Fetch full task record
    detail_fields = TASK_FIELDS + [
        "description", "date_deadline", "date_end", "date_assign",
        "planned_date_begin",
        "user_ids", "tag_ids", "priority",
        "partner_id", "partner_name", "partner_phone", "email_from",
        "allocated_hours", "effective_hours", "remaining_hours",
        "helpdesk_ticket_id",
    ]

    tasks = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "project.task",
        [("id", "=", task_id)],
        detail_fields, 1,
    )

    if not tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[0]

    # Fetch chatter messages (mail.message)
    messages = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "mail.message",
        [
            ("res_id", "=", task_id),
            ("model", "=", "project.task"),
            ("message_type", "in", ["comment", "email", "notification"]),
        ],
        ["body", "date", "author_id", "message_type", "subtype_id",
         "attachment_ids"],
    )

    # Sort messages newest first
    messages.sort(key=lambda m: m.get("date", ""), reverse=True)

    # Fetch attachment info if any messages have them
    all_attachment_ids = []
    for msg in messages:
        all_attachment_ids.extend(msg.get("attachment_ids", []))

    attachments = {}
    if all_attachment_ids:
        att_records = await _odoo(
            odoo_search_read,
            creds["uid"], creds["api_key"], "ir.attachment",
            [("id", "in", all_attachment_ids)],
            ["name", "mimetype", "file_size"],
        )
        for a in att_records:
            attachments[a["id"]] = a

    # Enrich messages with attachment details
    for msg in messages:
        msg["_attachments"] = [
            attachments[aid] for aid in msg.get("attachment_ids", [])
            if aid in attachments
        ]
        # author_id is [id, name]
        if isinstance(msg.get("author_id"), (list, tuple)):
            msg["_author"] = msg["author_id"][1]
        else:
            msg["_author"] = "System"
        # Sanitize chatter HTML before it reaches innerHTML on the client
        msg["body"] = sanitize_html(msg.get("body"))

    # Enrich the task using the same code path as the backlog so the
    # post-save row repaint has every field it needs.
    weight_map = await _odoo(get_weight_map_cached, creds["uid"], creds["api_key"])
    sel_labels = await _odoo(get_sel_labels_cached, creds["uid"], creds["api_key"])
    enriched = enrich_tasks([task], weight_map, sel_labels)
    await _odoo(resolve_user_names, enriched, creds["uid"], creds["api_key"])
    await _odoo(resolve_tag_names, enriched, creds["uid"], creds["api_key"])
    task = enriched[0]
    task["description"] = sanitize_html(task.get("description"))

    return {
        "task": task,
        "messages": messages,
    }


@app.get("/api/ticket/detail/{ticket_id}")
async def api_ticket_detail(request: Request, ticket_id: int):
    """Fetch full helpdesk ticket details + chatter messages."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    tickets = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "helpdesk.ticket",
        [("id", "=", ticket_id)],
        TICKET_FIELDS + ["description"], 1,
    )

    if not tickets:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Run the single ticket through the same enrichment pipeline the
    # backlog uses so the response shape matches api_task_detail. That
    # lets updateBacklogRow() on the frontend repaint ticket rows
    # without branching on item_type.
    weight_map = await _odoo(get_weight_map_cached, creds["uid"], creds["api_key"])
    sel_labels = await _odoo(get_sel_labels_cached, creds["uid"], creds["api_key"])
    enriched = enrich_tickets(tickets, weight_map, sel_labels)
    await _odoo(resolve_tag_names, enriched, creds["uid"], creds["api_key"],
                "helpdesk.tag", "helpdesk_tag_names")
    ticket = enriched[0]
    ticket["description"] = sanitize_html(ticket.get("description"))

    # Messages
    messages = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "mail.message",
        [("res_id", "=", ticket_id), ("model", "=", "helpdesk.ticket"),
         ("message_type", "in", ["comment", "email", "notification"])],
        ["body", "date", "author_id", "message_type", "subtype_id"],
    )
    messages.sort(key=lambda m: m.get("date", ""), reverse=True)
    for msg in messages:
        if isinstance(msg.get("author_id"), (list, tuple)):
            msg["_author"] = msg["author_id"][1]
        else:
            msg["_author"] = "System"
        msg["body"] = sanitize_html(msg.get("body"))

    return {"ticket": ticket, "messages": messages}


@app.get("/api/project/{project_id}")
async def api_project_detail(request: Request, project_id: int):
    """Fetch project details and its open tasks."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    projects = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "project.project",
        [("id", "=", project_id)],
        ["name", "user_id", "partner_id", "date_start", "date",
         "task_count", "description"], 1,
    )

    if not projects:
        raise HTTPException(status_code=404, detail="Project not found")

    project = projects[0]

    # Get open tasks in this project
    tasks = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "project.task",
        [("project_id", "=", project_id),
         ("stage_id.name", "not in", list(EXCLUDED_STAGES))],
        ["id", "name", "stage_id", "user_ids", "x_studio_issue_type",
         "x_studio_level_of_effort", "create_date"],
    )

    weight_map = await _odoo(get_weight_map_cached, creds["uid"], creds["api_key"])
    sel_labels = await _odoo(get_sel_labels_cached, creds["uid"], creds["api_key"])

    for t in tasks:
        t["_score"] = score_task(t, weight_map, sel_labels)
        t["_stage"] = t["stage_id"][1] if isinstance(t.get("stage_id"), (list, tuple)) else ""

    tasks.sort(key=lambda t: t["_score"])
    project["description"] = sanitize_html(project.get("description"))

    return {"project": project, "tasks": tasks}


@app.post("/api/task/{task_id}/comment")
async def api_post_task_comment(request: Request, task_id: int):
    """Post a comment to a project.task's chatter as the logged-in user."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    body = data.get("body", "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment body is required")

    # Treat the user's input as plain text — escape HTML, then add <br/>
    # for line breaks. Prevents stored XSS via comment field.
    html_body = html_escape(body).replace("\n", "<br/>")

    await _odoo(odoo_message_post, creds["uid"], creds["api_key"],
                "project.task", task_id, html_body)
    return {"ok": True}


@app.post("/api/ticket/{ticket_id}/comment")
async def api_post_ticket_comment(request: Request, ticket_id: int):
    """Post a comment to a helpdesk.ticket's chatter as the logged-in user."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    body = data.get("body", "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment body is required")

    html_body = html_escape(body).replace("\n", "<br/>")

    await _odoo(odoo_message_post, creds["uid"], creds["api_key"],
                "helpdesk.ticket", ticket_id, html_body)
    return {"ok": True}


@app.post("/api/tasks/bulk-update")
async def api_bulk_update(request: Request):
    """Bulk update multiple project.task records."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    task_ids = data.get("task_ids", [])
    values = {}

    if data.get("stage_id"):
        values["stage_id"] = int(data["stage_id"])
    if data.get("user_ids") is not None:
        # user_ids is a many2many — use [(6, 0, [ids])] to replace
        user_list = data["user_ids"]
        if isinstance(user_list, list):
            values["user_ids"] = [(6, 0, [int(u) for u in user_list])]
        elif user_list:
            values["user_ids"] = [(6, 0, [int(user_list)])]

    if not task_ids or not values:
        raise HTTPException(status_code=400, detail="task_ids and at least one field required")

    try:
        await _odoo(odoo_write, creds["uid"], creds["api_key"], "project.task",
                    [int(t) for t in task_ids], values)
    except Exception as e:
        raise _odoo_error_response("Bulk task update", e)

    cache_clear(creds["uid"], "open_tasks")
    cache_clear(creds["uid"], "open_tasks_gantt")
    cache_clear(creds["uid"], "open_tickets")
    return {"ok": True, "updated": len(task_ids)}


@app.post("/api/ticket/{ticket_id}/update-status")
async def api_update_ticket_status(request: Request, ticket_id: int):
    """Update a helpdesk ticket's stage."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    stage_id = data.get("stage_id")
    if not stage_id:
        raise HTTPException(status_code=400, detail="stage_id is required")

    try:
        await _odoo(odoo_write, creds["uid"], creds["api_key"], "helpdesk.ticket",
                    [ticket_id], {"stage_id": int(stage_id)})
    except Exception as e:
        raise _odoo_error_response("Ticket status update", e)

    # Bust the cached task/ticket lists so the next /backlog render
    # reflects the new stage (a closed ticket should drop off the open
    # list immediately, not in 15 minutes).
    cache_clear(creds["uid"], "open_tickets")
    cache_clear(creds["uid"], "open_tasks")
    cache_clear(creds["uid"], "open_tasks_gantt")

    return {"ok": True}


@app.post("/api/ticket/{ticket_id}/update")
async def api_update_ticket(request: Request, ticket_id: int):
    """Partial update of a helpdesk ticket. Accepts the same body shape
    as /api/task/{id}/update — frontend sends only the fields that
    changed (dirty-tracking). convert_value handles the type coercion.

    Note: tickets use single user_id (many2one), not many2many like
    tasks. partner_id is single m2o. tag_ids is the helpdesk.tag m2m.
    """
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()

    # Mapping: incoming form field → (Odoo field on helpdesk.ticket, ftype).
    # The frontend uses the same field names as the task panel so panel
    # save logic is identical; the server routes them to the right Odoo
    # field here.
    ticket_field_map = {
        "name": ("name", "string"),
        "stage_id": ("stage_id", "int"),
        "priority": ("priority", "string"),
        "user_id": ("user_id", "int_or_false"),
        "x_studio_customer": ("partner_id", "int_or_false"),
        "x_studio_issue_type": ("x_studio_customer_impact", "string_or_false"),
        "x_studio_related_field_5vi_1jnfmj9cf": ("x_studio_escalated", "bool"),
        "x_studio_related_field_gd_1jnftb4gl": ("x_studio_customer_funded", "string_or_false"),
        "x_studio_related_field_27d_1jnftbs3p": ("x_studio_paid_prioritization", "bool"),
        "tag_ids": ("tag_ids", "many2many_replace"),
    }

    values = {}
    for key, (odoo_field, ftype) in ticket_field_map.items():
        if key not in data:
            continue
        result = convert_value(data[key], ftype)
        if result is not _SKIP:
            values[odoo_field] = result

    if not values:
        return {"ok": True, "updated": 0}

    try:
        await _odoo(odoo_write, creds["uid"], creds["api_key"], "helpdesk.ticket",
                    [ticket_id], values)
    except Exception as e:
        raise _odoo_error_response("Ticket update", e)

    # Writes to a ticket also flow back to the linked project.task via
    # Odoo Studio related fields, so the task-side caches must be busted
    # too — otherwise the Projects backlog renders stale data.
    cache_clear(creds["uid"], "open_tickets")
    cache_clear(creds["uid"], "open_tasks")
    cache_clear(creds["uid"], "open_tasks_gantt")

    return {"ok": True, "updated": len(values)}


@app.get("/api/ticket/lookup/{ticket_ref}")
async def api_ticket_lookup(request: Request, ticket_ref: str):
    """Look up a helpdesk ticket by ticket_ref (Bugzilla ID) and return
    full details + messages, same as the task detail endpoint."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Find the helpdesk ticket by ticket_ref
    tickets = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "helpdesk.ticket",
        [("ticket_ref", "=", ticket_ref)],
        ["id", "name", "ticket_ref", "stage_id", "create_date",
         "partner_id", "user_id", "description", "priority"], 1,
    )

    if not tickets:
        raise HTTPException(status_code=404, detail=f"No ticket found with ID #{ticket_ref}")

    ticket = tickets[0]

    # Also try to find a linked project.task
    task_data = None
    tasks = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "project.task",
        [("helpdesk_ticket_id", "=", ticket["id"])],
        TASK_FIELDS + ["description", "date_deadline", "date_assign",
                       "allocated_hours", "effective_hours"], 1,
    )
    if tasks:
        task_data = tasks[0]
        weight_map = await _odoo(get_weight_map_cached, creds["uid"], creds["api_key"])
        sel_labels_lookup = await _odoo(get_sel_labels_cached, creds["uid"], creds["api_key"])
        task_data["_score"] = score_task(task_data, weight_map, sel_labels_lookup)
        task_data["_age"] = compute_age_bracket(task_data.get("create_date", ""))

    # Fetch chatter messages from the helpdesk ticket
    messages = await _odoo(
        odoo_search_read,
        creds["uid"], creds["api_key"], "mail.message",
        [
            ("res_id", "=", ticket["id"]),
            ("model", "=", "helpdesk.ticket"),
            ("message_type", "in", ["comment", "email", "notification"]),
        ],
        ["body", "date", "author_id", "message_type", "subtype_id",
         "attachment_ids"], 50,
    )

    messages.sort(key=lambda m: m.get("date", ""), reverse=True)

    # Enrich messages with author names
    for msg in messages:
        if isinstance(msg.get("author_id"), (list, tuple)):
            msg["_author"] = msg["author_id"][1]
        else:
            msg["_author"] = "System"
        msg["body"] = sanitize_html(msg.get("body"))
    ticket["description"] = sanitize_html(ticket.get("description"))

    return {
        "ticket": ticket,
        "task": task_data,
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------
@app.post("/api/presence/heartbeat")
async def presence_heartbeat(request: Request):
    creds = get_session_creds(request)
    if not creds:
        return {"ok": False}
    import time
    active_users[creds["login"]] = time.time()
    return {"ok": True}


@app.get("/api/presence/online")
async def presence_online(request: Request):
    import time
    now = time.time()
    online = [login for login, ts in active_users.items()
              if now - ts < PRESENCE_TIMEOUT]
    return {"users": online}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
