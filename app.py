"""
ACR Prioritization Engine — Standalone Web App
Talks to Odoo via JSON-RPC. Auth via Odoo API keys.
"""
import json
import logging
import xmlrpc.client
from datetime import date
from pathlib import Path

import time as _time
from collections import defaultdict
from datetime import datetime as dt

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer
from starlette.middleware.sessions import SessionMiddleware

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
import os

app = FastAPI(title="ACR Prioritization Engine")
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "acr-priority-dev-secret-change-in-prod"))
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

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

ODOO_URL = os.environ.get("ODOO_URL", "https://odoo-ps-psus-all-about-technology-sandbox-30173849.dev.odoo.com")
ODOO_DB = os.environ.get("ODOO_DB", "odoo-ps-psus-all-about-technology-sandbox-30173849")

# Odoo models for weight storage
ATTR_MODEL = "x_acr_priority_attribute"
WEIGHT_MODEL = "x_acr_priority_weight"

# Stages to exclude from the backlog
EXCLUDED_STAGES = {"Complete", "Complete_1", "Cancelled"}

# Fields to pull from project.task
TASK_FIELDS = [
    "id", "name", "stage_id", "create_date", "user_ids",
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
# Weight storage (Odoo)
# ---------------------------------------------------------------------------
def load_weight_map(uid: int, api_key: str) -> dict:
    """Load all weights from Odoo into a dict for scoring."""
    attrs = odoo_search_read(uid, api_key, ATTR_MODEL, [],
                             ["x_name", "x_task_field", "x_field_type", "x_sequence"])

    result = {}
    for attr in attrs:
        weights = odoo_search_read(uid, api_key, WEIGHT_MODEL,
                                   [("x_attribute_id", "=", attr["id"])],
                                   ["x_value", "x_weight"])
        result[attr["x_task_field"]] = {
            "name": attr["x_name"],
            "field_type": attr["x_field_type"],
            "values": {w["x_value"]: w["x_weight"] for w in weights},
        }
    return result


def load_attributes_for_config(uid: int, api_key: str) -> list:
    """Load all attributes with their weights for the config page."""
    attrs = odoo_search_read(uid, api_key, ATTR_MODEL, [],
                             ["x_name", "x_task_field", "x_field_type", "x_sequence"])
    attrs.sort(key=lambda a: a.get("x_sequence", 0))

    result = []
    for attr in attrs:
        weights = odoo_search_read(uid, api_key, WEIGHT_MODEL,
                                   [("x_attribute_id", "=", attr["id"])],
                                   ["x_value", "x_weight", "x_description"])
        weights.sort(key=lambda w: w.get("x_weight", 0))
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
                for w in weights
            ],
        })
    return result


# ---------------------------------------------------------------------------
# Odoo RPC helpers
# ---------------------------------------------------------------------------
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


# Cache for selection field key→label mappings
_selection_labels_cache: dict | None = None


def get_selection_labels(uid: int, api_key: str) -> dict:
    """Load selection field key→label mappings from Odoo. Cached per process."""
    global _selection_labels_cache
    if _selection_labels_cache is not None:
        return _selection_labels_cache

    models_proxy = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    field_defs = models_proxy.execute_kw(
        ODOO_DB, uid, api_key,
        "project.task", "fields_get",
        [["x_studio_issue_type", "x_studio_level_of_effort",
          "x_studio_related_field_gd_1jnftb4gl"]],
        {"attributes": ["selection"]},
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
    try:
        uid = odoo_authenticate(login, api_key)
    except Exception:
        return RedirectResponse("/login?error=Authentication+failed.+Check+your+email+and+API+key.", status_code=303)
    request.session["uid"] = uid
    request.session["api_key"] = api_key
    request.session["login"] = login
    return RedirectResponse("/backlog", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/backlog", response_class=HTMLResponse)
async def backlog(request: Request):
    creds = get_session_creds(request)
    if not creds:
        return RedirectResponse("/login", status_code=303)

    # Pull open tasks (cached)
    tasks = get_open_tasks_cached(creds["uid"], creds["api_key"])
    weight_map = get_weight_map_cached(creds["uid"], creds["api_key"])
    sel_labels = get_sel_labels_cached(creds["uid"], creds["api_key"])
    # Deep copy so enrichment doesn't mutate cache
    tasks = [dict(t) for t in tasks]
    tasks = enrich_tasks(tasks, weight_map, sel_labels)
    resolve_user_names(tasks, creds["uid"], creds["api_key"])

    task_json = json.dumps(tasks, default=str)

    thresholds = compute_score_thresholds(tasks)

    return templates.TemplateResponse(request, "backlog.html", {
        "tasks": tasks,
        "task_json": task_json,
        "login": creds["login"],
        "field_labels": FIELD_LABELS,
        "task_count": len(tasks),
        "thresholds": thresholds,
    })


@app.get("/gantt", response_class=HTMLResponse)
async def gantt_page(request: Request):
    creds = get_session_creds(request)
    if not creds:
        return RedirectResponse("/login", status_code=303)

    # Pull open tasks with date fields (cached)
    gantt_extra = ["planned_date_begin", "date_end", "date_deadline", "user_ids"]
    tasks = get_open_tasks_cached(creds["uid"], creds["api_key"], extra_fields=gantt_extra)
    weight_map = get_weight_map_cached(creds["uid"], creds["api_key"])
    sel_labels = get_sel_labels_cached(creds["uid"], creds["api_key"])
    tasks = [dict(t) for t in tasks]
    tasks = enrich_tasks(tasks, weight_map, sel_labels)
    resolve_user_names(tasks, creds["uid"], creds["api_key"])

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

    tasks = get_open_tasks_cached(creds["uid"], creds["api_key"])
    weight_map = get_weight_map_cached(creds["uid"], creds["api_key"])
    sel_labels = get_sel_labels_cached(creds["uid"], creds["api_key"])

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
        odoo_write(creds["uid"], creds["api_key"], "project.task", [task_id], values)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    cache_clear(creds["uid"], "open_tasks")
    cache_clear(creds["uid"], "open_tasks_gantt")
    return {"ok": True}


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    creds = get_session_creds(request)
    if not creds:
        return RedirectResponse("/login", status_code=303)

    attributes = load_attributes_for_config(creds["uid"], creds["api_key"])

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
        odoo_write(creds["uid"], creds["api_key"], WEIGHT_MODEL, [weight_id],
                   {"x_weight": weight})
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
    stages = odoo_search_read(
        creds["uid"], creds["api_key"], "project.task.type",
        [], ["name"], limit=100,
    )

    # Fetch customers (partners used as x_studio_customer)
    customers = odoo_search_read(
        creds["uid"], creds["api_key"], "res.partner",
        [("customer_rank", ">", 0)], ["name"], limit=200,
    )
    # If no customer_rank results, try getting all companies
    if not customers:
        customers = odoo_search_read(
            creds["uid"], creds["api_key"], "res.partner",
            [("is_company", "=", True)], ["name"], limit=200,
        )

    # Selection field values from the Odoo field definitions
    models_proxy = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    field_defs = models_proxy.execute_kw(
        ODOO_DB, creds["uid"], creds["api_key"],
        "project.task", "fields_get",
        [["x_studio_issue_type", "x_studio_level_of_effort",
          "x_studio_related_field_gd_1jnftb4gl", "priority"]],
        {"attributes": ["selection"]},
    )

    # Fetch users (assignees)
    users = odoo_search_read(
        creds["uid"], creds["api_key"], "res.users",
        [("share", "=", False)], ["name"], limit=200,
    )

    return {
        "stages": [{"id": s["id"], "name": s["name"]} for s in stages],
        "customers": [{"id": c["id"], "name": c["name"]} for c in customers],
        "users": [{"id": u["id"], "name": u["name"]} for u in users],
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
    }

    # Fields that live on the linked helpdesk.ticket (related fields)
    ticket_field_map = {
        "x_studio_customer": ("partner_id", "int_or_false"),
        "x_studio_issue_type": ("x_studio_customer_impact", "string_or_false"),
        "x_studio_related_field_5vi_1jnfmj9cf": ("x_studio_escalated", "bool"),
        "x_studio_related_field_gd_1jnftb4gl": ("x_studio_customer_funded", "string_or_false"),
        "x_studio_related_field_27d_1jnftbs3p": ("x_studio_paid_prioritization", "bool"),
    }

    def convert_value(val, ftype):
        if val == "" and ftype not in ("bool",):
            return None  # skip
        if ftype == "bool":
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes")
            return bool(val)
        elif ftype in ("int", "int_or_false"):
            return int(val) if val and str(val).strip() else False
        elif ftype == "float":
            return float(val) if val else 0.0
        elif ftype == "date_or_false":
            return val if val else False
        elif ftype == "string_or_false":
            return val if val else False
        elif ftype == "many2many_single":
            return [(6, 0, [int(val)])] if val else [(5, 0, 0)]
        return val

    # Process task fields
    task_values = {}
    for key, (odoo_field, ftype) in task_field_map.items():
        if key not in data:
            continue
        result = convert_value(data[key], ftype)
        if result is not None:
            task_values[odoo_field] = result

    # Process ticket fields
    ticket_values = {}
    for key, (odoo_field, ftype) in ticket_field_map.items():
        if key not in data:
            continue
        result = convert_value(data[key], ftype)
        if result is not None:
            ticket_values[odoo_field] = result

    updated = 0

    # Write task fields
    if task_values:
        try:
            odoo_write(creds["uid"], creds["api_key"], "project.task", [task_id], task_values)
            updated += len(task_values)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Task update failed: {e}")

    # Write ticket fields (if there's a linked ticket)
    if ticket_values:
        # Look up the linked helpdesk ticket
        task_rec = odoo_search_read(
            creds["uid"], creds["api_key"], "project.task",
            [("id", "=", task_id)],
            ["helpdesk_ticket_id"],
            limit=1,
        )
        ticket_id_link = None
        if task_rec and task_rec[0].get("helpdesk_ticket_id"):
            ht = task_rec[0]["helpdesk_ticket_id"]
            ticket_id_link = ht[0] if isinstance(ht, (list, tuple)) else ht

        if ticket_id_link:
            try:
                odoo_write(creds["uid"], creds["api_key"], "helpdesk.ticket",
                           [ticket_id_link], ticket_values)
                updated += len(ticket_values)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Ticket update failed: {e}")
        else:
            logging.warning(f"Task {task_id} has no linked helpdesk ticket — "
                            f"skipping ticket fields: {list(ticket_values.keys())}")

    # Invalidate task cache since data changed
    cache_clear(creds["uid"], "open_tasks")
    cache_clear(creds["uid"], "open_tasks_gantt")

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

    tasks = odoo_search_read(
        creds["uid"], creds["api_key"], "project.task",
        [("id", "=", task_id)],
        detail_fields,
        limit=1,
    )

    if not tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[0]

    # Fetch chatter messages (mail.message)
    messages = odoo_search_read(
        creds["uid"], creds["api_key"], "mail.message",
        [
            ("res_id", "=", task_id),
            ("model", "=", "project.task"),
            ("message_type", "in", ["comment", "email", "notification"]),
        ],
        ["body", "date", "author_id", "message_type", "subtype_id",
         "attachment_ids"],
        limit=0,
    )

    # Sort messages newest first
    messages.sort(key=lambda m: m.get("date", ""), reverse=True)

    # Fetch attachment info if any messages have them
    all_attachment_ids = []
    for msg in messages:
        all_attachment_ids.extend(msg.get("attachment_ids", []))

    attachments = {}
    if all_attachment_ids:
        att_records = odoo_search_read(
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

    # Compute score
    weight_map = get_weight_map_cached(creds["uid"], creds["api_key"])
    sel_labels = get_sel_labels_cached(creds["uid"], creds["api_key"])
    task["_score"] = score_task(task, weight_map, sel_labels)
    task["_age"] = compute_age_bracket(task.get("create_date", ""))
    task["_stage"] = task["stage_id"][1] if isinstance(task.get("stage_id"), (list, tuple)) else ""
    task["_customer"] = ""
    cust = task.get("x_studio_customer")
    if isinstance(cust, (list, tuple)) and len(cust) > 1:
        task["_customer"] = cust[1]
    task["_grooming"] = compute_grooming(task)

    return {
        "task": task,
        "messages": messages,
    }


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

    # Convert newlines to <br> for HTML
    html_body = body.replace("\n", "<br/>")

    models_proxy = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    models_proxy.execute_kw(
        ODOO_DB, creds["uid"], creds["api_key"],
        "project.task", "message_post", [task_id],
        {
            "body": html_body,
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_comment",
        },
    )

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

    html_body = body.replace("\n", "<br/>")

    models_proxy = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    models_proxy.execute_kw(
        ODOO_DB, creds["uid"], creds["api_key"],
        "helpdesk.ticket", "message_post", [ticket_id],
        {
            "body": html_body,
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_comment",
        },
    )

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
        odoo_write(creds["uid"], creds["api_key"], "project.task",
                   [int(t) for t in task_ids], values)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    cache_clear(creds["uid"], "open_tasks")
    cache_clear(creds["uid"], "open_tasks_gantt")
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
        odoo_write(creds["uid"], creds["api_key"], "helpdesk.ticket",
                   [ticket_id], {"stage_id": int(stage_id)})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"ok": True}


@app.get("/api/ticket/lookup/{ticket_ref}")
async def api_ticket_lookup(request: Request, ticket_ref: str):
    """Look up a helpdesk ticket by ticket_ref (Bugzilla ID) and return
    full details + messages, same as the task detail endpoint."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Find the helpdesk ticket by ticket_ref
    tickets = odoo_search_read(
        creds["uid"], creds["api_key"], "helpdesk.ticket",
        [("ticket_ref", "=", ticket_ref)],
        ["id", "name", "ticket_ref", "stage_id", "create_date",
         "partner_id", "user_id", "description", "priority"],
        limit=1,
    )

    if not tickets:
        raise HTTPException(status_code=404, detail=f"No ticket found with ID #{ticket_ref}")

    ticket = tickets[0]

    # Also try to find a linked project.task
    task_data = None
    tasks = odoo_search_read(
        creds["uid"], creds["api_key"], "project.task",
        [("helpdesk_ticket_id", "=", ticket["id"])],
        TASK_FIELDS + ["description", "date_deadline", "date_assign",
                       "allocated_hours", "effective_hours"],
        limit=1,
    )
    if tasks:
        task_data = tasks[0]
        weight_map = get_weight_map_cached(creds["uid"], creds["api_key"])
        sel_labels_lookup = get_sel_labels_cached(creds["uid"], creds["api_key"])
        task_data["_score"] = score_task(task_data, weight_map, sel_labels_lookup)
        task_data["_age"] = compute_age_bracket(task_data.get("create_date", ""))

    # Fetch chatter messages from the helpdesk ticket
    messages = odoo_search_read(
        creds["uid"], creds["api_key"], "mail.message",
        [
            ("res_id", "=", ticket["id"]),
            ("model", "=", "helpdesk.ticket"),
            ("message_type", "in", ["comment", "email", "notification"]),
        ],
        ["body", "date", "author_id", "message_type", "subtype_id",
         "attachment_ids"],
        limit=50,
    )

    messages.sort(key=lambda m: m.get("date", ""), reverse=True)

    # Enrich messages with author names
    for msg in messages:
        if isinstance(msg.get("author_id"), (list, tuple)):
            msg["_author"] = msg["author_id"][1]
        else:
            msg["_author"] = "System"

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
