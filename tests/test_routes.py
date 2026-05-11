"""Route-level integration tests.

Every test uses the fake_odoo fixture from conftest.py — no real RPC.
The goal is to catch:
  * status codes + redirects on auth + CSRF paths
  * response shape (especially the enriched fields on detail endpoints)
  * the exact Odoo write commands that update_task emits
    (convert_value wiring — the assignee-clear regression)
  * cache invalidation on writes
  * sanitization on chatter content
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app
from conftest import VALID_API_KEY, TEST_UID


ORIGIN = {"Origin": "http://testserver"}


# ---------------------------------------------------------------------------
# Auth + index
# ---------------------------------------------------------------------------
class TestAuth:
    def test_index_unauthenticated_redirects_to_login(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"

    def test_index_authenticated_redirects_to_backlog(self, authed_client):
        r = authed_client.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/backlog"

    def test_login_page_renders(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert "Sign in" in r.text or "Connect to Odoo" in r.text

    def test_login_success(self, client, fake_odoo):
        r = client.post(
            "/login",
            data={"login": "darcy@allabout.technology", "api_key": VALID_API_KEY},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/backlog"
        assert fake_odoo.auth_attempts == [("darcy@allabout.technology", VALID_API_KEY)]
        # Encrypted session cookie was issued
        assert "acr_session" in r.cookies

    def test_login_bad_api_key_redirects_with_error(self, client, fake_odoo):
        r = client.post(
            "/login",
            data={"login": "darcy@allabout.technology", "api_key": "wrong"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "error=" in r.headers["location"]
        # No session was issued
        assert "acr_session" not in r.cookies or not r.cookies.get("acr_session")

    def test_login_without_origin_is_csrf_blocked(self, client):
        r = client.post(
            "/login",
            data={"login": "x@y.z", "api_key": VALID_API_KEY},
            follow_redirects=False,
        )
        assert r.status_code == 403

    def test_logout_clears_session(self, authed_client):
        r = authed_client.get("/logout", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"
        # Set-Cookie with Max-Age=0 deletes
        sc = r.headers.get("set-cookie", "")
        assert "Max-Age=0" in sc


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
class TestPages:
    def test_backlog_renders(self, authed_client):
        r = authed_client.get("/backlog?view=tasks")
        assert r.status_code == 200
        assert "Prioritized Backlog" in r.text
        # Issue Type column should render the resolved label, not the key
        assert "Non-critical Workflow Bug" in r.text

    def test_backlog_helpdesk_view(self, authed_client):
        r = authed_client.get("/backlog?view=tickets")
        assert r.status_code == 200
        assert "Prioritized Backlog" in r.text

    def test_backlog_requires_auth(self, client):
        r = client.get("/backlog", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"

    def test_gantt_renders(self, authed_client):
        r = authed_client.get("/gantt")
        assert r.status_code == 200
        assert "Gantt View" in r.text

    def test_config_renders(self, authed_client):
        r = authed_client.get("/config")
        assert r.status_code == 200
        assert "Scoring Configuration" in r.text
        # Both attributes from the fake odoo show up
        assert "Customer" in r.text
        assert "Issue Type" in r.text


# ---------------------------------------------------------------------------
# Task detail
# ---------------------------------------------------------------------------
class TestApiTaskDetail:
    def test_returns_enriched_task(self, authed_client):
        r = authed_client.get("/api/task/3826")
        assert r.status_code == 200
        d = r.json()
        task = d["task"]
        # The row repaint depends on these fields being present
        for k in ["_score", "_age", "_grooming", "_stage", "_customer",
                  "_project", "_project_id", "_issue_type_label",
                  "_assignee", "_assignee_id"]:
            assert k in task, f"missing enriched field: {k}"

    def test_issue_type_label_resolved(self, authed_client):
        r = authed_client.get("/api/task/3826")
        assert r.json()["task"]["_issue_type_label"] == "Non-critical Workflow Bug"

    def test_description_sanitized(self, authed_client, fake_odoo):
        # Inject a script tag into the task description
        fake_odoo.tasks[3826]["description"] = "<p>safe</p><script>alert(1)</script>"
        r = authed_client.get("/api/task/3826")
        desc = r.json()["task"]["description"]
        assert "<script>" not in desc
        assert "alert(1)" not in desc
        assert "<p>safe</p>" in desc

    def test_chatter_messages_sanitized(self, authed_client, fake_odoo):
        fake_odoo.messages.append({
            "id": 99, "res_id": 3826, "model": "project.task",
            "message_type": "comment",
            "body": "<p>hi</p><script>alert('xss')</script>",
            "date": "2026-05-01 10:00:00",
            "author_id": [1297, "Andrew Temple"],
            "subtype_id": [1, "Comment"], "attachment_ids": [],
        })
        r = authed_client.get("/api/task/3826")
        msgs = r.json()["messages"]
        assert any("hi" in m.get("body", "") for m in msgs)
        for m in msgs:
            assert "<script>" not in m.get("body", "")
            assert "alert('xss')" not in m.get("body", "")

    def test_404_for_unknown_task(self, authed_client):
        r = authed_client.get("/api/task/999999")
        assert r.status_code == 404

    def test_401_unauthenticated(self, client):
        r = client.get("/api/task/3826")
        assert r.status_code == 401

    def test_cache_control_no_store(self, authed_client):
        r = authed_client.get("/api/task/3826")
        assert r.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# Task update — the convert_value wiring tests (Emily's "unassign" bug)
# ---------------------------------------------------------------------------
class TestApiTaskUpdate:
    def test_unassign_writes_clear_command(self, authed_client, fake_odoo):
        """The reported bug: picking '—' in Assignee must clear user_ids."""
        r = authed_client.post(
            "/api/task/3826/update",
            json={"user_id": ""},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        # Odoo should have received the m2m clear command
        assert len(fake_odoo.writes) == 1
        model, ids, values = fake_odoo.writes[0]
        assert model == "project.task"
        assert ids == [3826]
        assert values == {"user_ids": [(5, 0, 0)]}
        # In-memory state updated
        assert fake_odoo.tasks[3826]["user_ids"] == []

    def test_assign_writes_set_command(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/update",
            json={"user_id": "1297"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"user_ids": [(6, 0, [1297])]}

    def test_clear_effort_writes_false(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/update",
            json={"x_studio_level_of_effort": ""},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"x_studio_level_of_effort": False}

    def test_clear_deadline_writes_false(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/update",
            json={"date_deadline": ""},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"date_deadline": False}

    def test_set_deadline_writes_date_string(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/update",
            json={"date_deadline": "2026-06-01"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"date_deadline": "2026-06-01"}

    def test_partial_update_only_writes_specified_fields(self, authed_client, fake_odoo):
        """Dirty-tracking on the client sends only the changed fields.
        The server must not over-write other fields."""
        r = authed_client.post(
            "/api/task/3826/update",
            json={"x_studio_road_map_flag": True},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        # Only one field in the write — no spurious "set everything else"
        assert values == {"x_studio_road_map_flag": True}

    def test_ticket_fields_route_to_helpdesk_ticket(self, authed_client, fake_odoo):
        """Ticket-side fields (Issue Type, Customer Funded, etc.) write to
        the linked helpdesk.ticket, not project.task. Tests the dual-write
        dispatch in update_task."""
        r = authed_client.post(
            "/api/task/3826/update",
            json={"x_studio_issue_type": "Enhancement"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        # Two writes happen: one to lookup helpdesk_ticket_id (read), then
        # one to write to helpdesk.ticket — but actually the lookup is a
        # search_read, not a write. So only one write.
        assert len(fake_odoo.writes) == 1
        model, ids, values = fake_odoo.writes[0]
        assert model == "helpdesk.ticket"
        assert ids == [101274]  # the linked ticket
        assert values == {"x_studio_customer_impact": "Enhancement"}

    def test_clearing_issue_type_writes_false_to_ticket(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/update",
            json={"x_studio_issue_type": ""},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        model, ids, values = fake_odoo.writes[0]
        assert model == "helpdesk.ticket"
        assert values == {"x_studio_customer_impact": False}

    def test_task_without_linked_ticket_skips_ticket_writes(self, authed_client, fake_odoo):
        """Task 4997 has no helpdesk_ticket_id — issue_type changes should
        skip silently rather than 500."""
        r = authed_client.post(
            "/api/task/4997/update",
            json={"x_studio_issue_type": "Enhancement"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        # No write happened — ticket fields with no linked ticket are dropped
        assert fake_odoo.writes == []

    def test_mixed_task_and_ticket_fields(self, authed_client, fake_odoo):
        """An edit that touches both a task field and a ticket field
        should produce TWO writes (one per table)."""
        r = authed_client.post(
            "/api/task/3826/update",
            json={
                "name": "Renamed task",
                "x_studio_issue_type": "Enhancement",
            },
            headers=ORIGIN,
        )
        assert r.status_code == 200
        models = sorted(w[0] for w in fake_odoo.writes)
        assert models == ["helpdesk.ticket", "project.task"]

    def test_invalidates_open_tasks_cache(self, authed_client, fake_odoo):
        # Prime the cache
        authed_client.get("/backlog?view=tasks")
        assert "open_tasks" in app._data_cache.get(TEST_UID, {})
        # Update a task
        authed_client.post(
            "/api/task/3826/update",
            json={"x_studio_road_map_flag": True},
            headers=ORIGIN,
        )
        # Cache should be invalidated
        assert "open_tasks" not in app._data_cache.get(TEST_UID, {})

    def test_csrf_blocks_post_without_origin(self, authed_client):
        r = authed_client.post(
            "/api/task/3826/update",
            json={"user_id": ""},
        )
        assert r.status_code == 403

    def test_unauthenticated_returns_401(self, client):
        r = client.post(
            "/api/task/3826/update",
            json={"user_id": ""},
            headers=ORIGIN,
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Gantt date drag-drop
# ---------------------------------------------------------------------------
class TestApiTaskDates:
    def test_drag_writes_dates(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/dates",
            json={"start": "2026-05-15", "end": "2026-05-22"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {
            "planned_date_begin": "2026-05-15",
            "date_end": "2026-05-22",
        }

    def test_drag_with_only_start(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/dates",
            json={"start": "2026-05-15"},
            headers=ORIGIN,
        )
        _, _, values = fake_odoo.writes[0]
        assert values == {"planned_date_begin": "2026-05-15"}

    def test_drag_with_assignee(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/dates",
            json={"start": "2026-05-15", "user_ids": [1297]},
            headers=ORIGIN,
        )
        _, _, values = fake_odoo.writes[0]
        assert values["user_ids"] == [(6, 0, [1297])]


# ---------------------------------------------------------------------------
# Bulk update
# ---------------------------------------------------------------------------
class TestApiBulkUpdate:
    def test_bulk_stage_change(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/tasks/bulk-update",
            json={"task_ids": [3826, 4997], "stage_id": 200},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "updated": 2}
        _, ids, values = fake_odoo.writes[0]
        assert sorted(ids) == [3826, 4997]
        assert values == {"stage_id": 200}

    def test_bulk_without_fields_400(self, authed_client):
        r = authed_client.post(
            "/api/tasks/bulk-update",
            json={"task_ids": [3826]},
            headers=ORIGIN,
        )
        assert r.status_code == 400

    def test_bulk_without_ids_400(self, authed_client):
        r = authed_client.post(
            "/api/tasks/bulk-update",
            json={"stage_id": 200},
            headers=ORIGIN,
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
class TestComments:
    def test_post_task_comment(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/comment",
            json={"body": "Hello world"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        assert fake_odoo.message_posts == [
            ("project.task", 3826, "Hello world")
        ]

    def test_post_ticket_comment(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/ticket/101274/comment",
            json={"body": "Resolved"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        assert fake_odoo.message_posts == [
            ("helpdesk.ticket", 101274, "Resolved")
        ]

    def test_comment_script_tag_is_escaped(self, authed_client, fake_odoo):
        """Stored XSS via comments — user input is HTML-escaped before
        being sent to Odoo."""
        r = authed_client.post(
            "/api/task/3826/comment",
            json={"body": "<script>alert('xss')</script>"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        body = fake_odoo.message_posts[0][2]
        assert "<script>" not in body
        assert "&lt;script&gt;" in body

    def test_comment_newlines_become_br(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/comment",
            json={"body": "line1\nline2"},
            headers=ORIGIN,
        )
        body = fake_odoo.message_posts[0][2]
        assert "line1<br/>line2" == body

    def test_empty_comment_400(self, authed_client):
        r = authed_client.post(
            "/api/task/3826/comment",
            json={"body": "   "},
            headers=ORIGIN,
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Ticket endpoints
# ---------------------------------------------------------------------------
class TestApiTicket:
    def test_ticket_detail(self, authed_client):
        r = authed_client.get("/api/ticket/detail/101274")
        assert r.status_code == 200
        d = r.json()
        assert d["ticket"]["id"] == 101274
        assert d["ticket"]["_score"] is not None
        assert d["ticket"]["description"] == "<p>Real customer issue</p>"

    def test_ticket_detail_sanitizes(self, authed_client, fake_odoo):
        fake_odoo.tickets[101274]["description"] = "<p>ok</p><script>x</script>"
        r = authed_client.get("/api/ticket/detail/101274")
        desc = r.json()["ticket"]["description"]
        assert "<script>" not in desc

    def test_ticket_lookup_by_ref(self, authed_client):
        r = authed_client.get("/api/ticket/lookup/57077")
        assert r.status_code == 200
        d = r.json()
        assert d["ticket"]["id"] == 101274
        # Linked task is found
        assert d["task"]["id"] == 3826

    def test_ticket_lookup_404(self, authed_client):
        r = authed_client.get("/api/ticket/lookup/99999")
        assert r.status_code == 404

    def test_update_ticket_status(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/ticket/101274/update-status",
            json={"stage_id": 200},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, ids, values = fake_odoo.writes[0]
        assert values == {"stage_id": 200}
        assert ids == [101274]

    def test_update_ticket_status_invalidates_caches(self, authed_client):
        """Without this, closing a ticket via the inline picker leaves
        the cached open_tickets list showing it as still open for
        another 15 minutes."""
        # Prime the cache by hitting both views
        authed_client.get("/backlog?view=tasks")
        authed_client.get("/backlog?view=tickets")
        cache = app._data_cache.get(TEST_UID, {})
        assert "open_tickets" in cache
        assert "open_tasks" in cache
        # Status change
        authed_client.post(
            "/api/ticket/101274/update-status",
            json={"stage_id": 200},
            headers=ORIGIN,
        )
        cache = app._data_cache.get(TEST_UID, {})
        assert "open_tickets" not in cache
        assert "open_tasks" not in cache

    def test_update_ticket_status_requires_stage(self, authed_client):
        r = authed_client.post(
            "/api/ticket/101274/update-status",
            json={},
            headers=ORIGIN,
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Options + project + recalculate
# ---------------------------------------------------------------------------
class TestApiOptions:
    def test_options_shape(self, authed_client):
        r = authed_client.get("/api/options")
        assert r.status_code == 200
        d = r.json()
        for key in ["stages", "customers", "users", "issue_types",
                    "effort_levels", "customer_funded", "priorities"]:
            assert key in d, f"missing options bucket: {key}"
        # Stages come from the fake odoo
        assert any(s["name"] == "Inbox" for s in d["stages"])

    def test_options_uses_customer_rank(self, authed_client):
        # Partner with customer_rank > 0 should appear
        r = authed_client.get("/api/options")
        names = [c["name"] for c in r.json()["customers"]]
        assert "Schnuck Markets, Inc." in names


class TestApiProject:
    def test_project_detail(self, authed_client):
        r = authed_client.get("/api/project/75")
        assert r.status_code == 200
        d = r.json()
        assert d["project"]["id"] == 75
        # description sanitized
        assert "<script>" not in d["project"]["description"]
        # tasks list returned
        assert isinstance(d["tasks"], list)


class TestRecalculateScores:
    def test_recalculate(self, authed_client):
        r = authed_client.post(
            "/api/recalculate-scores",
            headers=ORIGIN,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "scores" in d
        assert "thresholds" in d


# ---------------------------------------------------------------------------
# Weight config
# ---------------------------------------------------------------------------
class TestWeightConfig:
    def test_update_weight(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/config/weight/10",
            data={"weight": "7"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/config"
        _, ids, values = fake_odoo.writes[0]
        assert ids == [10]
        assert values == {"x_weight": 7}


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------
class TestPresence:
    def test_heartbeat_records_login(self, authed_client):
        r = authed_client.post("/api/presence/heartbeat", headers=ORIGIN)
        assert r.status_code == 200
        assert "darcy@allabout.technology" in app.active_users

    def test_online_returns_recent_logins(self, authed_client):
        authed_client.post("/api/presence/heartbeat", headers=ORIGIN)
        r = authed_client.get("/api/presence/online")
        assert r.status_code == 200
        assert "darcy@allabout.technology" in r.json()["users"]

    def test_heartbeat_unauthenticated(self, client):
        r = client.post("/api/presence/heartbeat", headers=ORIGIN)
        assert r.json() == {"ok": False}


# ---------------------------------------------------------------------------
# Cache-Control middleware
# ---------------------------------------------------------------------------
class TestCacheControl:
    def test_api_routes_no_store(self, authed_client):
        for path in [
            "/api/task/3826",
            "/api/ticket/detail/101274",
            "/api/project/75",
            "/api/options",
            "/api/presence/online",
        ]:
            r = authed_client.get(path)
            assert r.headers.get("cache-control") == "no-store", (
                f"{path} missing Cache-Control: no-store"
            )

    def test_html_pages_not_no_store(self, authed_client):
        r = authed_client.get("/backlog?view=tasks")
        # HTML pages don't get the no-store header (only /api/* does)
        assert r.headers.get("cache-control") != "no-store"
