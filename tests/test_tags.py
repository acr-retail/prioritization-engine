"""Tests for the tag feature: convert_value many2many_replace,
resolve_tag_names, /api/options.tags bucket, and the tag write path
through /api/task/{id}/update."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app
from conftest import VALID_API_KEY, TEST_UID


ORIGIN = {"Origin": "http://testserver"}


# ---------------------------------------------------------------------------
# convert_value: many2many_replace
# ---------------------------------------------------------------------------
class TestConvertValueManyToManyReplace:
    def test_csv_string_to_replace_command(self):
        assert app.convert_value("1,3,7", "many2many_replace") == [(6, 0, [1, 3, 7])]

    def test_single_id_csv(self):
        assert app.convert_value("2", "many2many_replace") == [(6, 0, [2])]

    def test_empty_clears_all(self):
        # Picking nothing or removing every chip → clear command
        assert app.convert_value("", "many2many_replace") == [(5, 0, 0)]
        assert app.convert_value(None, "many2many_replace") == [(5, 0, 0)]

    def test_list_input_accepted(self):
        # Some callers may pass a real list rather than CSV
        assert app.convert_value([1, 3], "many2many_replace") == [(6, 0, [1, 3])]

    def test_list_with_string_ids(self):
        assert app.convert_value(["1", "3"], "many2many_replace") == [(6, 0, [1, 3])]

    def test_whitespace_around_csv_tolerated(self):
        assert app.convert_value("1, 3, 7", "many2many_replace") == [(6, 0, [1, 3, 7])]

    def test_list_with_empty_strings_filtered(self):
        assert app.convert_value(["1", "", "3"], "many2many_replace") == [(6, 0, [1, 3])]


# ---------------------------------------------------------------------------
# resolve_tag_names
# ---------------------------------------------------------------------------
class TestResolveTagNames:
    def setup_method(self):
        app._data_cache.clear()

    def test_attaches_tags_to_each_task(self, monkeypatch):
        def fake(uid, key, model, domain, fields, limit=0):
            assert model == "project.tags"
            return [
                {"id": 1, "name": "Bug"},
                {"id": 2, "name": "Enhancement"},
            ]

        monkeypatch.setattr(app, "odoo_search_read", fake)
        tasks = [
            {"id": 10, "tag_ids": [1]},
            {"id": 11, "tag_ids": [1, 2]},
            {"id": 12, "tag_ids": []},
        ]
        app.resolve_tag_names(tasks, 1, "k")
        assert tasks[0]["_tags"] == [{"id": 1, "name": "Bug"}]
        assert tasks[1]["_tags"] == [
            {"id": 1, "name": "Bug"},
            {"id": 2, "name": "Enhancement"},
        ]
        assert tasks[2]["_tags"] == []

    def test_batches_single_query(self, monkeypatch):
        """20 tasks sharing 2 tags must produce ONE project.tags query."""
        calls = []

        def fake(uid, key, model, domain, fields, limit=0):
            calls.append(model)
            return [{"id": 1, "name": "Bug"}, {"id": 2, "name": "Enhancement"}]

        monkeypatch.setattr(app, "odoo_search_read", fake)
        tasks = [{"id": i, "tag_ids": [1 if i % 2 == 0 else 2]} for i in range(20)]
        app.resolve_tag_names(tasks, 1, "k")
        assert calls.count("project.tags") == 1

    def test_uses_cache_on_second_call(self, monkeypatch):
        calls = []

        def fake(uid, key, model, domain, fields, limit=0):
            calls.append(model)
            return [{"id": 1, "name": "Bug"}]

        monkeypatch.setattr(app, "odoo_search_read", fake)
        app.resolve_tag_names([{"id": 1, "tag_ids": [1]}], 1, "k")
        app.resolve_tag_names([{"id": 2, "tag_ids": [1]}], 1, "k")
        # Only the first call hit Odoo
        assert calls.count("project.tags") == 1

    def test_unknown_tag_id_falls_back_to_id_as_name(self, monkeypatch):
        monkeypatch.setattr(app, "odoo_search_read",
                            lambda *a, **kw: [])  # tag not found
        tasks = [{"id": 1, "tag_ids": [999]}]
        app.resolve_tag_names(tasks, 1, "k")
        assert tasks[0]["_tags"] == [{"id": 999, "name": "999"}]


# ---------------------------------------------------------------------------
# Route integration: /api/options exposes tags
# ---------------------------------------------------------------------------
class TestApiOptionsTags:
    def test_options_includes_tags(self, authed_client):
        r = authed_client.get("/api/options")
        d = r.json()
        assert "tags" in d
        names = [t["name"] for t in d["tags"]]
        assert "Bug" in names
        assert "Enhancement" in names

    def test_tag_objects_have_id_and_name(self, authed_client):
        r = authed_client.get("/api/options")
        for t in r.json()["tags"]:
            assert "id" in t and "name" in t


# ---------------------------------------------------------------------------
# Route integration: writing tag_ids through /api/task/{id}/update
# ---------------------------------------------------------------------------
class TestApiTaskUpdateTags:
    def test_set_tags_writes_replace_command(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/update",
            json={"tag_ids": "1,2"},  # Bug + Enhancement
            headers=ORIGIN,
        )
        assert r.status_code == 200
        model, ids, values = fake_odoo.writes[0]
        assert model == "project.task"
        assert ids == [3826]
        assert values == {"tag_ids": [(6, 0, [1, 2])]}

    def test_clear_tags_writes_clear_command(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/3826/update",
            json={"tag_ids": ""},
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"tag_ids": [(5, 0, 0)]}

    def test_set_single_tag(self, authed_client, fake_odoo):
        r = authed_client.post(
            "/api/task/4997/update",
            json={"tag_ids": "4"},  # Roadmap
            headers=ORIGIN,
        )
        assert r.status_code == 200
        _, _, values = fake_odoo.writes[0]
        assert values == {"tag_ids": [(6, 0, [4])]}


# ---------------------------------------------------------------------------
# Route integration: /api/task/{id} GET returns _tags
# ---------------------------------------------------------------------------
class TestApiTaskDetailTags:
    def test_response_includes_resolved_tags(self, authed_client):
        r = authed_client.get("/api/task/3826")
        task = r.json()["task"]
        assert "_tags" in task
        # Task 3826 in conftest has tag_ids=[1]
        assert task["_tags"] == [{"id": 1, "name": "Bug"}]

    def test_untagged_task_has_empty_tags_list(self, authed_client):
        r = authed_client.get("/api/task/4997")
        task = r.json()["task"]
        assert task["_tags"] == []


# ---------------------------------------------------------------------------
# Backlog page renders the tags column
# ---------------------------------------------------------------------------
class TestBacklogTagsRender:
    def test_tag_column_rendered_in_backlog(self, authed_client):
        r = authed_client.get("/backlog?view=tasks")
        # The Tags column header
        assert ">Tags</th>" in r.text
        # The chip CSS class applied to actual tags
        assert "tag-chip" in r.text
        # The "Bug" tag from task 3826 should appear as a chip
        assert "Bug" in r.text
        # data-tags attribute used by the filter for substring matching
        assert 'data-tags="Bug"' in r.text
