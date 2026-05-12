"""Shared fixtures. Set required env vars BEFORE app.py is imported so we can
test against the fail-loud SECRET_KEY check without bypassing it."""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("ODOO_URL", "https://localhost-nowhere.invalid")
os.environ.setdefault("ODOO_DB", "test-db")
os.environ.setdefault("ACR_ALLOWED_ORIGINS", "http://testserver,http://localhost:8000")

import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# FakeOdoo — in-memory stand-in for the Odoo XML-RPC server
# ---------------------------------------------------------------------------
# Routes call odoo_authenticate / odoo_search_read / odoo_write /
# odoo_message_post / odoo_fields_get. These are sync helpers in app.py
# that talk to Odoo. We replace them with FakeOdoo's methods so route
# tests run in isolation and we can assert exactly which Odoo calls
# happened with which arguments.

VALID_API_KEY = "valid-test-key"
TEST_UID = 1308

# Canonical canned data — each test gets a fresh deepcopy so mutations
# don't leak across tests.
_TASKS = [
    {
        "id": 4997,
        "name": "ACR-5000 Integration Platform (Inbound)",
        "stage_id": [30, "New_1"],
        "create_date": "2026-04-10 14:00:00",
        "user_ids": [],
        "tag_ids": [],
        "project_id": [75, "Bugs - 2021"],
        "x_studio_customer": False,
        "x_studio_issue_type": False,
        "x_studio_level_of_effort": False,
        "x_studio_road_map_flag": False,
        "x_studio_related_field_5vi_1jnfmj9cf": False,
        "x_studio_related_field_gd_1jnftb4gl": False,
        "x_studio_related_field_27d_1jnftbs3p": False,
        "helpdesk_ticket_id": False,
        "description": "<p>Migrated from Bugzilla</p>",
        "date_deadline": False,
        "date_end": False,
        "date_assign": False,
        "planned_date_begin": False,
        "tag_ids": [],
        "priority": "0",
        "partner_id": False,
        "partner_name": False,
        "partner_phone": False,
        "email_from": False,
        "allocated_hours": 0.0,
        "effective_hours": 0.0,
        "remaining_hours": 0.0,
    },
    {
        "id": 3826,
        "name": "JC keyboard shifted off screen",
        "stage_id": [30, "New_1"],
        "create_date": "2026-04-01 09:00:00",
        "user_ids": [1297],
        "tag_ids": [1],  # tagged "Bug"
        "project_id": [75, "Bugs - 2021"],
        "x_studio_customer": [9, "Schnuck Markets, Inc."],
        "x_studio_issue_type": "Minor",
        "x_studio_level_of_effort": False,
        "x_studio_road_map_flag": False,
        "x_studio_related_field_5vi_1jnfmj9cf": False,
        "x_studio_related_field_gd_1jnftb4gl": "Yes",
        "x_studio_related_field_27d_1jnftbs3p": False,
        "helpdesk_ticket_id": [101274, "JC keyboard shifted off screen (#57077)"],
        "description": "<p>Linked to Bugzilla #57077</p>",
        "date_deadline": False,
        "date_end": False,
        "date_assign": False,
        "planned_date_begin": False,
        "priority": "0",
        "partner_id": False,
        "partner_name": False,
        "partner_phone": False,
        "email_from": False,
        "allocated_hours": 0.0,
        "effective_hours": 0.0,
        "remaining_hours": 0.0,
    },
]

_TICKETS = [
    {
        "id": 101274,
        "name": "JC keyboard shifted off screen",
        "stage_id": [1, "New"],
        "ticket_ref": "57077",
        "create_date": "2026-04-01 09:00:00",
        "user_id": [1297, "Andrew Temple"],
        "partner_id": [9, "Schnuck Markets, Inc."],
        "tag_ids": [50],  # "Customer Reported" (helpdesk.tag)
        "x_studio_customer_impact": "Minor",
        "x_studio_customer_funded": "Yes",
        "x_studio_escalated": False,
        "x_studio_paid_prioritization": False,
        "description": "<p>Real customer issue</p>",
        "priority": "0",
    },
]

_USERS = [
    {"id": TEST_UID, "name": "Darcy Reno"},
    {"id": 1297, "name": "Andrew Temple"},
    {"id": 1302, "name": "Jim Lockwood"},
]

_PARTNERS = [
    {"id": 9, "name": "Schnuck Markets, Inc.", "customer_rank": 1, "is_company": True},
    {"id": 6, "name": "Rouses Markets", "customer_rank": 1, "is_company": True},
    {"id": 33, "name": "ACR SYSTEMS, INC.", "customer_rank": 0, "is_company": True},
]

_STAGES = [
    {"id": 30, "name": "New_1"},
    {"id": 133, "name": "Inbox"},
    {"id": 200, "name": "In Progress"},
    {"id": 999, "name": "Complete"},
]

_TAGS = [
    {"id": 1, "name": "Bug"},
    {"id": 2, "name": "Enhancement"},
    {"id": 3, "name": "Development Task"},
    {"id": 4, "name": "Roadmap"},
]

# helpdesk.tag is a separate namespace from project.tags. In the real
# Odoo it's currently empty — we seed a couple here so tests can
# exercise the helpdesk-side flow.
_HELPDESK_TAGS = [
    {"id": 50, "name": "Customer Reported"},
    {"id": 51, "name": "Internal Triage"},
]

_PROJECTS = [
    {
        "id": 75, "name": "Bugs - 2021",
        "user_id": [TEST_UID, "Darcy Reno"],
        "partner_id": [9, "Schnuck Markets, Inc."],
        "date_start": "2021-01-01", "date": False,
        "task_count": 2, "description": "<p>Bug tracking</p>",
    },
]

_MESSAGES = [
    {
        "id": 1, "res_id": 3826, "model": "project.task",
        "message_type": "comment",
        "body": "<p>First note on this task</p>",
        "date": "2026-04-02 10:00:00",
        "author_id": [1297, "Andrew Temple"],
        "subtype_id": [1, "Comment"],
        "attachment_ids": [],
    },
]

_PRIORITY_ATTRS = [
    {"id": 1, "x_name": "Customer", "x_task_field": "x_studio_customer",
     "x_field_type": "many2one", "x_sequence": 1},
    {"id": 2, "x_name": "Issue Type", "x_task_field": "x_studio_issue_type",
     "x_field_type": "selection", "x_sequence": 3},
]

_PRIORITY_WEIGHTS = [
    {"id": 10, "x_value": "Schnuck Markets, Inc.", "x_weight": 1,
     "x_description": "", "x_attribute_id": [1, "Customer"]},
    {"id": 11, "x_value": "Non-critical Workflow Bug", "x_weight": 4,
     "x_description": "", "x_attribute_id": [2, "Issue Type"]},
    {"id": 12, "x_value": "System stopping bug - No workaround", "x_weight": -15,
     "x_description": "", "x_attribute_id": [2, "Issue Type"]},
]

_FIELD_DEFS = {
    "project.task": {
        "x_studio_issue_type": {
            "selection": [
                ["Critical", "System stopping bug - No workaround"],
                ["Minor", "Non-critical Workflow Bug"],
                ["Enhancement", "Enhancement"],
            ],
        },
        "x_studio_level_of_effort": {
            "selection": [
                ["<10 Hrs", "<10 Hrs"],
                ["10-40 Hrs", "10-40 Hrs"],
                [">100 Hrs", ">100 Hrs"],
            ],
        },
        "x_studio_related_field_gd_1jnftb4gl": {
            "selection": [["Yes", "Yes"], ["No", "No"]],
        },
        "priority": {"selection": [["0", "Normal"], ["1", "High"]]},
    },
}


class FakeOdoo:
    """In-memory stand-in for the Odoo XML-RPC server.

    Routes call odoo_* helpers; tests monkey-patch those helpers to
    delegate to a FakeOdoo instance. Records every write/message_post
    so tests can assert exactly which Odoo calls happened.
    """

    def __init__(self):
        self.tasks = {t["id"]: deepcopy(t) for t in _TASKS}
        self.tickets = {t["id"]: deepcopy(t) for t in _TICKETS}
        self.users = {u["id"]: deepcopy(u) for u in _USERS}
        self.partners = {p["id"]: deepcopy(p) for p in _PARTNERS}
        self.stages = {s["id"]: deepcopy(s) for s in _STAGES}
        self.projects = {p["id"]: deepcopy(p) for p in _PROJECTS}
        self.tags = {t["id"]: deepcopy(t) for t in _TAGS}
        self.helpdesk_tags = {t["id"]: deepcopy(t) for t in _HELPDESK_TAGS}
        self.messages = list(deepcopy(_MESSAGES))
        self.attrs = list(deepcopy(_PRIORITY_ATTRS))
        self.weights = list(deepcopy(_PRIORITY_WEIGHTS))
        # Call log for assertions
        self.writes = []           # list of (model, ids, values)
        self.message_posts = []    # list of (model, record_id, body)
        self.auth_attempts = []    # list of (login, api_key)

    # ---- auth ----
    def authenticate(self, login, api_key):
        self.auth_attempts.append((login, api_key))
        return TEST_UID if api_key == VALID_API_KEY else False

    # ---- search_read ----
    def search_read(self, uid, api_key, model, domain, fields, limit=0):
        rows = self._collection_for(model)
        filtered = [r for r in rows if self._matches(r, domain)]
        if limit:
            filtered = filtered[:limit]
        return [self._project(r, fields) for r in filtered]

    def _collection_for(self, model):
        m = {
            "project.task": list(self.tasks.values()),
            "helpdesk.ticket": list(self.tickets.values()),
            "res.users": list(self.users.values()),
            "res.partner": list(self.partners.values()),
            "project.task.type": list(self.stages.values()),
            "project.project": list(self.projects.values()),
            "project.tags": list(self.tags.values()),
            "helpdesk.tag": list(self.helpdesk_tags.values()),
            "mail.message": self.messages,
            "x_acr_priority_attribute": self.attrs,
            "x_acr_priority_weight": self.weights,
            "ir.attachment": [],
        }
        return m.get(model, [])

    def _matches(self, row, domain):
        for clause in domain:
            field, op, value = clause
            actual = self._read_path(row, field)
            # Odoo m2o reads come back as [id, "Name"]. When the query
            # compares against an int, we mean the id.
            if isinstance(actual, list) and len(actual) >= 1 and isinstance(value, int):
                actual = actual[0]
            if op == "=":
                if actual != value:
                    return False
            elif op == "!=":
                if actual == value:
                    return False
            elif op == "in":
                actual_id = actual[0] if isinstance(actual, list) else actual
                if actual_id not in value:
                    return False
            elif op == "not in":
                actual_name = actual[1] if isinstance(actual, list) else actual
                if actual_name in value:
                    return False
            elif op == ">":
                if not (actual and actual > value):
                    return False
            else:
                # Unsupported operator — keep all to surface as a test bug
                pass
        return True

    @staticmethod
    def _read_path(row, field):
        """Resolve a dotted field path like 'stage_id.name' against a row."""
        if "." not in field:
            return row.get(field)
        head, rest = field.split(".", 1)
        val = row.get(head)
        if isinstance(val, list) and len(val) >= 2 and rest == "name":
            return val[1]
        return None

    @staticmethod
    def _project(row, fields):
        if not fields:
            return dict(row)
        return {"id": row["id"], **{f: row.get(f, False) for f in fields if f != "id"}}

    # ---- write ----
    def write(self, uid, api_key, model, ids, values):
        self.writes.append((model, list(ids), dict(values)))
        store = {"project.task": self.tasks, "helpdesk.ticket": self.tickets}.get(model)
        if store is None:
            return True
        for rid in ids:
            if rid not in store:
                continue
            for k, v in values.items():
                # m2m write commands → simulate (5,0,0) and (6,0,[ids])
                if isinstance(v, list) and v and isinstance(v[0], tuple):
                    op = v[0]
                    if op[:2] == (5, 0):
                        store[rid][k] = []
                    elif op[0] == 6 and len(op) == 3:
                        store[rid][k] = list(op[2])
                else:
                    store[rid][k] = v
        return True

    # ---- message_post ----
    def message_post(self, uid, api_key, model, record_id, body):
        self.message_posts.append((model, record_id, body))
        return True

    # ---- fields_get ----
    def fields_get(self, uid, api_key, model, field_names, attributes):
        defs = _FIELD_DEFS.get(model, {})
        return {f: defs.get(f, {"selection": []}) for f in field_names}


@pytest.fixture
def fake_odoo(monkeypatch):
    """Replace the Odoo helpers in app.py with a fresh FakeOdoo for the test."""
    import app

    f = FakeOdoo()

    monkeypatch.setattr(app, "odoo_authenticate",
                        lambda login, key: (
                            f.authenticate(login, key) or
                            (_ for _ in ()).throw(ValueError("Authentication failed"))
                        ))
    monkeypatch.setattr(app, "odoo_search_read",
                        lambda uid, key, model, domain, fields, limit=0:
                        f.search_read(uid, key, model, domain, fields, limit))
    monkeypatch.setattr(app, "odoo_write",
                        lambda uid, key, model, ids, values:
                        f.write(uid, key, model, ids, values))
    monkeypatch.setattr(app, "odoo_message_post",
                        lambda uid, key, model, rid, body:
                        f.message_post(uid, key, model, rid, body))
    monkeypatch.setattr(app, "odoo_fields_get",
                        lambda uid, key, model, names, attrs:
                        f.fields_get(uid, key, model, names, attrs))

    # Wipe process-level caches that other tests may have populated
    app._data_cache.clear()
    app.active_users.clear()
    app._selection_labels_cache = None
    app._login_attempts.clear()

    return f


@pytest.fixture
def client(fake_odoo):
    """Unauthenticated TestClient."""
    from fastapi.testclient import TestClient
    import app
    return TestClient(app.app)


@pytest.fixture
def authed_client(fake_odoo):
    """TestClient with an established session (post-login)."""
    from fastapi.testclient import TestClient
    import app
    c = TestClient(app.app)
    r = c.post(
        "/login",
        data={"login": "darcy@allabout.technology", "api_key": VALID_API_KEY},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"login fixture failed: {r.status_code} {r.text}"
    return c
