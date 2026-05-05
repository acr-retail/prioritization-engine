"""
ACR Prioritization Engine — Standalone Web App
Talks to Odoo via JSON-RPC. Auth via Odoo API keys.
"""
import json
import xmlrpc.client
from datetime import date
from pathlib import Path

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


def get_field_display(task: dict, field_name: str, field_type: str) -> str:
    """Extract the display value from an Odoo task record."""
    if field_type == "computed":
        return compute_age_bracket(task.get("create_date", ""))

    raw = task.get(field_name)
    if raw is False or raw is None:
        return ""

    if field_type == "boolean":
        return "True" if raw else "False"

    if field_type == "many2one" and isinstance(raw, (list, tuple)):
        return raw[1] if len(raw) > 1 else str(raw[0])

    return str(raw) if raw else ""


def score_task(task: dict, weight_map: dict) -> int:
    score = 0
    for field_name, config in weight_map.items():
        display_val = get_field_display(task, field_name, config["field_type"])
        if display_val in config["values"]:
            score += config["values"][display_val]
    return score


def enrich_tasks(tasks: list, weight_map: dict) -> list:
    """Add score, age bracket, and friendly field values to each task."""
    for task in tasks:
        task["_score"] = score_task(task, weight_map)
        task["_age"] = compute_age_bracket(task.get("create_date", ""))
        task["_stage"] = task["stage_id"][1] if isinstance(task.get("stage_id"), (list, tuple)) else ""
        task["_customer"] = ""
        cust = task.get("x_studio_customer")
        if isinstance(cust, (list, tuple)) and len(cust) > 1:
            task["_customer"] = cust[1]
        elif cust and cust is not False:
            task["_customer"] = str(cust)
    tasks.sort(key=lambda t: t["_score"])
    return tasks


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

    # Pull open tasks from Odoo
    tasks = odoo_search_read(
        creds["uid"], creds["api_key"], "project.task",
        [("stage_id.name", "not in", list(EXCLUDED_STAGES))],
        TASK_FIELDS,
    )

    weight_map = load_weight_map(creds["uid"], creds["api_key"])
    tasks = enrich_tasks(tasks, weight_map)

    # Build JSON-safe version for the detail panel
    task_json = json.dumps(tasks, default=str)

    return templates.TemplateResponse(request, "backlog.html", {
        "tasks": tasks,
        "task_json": task_json,
        "login": creds["login"],
        "field_labels": FIELD_LABELS,
        "task_count": len(tasks),
    })


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

    return {
        "stages": [{"id": s["id"], "name": s["name"]} for s in stages],
        "customers": [{"id": c["id"], "name": c["name"]} for c in customers],
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

    # Field mapping: form key → (odoo field, type)
    field_map = {
        "name": ("name", "string"),
        "stage_id": ("stage_id", "int"),
        "x_studio_customer": ("x_studio_customer", "int_or_false"),
        "x_studio_issue_type": ("x_studio_issue_type", "string_or_false"),
        "x_studio_level_of_effort": ("x_studio_level_of_effort", "string_or_false"),
        "x_studio_related_field_5vi_1jnfmj9cf": ("x_studio_related_field_5vi_1jnfmj9cf", "bool"),
        "x_studio_related_field_gd_1jnftb4gl": ("x_studio_related_field_gd_1jnftb4gl", "string_or_false"),
        "x_studio_related_field_27d_1jnftbs3p": ("x_studio_related_field_27d_1jnftbs3p", "bool"),
        "x_studio_road_map_flag": ("x_studio_road_map_flag", "bool"),
        "priority": ("priority", "string"),
        "date_deadline": ("date_deadline", "date_or_false"),
        "date_assign": ("date_assign", "date_or_false"),
        "allocated_hours": ("allocated_hours", "float"),
    }

    for key, (odoo_field, ftype) in field_map.items():
        if key not in data:
            continue
        val = data[key]

        if ftype == "bool":
            values[odoo_field] = bool(val)
        elif ftype == "int":
            values[odoo_field] = int(val) if val else False
        elif ftype == "int_or_false":
            values[odoo_field] = int(val) if val else False
        elif ftype == "float":
            values[odoo_field] = float(val) if val else 0.0
        elif ftype == "date_or_false":
            values[odoo_field] = val if val else False
        elif ftype == "string_or_false":
            values[odoo_field] = val if val else False
        else:
            values[odoo_field] = val

    if not values:
        return {"ok": True, "updated": 0}

    try:
        odoo_write(creds["uid"], creds["api_key"], "project.task", [task_id], values)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"ok": True, "updated": len(values)}


@app.get("/api/task/{task_id}")
async def api_task_detail(request: Request, task_id: int):
    """Fetch full task details + chatter messages from Odoo."""
    creds = get_session_creds(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Fetch full task record
    detail_fields = TASK_FIELDS + [
        "description", "date_deadline", "date_assign",
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
        limit=50,
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
    weight_map = load_weight_map(creds["uid"], creds["api_key"])
    task["_score"] = score_task(task, weight_map)
    task["_age"] = compute_age_bracket(task.get("create_date", ""))
    task["_stage"] = task["stage_id"][1] if isinstance(task.get("stage_id"), (list, tuple)) else ""
    task["_customer"] = ""
    cust = task.get("x_studio_customer")
    if isinstance(cust, (list, tuple)) and len(cust) > 1:
        task["_customer"] = cust[1]

    return {
        "task": task,
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
