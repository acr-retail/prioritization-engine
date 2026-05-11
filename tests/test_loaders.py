"""Tests for load_weight_map / load_attributes_for_config — verifies the
N+1 → 2-query collapse and the bucketed assembly logic.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as a


# Fake Odoo data — 3 attributes with weights, mimicking what search_read returns
_FAKE_ATTRS = [
    {"id": 1, "x_name": "Customer", "x_task_field": "x_studio_customer",
     "x_field_type": "many2one", "x_sequence": 1},
    {"id": 2, "x_name": "Issue Type", "x_task_field": "x_studio_issue_type",
     "x_field_type": "selection", "x_sequence": 3},
    {"id": 3, "x_name": "Roadmap", "x_task_field": "x_studio_road_map_flag",
     "x_field_type": "boolean", "x_sequence": 6},
]
_FAKE_WEIGHTS = [
    # Customer (attr 1)
    {"id": 10, "x_value": "Schnucks", "x_weight": 1, "x_description": "",
     "x_attribute_id": [1, "Customer"]},
    {"id": 11, "x_value": "Rouses", "x_weight": 3, "x_description": "",
     "x_attribute_id": [1, "Customer"]},
    # Issue Type (attr 2)
    {"id": 20, "x_value": "Non-critical Workflow Bug", "x_weight": 4,
     "x_description": "", "x_attribute_id": [2, "Issue Type"]},
    {"id": 21, "x_value": "System stopping bug - No workaround", "x_weight": -15,
     "x_description": "", "x_attribute_id": [2, "Issue Type"]},
    # Roadmap (attr 3)
    {"id": 30, "x_value": "True", "x_weight": 1, "x_description": "",
     "x_attribute_id": [3, "Roadmap"]},
    {"id": 31, "x_value": "False", "x_weight": 5, "x_description": "",
     "x_attribute_id": [3, "Roadmap"]},
]


@pytest.fixture
def mock_odoo(monkeypatch):
    """Replaces odoo_search_read with a recorder that returns canned data."""
    calls = []

    def fake_search_read(uid, api_key, model, domain, fields, limit=0):
        calls.append({"model": model, "domain": domain, "fields": fields})
        if model == a.ATTR_MODEL:
            return [dict(r) for r in _FAKE_ATTRS]
        if model == a.WEIGHT_MODEL:
            # Filter by the domain so the test's expectation that all weights
            # come back in one shot is honest. Real Odoo would apply the IN clause.
            if domain and domain[0][0] == "x_attribute_id" and domain[0][1] == "in":
                wanted = set(domain[0][2])
                return [dict(w) for w in _FAKE_WEIGHTS if w["x_attribute_id"][0] in wanted]
            if domain and domain[0][0] == "x_attribute_id" and domain[0][1] == "=":
                want = domain[0][2]
                return [dict(w) for w in _FAKE_WEIGHTS if w["x_attribute_id"][0] == want]
            return [dict(w) for w in _FAKE_WEIGHTS]
        return []

    monkeypatch.setattr(a, "odoo_search_read", fake_search_read)
    return calls


# ---------------------------------------------------------------------------
# load_weight_map
# ---------------------------------------------------------------------------
class TestLoadWeightMap:
    def test_uses_only_two_queries(self, mock_odoo):
        a.load_weight_map(1, "k")
        assert len(mock_odoo) == 2
        assert mock_odoo[0]["model"] == a.ATTR_MODEL
        assert mock_odoo[1]["model"] == a.WEIGHT_MODEL

    def test_second_query_uses_in_clause(self, mock_odoo):
        a.load_weight_map(1, "k")
        weight_query = mock_odoo[1]
        # Domain should be [("x_attribute_id", "in", [1, 2, 3])]
        assert weight_query["domain"][0][1] == "in"
        assert set(weight_query["domain"][0][2]) == {1, 2, 3}

    def test_buckets_weights_by_attribute(self, mock_odoo):
        result = a.load_weight_map(1, "k")
        assert "x_studio_customer" in result
        assert result["x_studio_customer"]["values"] == {"Schnucks": 1, "Rouses": 3}
        assert result["x_studio_issue_type"]["values"] == {
            "Non-critical Workflow Bug": 4,
            "System stopping bug - No workaround": -15,
        }
        assert result["x_studio_road_map_flag"]["values"] == {"True": 1, "False": 5}

    def test_preserves_attribute_metadata(self, mock_odoo):
        result = a.load_weight_map(1, "k")
        assert result["x_studio_issue_type"]["name"] == "Issue Type"
        assert result["x_studio_issue_type"]["field_type"] == "selection"

    def test_empty_attrs_skips_weight_query(self, monkeypatch):
        calls = []

        def empty_search(*args, **kwargs):
            calls.append(args[2])  # model name
            return []

        monkeypatch.setattr(a, "odoo_search_read", empty_search)
        result = a.load_weight_map(1, "k")
        assert result == {}
        # Should not have made a weight query when attrs returned empty
        assert calls == [a.ATTR_MODEL]


# ---------------------------------------------------------------------------
# load_attributes_for_config
# ---------------------------------------------------------------------------
class TestLoadAttributesForConfig:
    def test_uses_only_two_queries(self, mock_odoo):
        a.load_attributes_for_config(1, "k")
        assert len(mock_odoo) == 2

    def test_attributes_sorted_by_sequence(self, mock_odoo):
        result = a.load_attributes_for_config(1, "k")
        seqs = [r["attr"]["task_field"] for r in result]
        # _FAKE_ATTRS sequences are 1, 3, 6 — already in order
        assert seqs == [
            "x_studio_customer",
            "x_studio_issue_type",
            "x_studio_road_map_flag",
        ]

    def test_weights_sorted_by_weight_ascending(self, mock_odoo):
        result = a.load_attributes_for_config(1, "k")
        # Issue Type: -15, then 4
        weights = [w["weight"] for w in result[1]["weights"]]
        assert weights == [-15, 4]
        # Roadmap: 1, then 5
        weights = [w["weight"] for w in result[2]["weights"]]
        assert weights == [1, 5]

    def test_each_attribute_gets_its_own_weights(self, mock_odoo):
        result = a.load_attributes_for_config(1, "k")
        # Customer attribute should only have Schnucks + Rouses, not Issue Type weights
        cust_values = {w["value"] for w in result[0]["weights"]}
        assert cust_values == {"Schnucks", "Rouses"}

    def test_handles_attribute_id_as_bare_int(self, monkeypatch):
        """Defensive: if Odoo ever returns x_attribute_id as a bare int
        rather than [id, name], the bucket lookup still works."""
        attrs = [{"id": 1, "x_name": "X", "x_task_field": "f",
                  "x_field_type": "selection", "x_sequence": 1}]
        weights = [{"id": 5, "x_value": "v", "x_weight": 2, "x_description": "",
                    "x_attribute_id": 1}]  # bare int, not [id, name]

        def search(uid, key, model, dom, fields, limit=0):
            return attrs if model == a.ATTR_MODEL else weights

        monkeypatch.setattr(a, "odoo_search_read", search)
        result = a.load_attributes_for_config(1, "k")
        assert result[0]["weights"][0]["value"] == "v"
