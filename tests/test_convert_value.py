"""Tests for convert_value — the form-to-Odoo write shape mapper.

Critical invariant: every field that has a "—" option in the panel
must be clearable. An empty form value must produce the Odoo write
shape that clears the field, not _SKIP it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as a


class TestConvertValueBool:
    def test_string_true(self):
        assert a.convert_value("true", "bool") is True

    def test_string_false(self):
        assert a.convert_value("false", "bool") is False

    def test_python_true(self):
        assert a.convert_value(True, "bool") is True

    def test_python_false(self):
        assert a.convert_value(False, "bool") is False

    def test_yes_one(self):
        assert a.convert_value("1", "bool") is True
        assert a.convert_value("yes", "bool") is True


class TestConvertValueInt:
    def test_numeric_string(self):
        assert a.convert_value("42", "int") == 42

    def test_empty_skips(self):
        # Plain int fields (stage_id) — no Odoo notion of "unset"
        assert a.convert_value("", "int") is a._SKIP
        assert a.convert_value(None, "int") is a._SKIP


class TestConvertValueIntOrFalse:
    def test_numeric(self):
        assert a.convert_value("9", "int_or_false") == 9

    def test_empty_clears_to_false(self):
        # Customer (m2o) — picking "—" should clear partner_id
        assert a.convert_value("", "int_or_false") is False
        assert a.convert_value(None, "int_or_false") is False


class TestConvertValueFloat:
    def test_numeric(self):
        assert a.convert_value("5.5", "float") == 5.5

    def test_empty_zero(self):
        # allocated_hours — clearing means 0.0
        assert a.convert_value("", "float") == 0.0
        assert a.convert_value(None, "float") == 0.0


class TestConvertValueDateOrFalse:
    def test_iso_date(self):
        assert a.convert_value("2026-05-15", "date_or_false") == "2026-05-15"

    def test_empty_clears(self):
        # planned_date_begin, date_end, deadline, date_assign — clearable
        assert a.convert_value("", "date_or_false") is False
        assert a.convert_value(None, "date_or_false") is False


class TestConvertValueStringOrFalse:
    def test_value(self):
        # Issue Type, Effort, Customer Funded
        assert a.convert_value("Minor", "string_or_false") == "Minor"

    def test_empty_clears(self):
        assert a.convert_value("", "string_or_false") is False
        assert a.convert_value(None, "string_or_false") is False


class TestConvertValueString:
    def test_value(self):
        assert a.convert_value("Hello", "string") == "Hello"

    def test_empty_skips(self):
        # Name (title), priority — required, no clear meaning of "unset"
        assert a.convert_value("", "string") is a._SKIP
        assert a.convert_value(None, "string") is a._SKIP


class TestConvertValueManyToManySingle:
    def test_single_id(self):
        # Assignee: picking a user from the dropdown
        assert a.convert_value("1297", "many2many_single") == [(6, 0, [1297])]

    def test_int_input(self):
        # Should accept int as well as numeric string
        assert a.convert_value(42, "many2many_single") == [(6, 0, [42])]

    def test_empty_clears_assignment(self):
        # The bug: picking "—" must unassign, not silently skip.
        # Odoo m2m command (5, 0, 0) removes all relations.
        assert a.convert_value("", "many2many_single") == [(5, 0, 0)]
        assert a.convert_value(None, "many2many_single") == [(5, 0, 0)]


class TestConvertValueUnknownType:
    def test_passes_value_through(self):
        # Defensive: if we ever add a new ftype and forget to handle it
        # in convert_value, the value passes through unchanged rather
        # than silently dropping.
        assert a.convert_value("anything", "unknown_type") == "anything"
