"""Tests for the pure scoring/display functions.

These never touch Odoo. They lock in the behavior we care about so the
upcoming refactors (N+1 collapse, async wrap, dirty-tracking) can be
verified by re-running the suite.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# Make app.py importable from the tests/ subdirectory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a


# ---------------------------------------------------------------------------
# compute_age_bracket
# ---------------------------------------------------------------------------
class TestComputeAgeBracket:
    def test_empty_input_returns_empty(self):
        assert a.compute_age_bracket("") == ""
        assert a.compute_age_bracket(None) == ""

    def test_today_is_under_30(self):
        assert a.compute_age_bracket(date.today().isoformat()) == "<30"

    def test_29_days_old_is_under_30(self):
        d = (date.today() - timedelta(days=29)).isoformat()
        assert a.compute_age_bracket(d) == "<30"

    def test_30_days_old_is_30_60(self):
        d = (date.today() - timedelta(days=30)).isoformat()
        assert a.compute_age_bracket(d) == "30-60"

    def test_60_days_old_is_30_60(self):
        d = (date.today() - timedelta(days=60)).isoformat()
        assert a.compute_age_bracket(d) == "30-60"

    def test_61_days_old_is_60_90(self):
        d = (date.today() - timedelta(days=61)).isoformat()
        assert a.compute_age_bracket(d) == "60-90"

    def test_91_days_old_is_over_90(self):
        d = (date.today() - timedelta(days=91)).isoformat()
        assert a.compute_age_bracket(d) == ">90"

    def test_accepts_iso_with_time_suffix(self):
        # Odoo returns "2026-04-10 14:30:00" style timestamps
        d = (date.today() - timedelta(days=5)).isoformat() + " 14:30:00"
        assert a.compute_age_bracket(d) == "<30"


# ---------------------------------------------------------------------------
# compute_grooming
# ---------------------------------------------------------------------------
class TestComputeGrooming:
    def test_fully_groomed(self):
        task = {
            "x_studio_issue_type": "Minor",
            "x_studio_level_of_effort": "10-40 Hrs",
            "x_studio_customer": [9, "Schnucks"],
        }
        r = a.compute_grooming(task)
        assert r["groomed"] is True
        assert r["missing"] == []
        assert r["missing_count"] == 0

    def test_missing_one(self):
        task = {
            "x_studio_issue_type": "Minor",
            "x_studio_level_of_effort": False,
            "x_studio_customer": [9, "Schnucks"],
        }
        r = a.compute_grooming(task)
        assert r["groomed"] is False
        assert r["missing"] == ["Level of Effort"]
        assert r["missing_count"] == 1

    def test_missing_all_three(self):
        r = a.compute_grooming({})
        assert r["groomed"] is False
        assert r["missing_count"] == 3
        assert set(r["missing"]) == {"Issue Type", "Level of Effort", "Customer"}

    def test_treats_empty_string_as_missing(self):
        task = {
            "x_studio_issue_type": "",
            "x_studio_level_of_effort": "10-40 Hrs",
            "x_studio_customer": [9, "Schnucks"],
        }
        r = a.compute_grooming(task)
        assert "Issue Type" in r["missing"]

    def test_treats_none_as_missing(self):
        task = {
            "x_studio_issue_type": None,
            "x_studio_level_of_effort": "10-40 Hrs",
            "x_studio_customer": [9, "Schnucks"],
        }
        r = a.compute_grooming(task)
        assert "Issue Type" in r["missing"]


# ---------------------------------------------------------------------------
# compute_score_thresholds
# ---------------------------------------------------------------------------
class TestComputeScoreThresholds:
    def test_empty_returns_zeros(self):
        assert a.compute_score_thresholds([]) == {"critical": 0, "high": 0, "medium": 0}

    def test_uniform_scores(self):
        tasks = [{"_score": 10} for _ in range(8)]
        r = a.compute_score_thresholds(tasks)
        assert r == {"critical": 10, "high": 10, "medium": 10}

    def test_percentile_25_50_75(self):
        # Scores 1..100 — quartiles fall at index n*pct/100
        tasks = [{"_score": i} for i in range(1, 101)]
        r = a.compute_score_thresholds(tasks)
        # n=100, 25% → idx 25 → scores[25] == 26 (0-indexed list)
        assert r["critical"] == 26
        assert r["high"] == 51
        assert r["medium"] == 76

    def test_single_task(self):
        r = a.compute_score_thresholds([{"_score": 42}])
        assert r == {"critical": 42, "high": 42, "medium": 42}

    def test_treats_missing_score_as_zero(self):
        # enrich_tasks always sets _score, but be defensive
        r = a.compute_score_thresholds([{}, {}, {}, {}])
        assert r == {"critical": 0, "high": 0, "medium": 0}


# ---------------------------------------------------------------------------
# get_field_display — covers Emily's bug area
# ---------------------------------------------------------------------------
class TestGetFieldDisplay:
    SEL_LABELS = {
        "x_studio_issue_type": {
            "Minor": "Non-critical Workflow Bug",
            "Major": "Critical Workflow Bug- No Workaround",
            "Enhancement": "Enhancement",
        },
        "x_studio_level_of_effort": {
            "<10 Hrs": "<10 Hrs",
            "10-40 Hrs": "10-40 Hrs",
        },
    }

    def test_computed_field_returns_age_bracket(self):
        d = (date.today() - timedelta(days=10)).isoformat()
        out = a.get_field_display({"create_date": d}, "create_date", "computed")
        assert out == "<30"

    def test_boolean_true(self):
        assert a.get_field_display({"f": True}, "f", "boolean") == "True"

    def test_boolean_false(self):
        assert a.get_field_display({"f": False}, "f", "boolean") == "False"

    def test_boolean_none_is_empty(self):
        assert a.get_field_display({"f": None}, "f", "boolean") == ""

    def test_missing_non_boolean_is_empty(self):
        assert a.get_field_display({}, "f", "selection") == ""
        assert a.get_field_display({"f": False}, "f", "selection") == ""
        assert a.get_field_display({"f": None}, "f", "selection") == ""

    def test_many2one_returns_label_half(self):
        # Odoo m2o reads come back as [id, "Display Name"]
        assert a.get_field_display(
            {"customer": [9, "Schnucks"]}, "customer", "many2one"
        ) == "Schnucks"

    def test_many2one_falls_back_to_id_when_no_label(self):
        assert a.get_field_display(
            {"customer": [9]}, "customer", "many2one"
        ) == "9"

    def test_selection_key_resolves_to_label(self):
        """The core of Emily's bug — selection field stores the key,
        we must translate to the human-readable label via sel_labels."""
        out = a.get_field_display(
            {"x_studio_issue_type": "Minor"},
            "x_studio_issue_type",
            "selection",
            sel_labels=self.SEL_LABELS,
        )
        assert out == "Non-critical Workflow Bug"

    def test_selection_without_sel_labels_returns_raw_key(self):
        out = a.get_field_display(
            {"x_studio_issue_type": "Minor"},
            "x_studio_issue_type",
            "selection",
            sel_labels=None,
        )
        assert out == "Minor"

    def test_selection_with_key_equals_label(self):
        # "Enhancement" is its own label — should still resolve
        out = a.get_field_display(
            {"x_studio_issue_type": "Enhancement"},
            "x_studio_issue_type",
            "selection",
            sel_labels=self.SEL_LABELS,
        )
        assert out == "Enhancement"


# ---------------------------------------------------------------------------
# score_task — integration with the weight map
# ---------------------------------------------------------------------------
class TestScoreTask:
    SEL_LABELS = {
        "x_studio_issue_type": {
            "Minor": "Non-critical Workflow Bug",
            "Critical": "System stopping bug - No workaround",
        },
    }
    WEIGHT_MAP = {
        "x_studio_issue_type": {
            "name": "Issue Type",
            "field_type": "selection",
            "values": {
                "System stopping bug - No workaround": -15,
                "Non-critical Workflow Bug": 4,
                "Not Set": 10,
            },
        },
        "x_studio_road_map_flag": {
            "name": "Roadmap",
            "field_type": "boolean",
            "values": {"True": 1, "False": 5},
        },
    }

    def test_scores_selection_by_label_not_key(self):
        # Task value "Minor" (key) resolves to label "Non-critical Workflow Bug"
        # (weight 4). Roadmap explicitly False matches "False" weight (5).
        task = {"x_studio_issue_type": "Minor", "x_studio_road_map_flag": False}
        assert a.score_task(task, self.WEIGHT_MAP, self.SEL_LABELS) == 4 + 5

    def test_top_priority_negative_weights(self):
        task = {"x_studio_issue_type": "Critical", "x_studio_road_map_flag": True}
        # -15 (Critical → System stopping bug) + 1 (Roadmap=True)
        assert a.score_task(task, self.WEIGHT_MAP, self.SEL_LABELS) == -14

    def test_unset_field_uses_not_set_weight(self):
        # Selection field missing entirely → falls back to "Not Set" weight (10).
        # Roadmap explicitly False → "False" weight (5).
        task = {"x_studio_road_map_flag": False}
        assert a.score_task(task, self.WEIGHT_MAP, self.SEL_LABELS) == 10 + 5

    def test_missing_boolean_treated_as_unset(self):
        # If a boolean field key is absent (vs explicit False), no weight applies
        # — even the "False" weight, because the display is "". Odoo always sends
        # explicit False for unset booleans, so this is a pure defensive check.
        task = {"x_studio_issue_type": "Critical"}
        assert a.score_task(task, self.WEIGHT_MAP, self.SEL_LABELS) == -15

    def test_unset_field_without_not_set_weight_contributes_zero(self):
        wm = {
            "x_studio_issue_type": {
                "name": "Issue Type",
                "field_type": "selection",
                "values": {"Non-critical Workflow Bug": 4},  # no "Not Set"
            }
        }
        assert a.score_task({}, wm, self.SEL_LABELS) == 0

    def test_unknown_selection_value_contributes_zero(self):
        # Task has a key the weight map doesn't know about
        task = {"x_studio_issue_type": "Trivia"}
        wm = {
            "x_studio_issue_type": {
                "name": "Issue Type",
                "field_type": "selection",
                "values": {"Non-critical Workflow Bug": 4},
            }
        }
        # Not in sel_labels either — display is "Trivia", not in values, no Not Set
        assert a.score_task(task, wm, self.SEL_LABELS) == 0


# ---------------------------------------------------------------------------
# enrich_tasks — verifies _issue_type_label is set (Emily's fix)
# ---------------------------------------------------------------------------
class TestEnrichTasks:
    SEL_LABELS = {
        "x_studio_issue_type": {
            "Minor": "Non-critical Workflow Bug",
            "Major": "Critical Workflow Bug- No Workaround",
        },
    }
    WEIGHT_MAP = {}

    def test_issue_type_label_resolved_from_key(self):
        tasks = [{"id": 1, "x_studio_issue_type": "Minor", "create_date": ""}]
        out = a.enrich_tasks(tasks, self.WEIGHT_MAP, self.SEL_LABELS)
        assert out[0]["_issue_type_label"] == "Non-critical Workflow Bug"

    def test_issue_type_label_empty_when_unset(self):
        tasks = [{"id": 1, "x_studio_issue_type": False, "create_date": ""}]
        out = a.enrich_tasks(tasks, self.WEIGHT_MAP, self.SEL_LABELS)
        assert out[0]["_issue_type_label"] == ""

    def test_issue_type_label_falls_back_to_key_when_no_mapping(self):
        # Unmapped key — show the raw key rather than nothing
        tasks = [{"id": 1, "x_studio_issue_type": "UnknownKey", "create_date": ""}]
        out = a.enrich_tasks(tasks, self.WEIGHT_MAP, self.SEL_LABELS)
        assert out[0]["_issue_type_label"] == "UnknownKey"


# ---------------------------------------------------------------------------
# enrich_tickets — same label resolution as tasks (uses customer_impact
# but reads through the x_studio_issue_type sel_labels which share the
# same selection set on this Odoo instance)
# ---------------------------------------------------------------------------
class TestEnrichTickets:
    SEL_LABELS = {
        "x_studio_issue_type": {
            "Minor": "Non-critical Workflow Bug",
        },
    }

    def test_ticket_remap_and_label(self):
        tickets = [{
            "id": 1,
            "stage_id": [1, "New"],
            "x_studio_customer_impact": "Minor",
            "x_studio_customer_funded": "Yes",
            "x_studio_escalated": False,
            "x_studio_paid_prioritization": False,
            "partner_id": [9, "Schnucks"],
            "create_date": "",
            "user_id": [1, "Dev"],
        }]
        out = a.enrich_tickets(tickets, {}, self.SEL_LABELS)
        t = out[0]
        # Remapped fields land on the scoring-shaped keys
        assert t["x_studio_issue_type"] == "Minor"
        assert t["x_studio_customer"] == [9, "Schnucks"]
        # Label resolved through the project.task selection map
        assert t["_issue_type_label"] == "Non-critical Workflow Bug"
        # _item_type stamped for the template
        assert t["_item_type"] == "ticket"
        # Assignee resolved
        assert t["_assignee"] == "Dev"
        assert t["user_ids"] == [1]
