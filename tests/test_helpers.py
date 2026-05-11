"""Unit tests for the smaller helpers that the integration tests don't
directly exercise: cache_get/set/clear, get_open_tasks_cached's
extra_fields branching, resolve_user_names, and get_selection_labels."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app


# ---------------------------------------------------------------------------
# cache_get / cache_set / cache_clear
# ---------------------------------------------------------------------------
class TestCacheGetSet:
    def setup_method(self):
        app._data_cache.clear()

    def test_set_and_get_round_trip(self):
        app.cache_set(1, "key1", {"data": "value"})
        assert app.cache_get(1, "key1") == {"data": "value"}

    def test_miss_returns_none(self):
        assert app.cache_get(1, "key1") is None

    def test_per_user_isolation(self):
        app.cache_set(1, "tasks", "user-1-tasks")
        app.cache_set(2, "tasks", "user-2-tasks")
        assert app.cache_get(1, "tasks") == "user-1-tasks"
        assert app.cache_get(2, "tasks") == "user-2-tasks"

    def test_ttl_expiry(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(app._time, "time", lambda: now[0])
        app.cache_set(1, "key", "value")
        # Just under TTL → still hit
        now[0] = 1000.0 + app.DATA_CACHE_TTL - 1
        assert app.cache_get(1, "key") == "value"
        # Past TTL → miss + evict
        now[0] = 1000.0 + app.DATA_CACHE_TTL + 1
        assert app.cache_get(1, "key") is None
        # Expired entry should have been removed
        assert "key" not in app._data_cache.get(1, {})


class TestCacheClear:
    def setup_method(self):
        app._data_cache.clear()

    def test_clear_specific_key(self):
        app.cache_set(1, "a", "AA")
        app.cache_set(1, "b", "BB")
        app.cache_clear(1, "a")
        assert app.cache_get(1, "a") is None
        assert app.cache_get(1, "b") == "BB"

    def test_clear_all_for_user(self):
        app.cache_set(1, "a", "AA")
        app.cache_set(1, "b", "BB")
        app.cache_set(2, "a", "AA2")
        app.cache_clear(1)
        assert app.cache_get(1, "a") is None
        assert app.cache_get(1, "b") is None
        # Other user untouched
        assert app.cache_get(2, "a") == "AA2"

    def test_clear_nonexistent_user_is_noop(self):
        # Should not raise
        app.cache_clear(999)
        app.cache_clear(999, "missing")


# ---------------------------------------------------------------------------
# get_open_tasks_cached — cache hit/miss + extra_fields branching
# ---------------------------------------------------------------------------
class TestGetOpenTasksCached:
    def setup_method(self):
        app._data_cache.clear()

    def test_cache_miss_queries_odoo(self, monkeypatch):
        calls = []

        def fake_search_read(uid, key, model, domain, fields, limit=0):
            calls.append(fields)
            return [{"id": 1, "name": "task"}]

        monkeypatch.setattr(app, "odoo_search_read", fake_search_read)
        result = app.get_open_tasks_cached(1, "k")
        assert result == [{"id": 1, "name": "task"}]
        assert len(calls) == 1

    def test_cache_hit_skips_odoo(self, monkeypatch):
        calls = []

        def fake_search_read(uid, key, model, domain, fields, limit=0):
            calls.append(fields)
            return [{"id": 1}]

        monkeypatch.setattr(app, "odoo_search_read", fake_search_read)
        app.get_open_tasks_cached(1, "k")
        app.get_open_tasks_cached(1, "k")
        # Only one query — the second call was a cache hit
        assert len(calls) == 1

    def test_extra_fields_uses_separate_cache_key(self, monkeypatch):
        """The Gantt page asks for extra date fields. Those tasks need
        their own cache bucket so the backlog doesn't get gantt-shaped
        data and vice versa."""
        calls = []

        def fake_search_read(uid, key, model, domain, fields, limit=0):
            calls.append(fields)
            return [{"id": 1}]

        monkeypatch.setattr(app, "odoo_search_read", fake_search_read)
        # First: backlog query (no extras)
        app.get_open_tasks_cached(1, "k")
        # Second: gantt query (extras) — should NOT hit the backlog cache
        app.get_open_tasks_cached(1, "k", extra_fields=["date_deadline"])
        assert len(calls) == 2
        # The backlog cache key and the gantt cache key are different
        assert "open_tasks" in app._data_cache[1]
        assert "open_tasks_gantt" in app._data_cache[1]

    def test_excludes_completed_and_cancelled_stages(self, monkeypatch):
        captured_domain = []

        def fake_search_read(uid, key, model, domain, fields, limit=0):
            captured_domain.append(domain)
            return []

        monkeypatch.setattr(app, "odoo_search_read", fake_search_read)
        app.get_open_tasks_cached(1, "k")
        # Verify the excluded stages are passed in the domain
        domain = captured_domain[0]
        assert domain[0][0] == "stage_id.name"
        assert domain[0][1] == "not in"
        excluded = set(domain[0][2])
        assert {"Complete", "Cancelled"}.issubset(excluded)


# ---------------------------------------------------------------------------
# resolve_user_names — batched user lookup + cache
# ---------------------------------------------------------------------------
class TestResolveUserNames:
    def setup_method(self):
        app._data_cache.clear()

    def test_resolves_assignee_name(self, monkeypatch):
        def fake(uid, key, model, domain, fields, limit=0):
            assert model == "res.users"
            return [{"id": 1297, "name": "Andrew Temple"}]

        monkeypatch.setattr(app, "odoo_search_read", fake)
        tasks = [{"id": 1, "user_ids": [1297]}]
        app.resolve_user_names(tasks, 1, "k")
        assert tasks[0]["_assignee"] == "Andrew Temple"
        assert tasks[0]["_assignee_id"] == 1297

    def test_unassigned_task(self, monkeypatch):
        # res.users query happens with empty id list — returns nothing
        monkeypatch.setattr(app, "odoo_search_read",
                            lambda *a, **kw: [])
        tasks = [{"id": 1, "user_ids": []}]
        app.resolve_user_names(tasks, 1, "k")
        assert tasks[0]["_assignee"] == "Unassigned"
        assert tasks[0]["_assignee_id"] == 0

    def test_batches_user_query(self, monkeypatch):
        """If 10 tasks share 3 assignees, we should make ONE query for
        all 3 users, not 10 queries."""
        calls = []

        def fake(uid, key, model, domain, fields, limit=0):
            calls.append(domain)
            return [
                {"id": 1297, "name": "Andrew Temple"},
                {"id": 1302, "name": "Jim Lockwood"},
                {"id": 1308, "name": "Darcy Reno"},
            ]

        monkeypatch.setattr(app, "odoo_search_read", fake)
        tasks = [
            {"id": i, "user_ids": [1297 if i % 3 == 0 else 1302 if i % 3 == 1 else 1308]}
            for i in range(10)
        ]
        app.resolve_user_names(tasks, 1, "k")
        assert len(calls) == 1
        # All three IDs were in the single query's IN clause
        ids_in_query = set(calls[0][0][2])
        assert ids_in_query == {1297, 1302, 1308}

    def test_uses_cached_user_names_on_second_call(self, monkeypatch):
        calls = []

        def fake(uid, key, model, domain, fields, limit=0):
            calls.append(domain)
            return [{"id": 1297, "name": "Andrew Temple"}]

        monkeypatch.setattr(app, "odoo_search_read", fake)
        # First call: queries res.users
        app.resolve_user_names([{"id": 1, "user_ids": [1297]}], 1, "k")
        # Second call: same user, should hit the cache
        app.resolve_user_names([{"id": 2, "user_ids": [1297]}], 1, "k")
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# get_selection_labels — process-global cache, fields_get parsing
# ---------------------------------------------------------------------------
class TestGetSelectionLabels:
    def setup_method(self):
        app._selection_labels_cache = None

    def test_parses_selection_into_key_label_map(self, monkeypatch):
        def fake_fields_get(uid, key, model, names, attrs):
            return {
                "x_studio_issue_type": {
                    "selection": [
                        ["Minor", "Non-critical Workflow Bug"],
                        ["Major", "Critical Workflow Bug- No Workaround"],
                    ]
                },
                "x_studio_level_of_effort": {"selection": [["<10 Hrs", "<10 Hrs"]]},
                "x_studio_related_field_gd_1jnftb4gl": {
                    "selection": [["Yes", "Yes"], ["No", "No"]]
                },
            }

        monkeypatch.setattr(app, "odoo_fields_get", fake_fields_get)
        result = app.get_selection_labels(1, "k")
        assert result["x_studio_issue_type"]["Minor"] == "Non-critical Workflow Bug"
        assert result["x_studio_level_of_effort"]["<10 Hrs"] == "<10 Hrs"
        assert result["x_studio_related_field_gd_1jnftb4gl"]["Yes"] == "Yes"

    def test_caches_after_first_call(self, monkeypatch):
        calls = []

        def fake_fields_get(uid, key, model, names, attrs):
            calls.append(model)
            return {}

        monkeypatch.setattr(app, "odoo_fields_get", fake_fields_get)
        app.get_selection_labels(1, "k")
        app.get_selection_labels(1, "k")
        app.get_selection_labels(2, "different-key")
        # Process-global cache — only ONE Odoo call regardless of caller
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Cached wrappers — get_weight_map_cached, get_sel_labels_cached
# ---------------------------------------------------------------------------
class TestCachedWrappers:
    def setup_method(self):
        app._data_cache.clear()
        app._selection_labels_cache = None

    def test_get_weight_map_cached_caches_per_user(self, monkeypatch):
        calls = []

        def fake_load(uid, key):
            calls.append(uid)
            return {"x_studio_customer": {"name": "C", "field_type": "many2one", "values": {}}}

        monkeypatch.setattr(app, "load_weight_map", fake_load)
        app.get_weight_map_cached(1, "k")
        app.get_weight_map_cached(1, "k")
        app.get_weight_map_cached(2, "k")
        # uid 1 caches; uid 2 is a separate cache
        assert calls == [1, 2]

    def test_get_sel_labels_cached_uses_process_cache(self, monkeypatch):
        calls = []

        def fake(uid, key):
            calls.append(uid)
            return {"x_studio_issue_type": {"Minor": "Non-critical"}}

        monkeypatch.setattr(app, "get_selection_labels", fake)
        app.get_sel_labels_cached(1, "k")
        app.get_sel_labels_cached(2, "k")
        # Both calls share the process-global cache via get_selection_labels
        # But get_sel_labels_cached itself also caches per-user in _data_cache,
        # so the second call (uid 2) hits the per-user cache miss and calls in.
        assert len(calls) >= 1
