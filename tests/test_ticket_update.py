"""Tests for the editable helpdesk-ticket flow:
  • /api/options exposes helpdesk_tags bucket
  • api_ticket_detail returns shape-parity with api_task_detail
    (so the frontend updateBacklogRow doesn't need to branch)
  • POST /api/ticket/{id}/update writes the right Odoo commands
  • Both task and ticket caches are busted on a ticket write
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
# /api/options.helpdesk_tags
# ---------------------------------------------------------------------------
class TestApiOptionsHelpdeskTags:
    def test_bucket_present(self, authed_client):
        r = authed_client.get("/api/options")
        d = r.json()
        assert "helpdesk_tags" in d
        names = [t["name"] for t in d["helpdesk_tags"]]
        assert "Customer Reported" in names
        assert "Internal Triage" in names

    def test_distinct_from_project_tags(self, authed_client):
        """The two buckets must be separate — same name in both should be
        possible without collision, and IDs should not be conflated."""
        r = authed_client.get("/api/options")
        d = r.json()
        project_ids = {t["id"] for t in d["tags"]}
        helpdesk_ids = {t["id"] for t in d["helpdesk_tags"]}
        # The fixture uses 1-4 for project tags and 50-51 for helpdesk
        assert project_ids.isdisjoint(helpdesk_ids)


# ---------------------------------------------------------------------------
# api_ticket_detail shape parity with api_task_detail
# ---------------------------------------------------------------------------
class TestApiTicketDetailShape:
    def test_response_carries_enrich_tickets_fields(self, authed_client):
        r = authed_client.get("/api/ticket/detail/101274")
        assert r.status_code == 200
        t = r.json()["ticket"]
        # These are what updateBacklogRow / panel reads — must be present
        # on tickets the same way they are on tasks.
        for key in ["_score", "_age", "_grooming", "_stage",
                    "_customer", "_assignee", "_assignee_id",
                    "_issue_type_label", "_tags"]:
            assert key in t, f"ticket response missing {key}"

    def test_tags_resolved_from_helpdesk_namespace(self, authed_client):
        """Ticket 101274 in the fixture has tag_ids=[50] (Customer Reported).
        The response should resolve it via helpdesk.tag, NOT project.tags."""
        r = authed_client.get("/api/ticket/detail/101274")
        t = r.json()["ticket"]
        assert t["_tags"] == [{"id": 50, "name": "Customer Reported"}]


# ---------------------------------------------------------------------------
# POST /api/ticket/{id}/update — the editable panel save target
# ---------------------------------------------------------------------------
class TestApiTicketUpdate:
    def test_update_name(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/ticket/101274/update",
            json={"name": "Renamed by tests"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        model, ids, values = fake_odoo.writes[0]
        assert model == "helpdesk.ticket"
        assert ids == [101274]
        assert values == {"name": "Renamed by tests"}

    def test_update_stage(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/ticket/101274/update",
            json={"stage_id": "200"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"stage_id": 200}

    def test_update_assignee_single_user(self, authed_client, fake_odoo):
        """Ticket user_id is many2one (single), NOT many2many like task.
        The write shape should be a plain int, not [(6, 0, [...])]."""
        r = authed_client.post(
            "/api/ticket/101274/update",
            json={"user_id": "1297"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"user_id": 1297}

    def test_unassign_ticket(self, authed_client, fake_odoo):
        """Picking '—' for assignee on a ticket should clear user_id to
        False (not [(5, 0, 0)] which is the many2many clear shape)."""
        r = authed_client.post(
            "/api/ticket/101274/update",
            json={"user_id": ""},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"user_id": False}

    def test_update_issue_type_routes_to_customer_impact(self, authed_client, fake_odoo):
        """The frontend uses x_studio_issue_type as the field name (same
        as on tasks). Server routes it to x_studio_customer_impact on
        helpdesk.ticket."""
        r = authed_client.post(
            "/api/ticket/101274/update",
            json={"x_studio_issue_type": "Major"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"x_studio_customer_impact": "Major"}

    def test_update_tag_ids_uses_helpdesk_namespace(self, authed_client, fake_odoo):
        """Tag IDs in this write are helpdesk.tag IDs (50, 51), not
        project.tags. The write is generic (just an m2m replace) — Odoo
        figures out which model by the field's relation."""
        r = authed_client.post(
            "/api/ticket/101274/update",
            json={"tag_ids": "50,51"},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"tag_ids": [(6, 0, [50, 51])]}

    def test_clear_tags(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/ticket/101274/update",
            json={"tag_ids": ""},
            headers=ORIGIN,
        )
        _, _, values = fake_odoo.writes[0]
        assert values == {"tag_ids": [(5, 0, 0)]}

    def test_partial_update_only_changed_fields(self, authed_client, fake_odoo):
        """Dirty-tracking sends just the fields that changed. Server must
        write only those, not over-write the rest."""
        r = authed_client.post(
            "/api/ticket/101274/update",
            json={"x_studio_related_field_5vi_1jnfmj9cf": True},
            headers=ORIGIN,
        )
        _, _, values = fake_odoo.writes[0]
        assert values == {"x_studio_escalated": True}

    def test_no_changes_is_a_noop(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/ticket/101274/update",
            json={},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "updated": 0}
        assert fake_odoo.writes == []

    def test_invalidates_both_task_and_ticket_caches(self, authed_client):
        """A ticket write flows back to the linked project.task through
        Studio related fields, so both lists must be invalidated."""
        authed_client.get("/backlog?view=tasks")
        authed_client.get("/backlog?view=tickets")
        cache = app._data_cache.get(TEST_UID, {})
        assert "open_tasks" in cache
        assert "open_tickets" in cache
        authed_client.post(
            "/api/ticket/101274/update",
            json={"x_studio_related_field_5vi_1jnfmj9cf": True},
            headers=ORIGIN,
        )
        cache = app._data_cache.get(TEST_UID, {})
        assert "open_tasks" not in cache
        assert "open_tickets" not in cache

    def test_csrf_blocks_post_without_origin(self, authed_client):
        r = authed_client.post(
            "/api/ticket/101274/update",
            json={"name": "x"},
        )
        assert r.status_code == 403

    def test_unauthenticated_returns_401(self, client):
        r = client.post(
            "/api/ticket/101274/update",
            json={"name": "x"},
            headers=ORIGIN,
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Parameterized resolve_tag_names — task vs ticket flow
# ---------------------------------------------------------------------------
class TestResolveTagNamesBothModels:
    def setup_method(self):
        app._data_cache.clear()

    def test_default_uses_project_tags(self, monkeypatch):
        captured = []

        def fake(uid, key, model, domain, fields, limit=0):
            captured.append(model)
            return [{"id": 1, "name": "Bug"}]

        monkeypatch.setattr(app, "odoo_search_read", fake)
        tasks = [{"id": 1, "tag_ids": [1]}]
        app.resolve_tag_names(tasks, 1, "k")
        assert captured == ["project.tags"]
        assert tasks[0]["_tags"] == [{"id": 1, "name": "Bug"}]

    def test_explicit_helpdesk_tag(self, monkeypatch):
        captured = []

        def fake(uid, key, model, domain, fields, limit=0):
            captured.append(model)
            return [{"id": 50, "name": "Customer Reported"}]

        monkeypatch.setattr(app, "odoo_search_read", fake)
        tickets = [{"id": 1, "tag_ids": [50]}]
        app.resolve_tag_names(
            tickets, 1, "k",
            tag_model="helpdesk.tag",
            cache_key="helpdesk_tag_names",
        )
        assert captured == ["helpdesk.tag"]
        assert tickets[0]["_tags"] == [{"id": 50, "name": "Customer Reported"}]

    def test_separate_caches_per_model(self, monkeypatch):
        """The cache_key parameter must namespace the two tag catalogs.
        Otherwise calling task flow then ticket flow would re-use the
        wrong catalog and tag names would come back as raw IDs."""
        call_count = {"project": 0, "helpdesk": 0}

        def fake(uid, key, model, domain, fields, limit=0):
            if model == "project.tags":
                call_count["project"] += 1
                return [{"id": 1, "name": "Bug"}]
            if model == "helpdesk.tag":
                call_count["helpdesk"] += 1
                return [{"id": 50, "name": "Customer Reported"}]
            return []

        monkeypatch.setattr(app, "odoo_search_read", fake)
        app.resolve_tag_names([{"id": 1, "tag_ids": [1]}], 1, "k")
        app.resolve_tag_names(
            [{"id": 2, "tag_ids": [50]}], 1, "k",
            tag_model="helpdesk.tag", cache_key="helpdesk_tag_names",
        )
        # Each model queried exactly once — and crucially, the helpdesk
        # call did NOT skip due to the project.tags cache existing
        assert call_count == {"project": 1, "helpdesk": 1}
