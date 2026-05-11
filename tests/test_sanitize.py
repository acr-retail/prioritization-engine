"""Tests for sanitize_html — the XSS barrier between Odoo content and innerHTML."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as a


class TestSanitizeHTMLStripsDangerousMarkup:
    def test_script_tag_stripped(self):
        out = a.sanitize_html('<p>hi</p><script>alert(1)</script>')
        assert "<script>" not in out
        assert "alert(1)" not in out
        assert "<p>hi</p>" in out

    def test_iframe_stripped(self):
        out = a.sanitize_html('<iframe src="https://evil.com"></iframe>safe')
        assert "<iframe" not in out
        assert "safe" in out

    def test_onerror_attribute_stripped(self):
        out = a.sanitize_html('<img src="x" onerror="alert(1)">')
        assert "onerror" not in out.lower()
        # bleach drops disallowed attrs but keeps allowed ones
        assert "<img" in out

    def test_onclick_attribute_stripped(self):
        out = a.sanitize_html('<a href="https://example.com" onclick="bad()">x</a>')
        assert "onclick" not in out.lower()
        assert 'href="https://example.com"' in out

    def test_javascript_url_stripped(self):
        out = a.sanitize_html('<a href="javascript:alert(1)">click</a>')
        # bleach removes the href entirely when the protocol isn't allowlisted
        assert "javascript:" not in out

    def test_style_tag_stripped(self):
        out = a.sanitize_html('<style>body{display:none}</style><p>real</p>')
        assert "<style>" not in out
        assert "display:none" not in out
        assert "<p>real</p>" in out

    def test_form_input_stripped(self):
        out = a.sanitize_html('<form><input name="x"></form>visible')
        assert "<form" not in out
        assert "<input" not in out
        assert "visible" in out

    def test_object_embed_stripped(self):
        out = a.sanitize_html('<object data="x.swf"></object><embed src="x">')
        assert "<object" not in out
        assert "<embed" not in out

    def test_data_url_in_img_stripped(self):
        # data: URLs are an XSS vector via SVG-with-script
        out = a.sanitize_html('<img src="data:image/svg+xml;base64,PHN2Zw==">')
        # bleach removes the src attr because 'data' isn't allowlisted
        assert 'src="data:' not in out


class TestSanitizeHTMLPreservesSafeMarkup:
    def test_basic_formatting_kept(self):
        out = a.sanitize_html("<p>hello <strong>world</strong></p>")
        assert "<p>" in out and "</p>" in out
        assert "<strong>world</strong>" in out

    def test_lists_kept(self):
        out = a.sanitize_html("<ul><li>one</li><li>two</li></ul>")
        assert out.count("<li>") == 2

    def test_safe_link_kept(self):
        out = a.sanitize_html('<a href="https://example.com">link</a>')
        assert 'href="https://example.com"' in out

    def test_mailto_link_kept(self):
        out = a.sanitize_html('<a href="mailto:x@y.com">email</a>')
        assert "mailto:x@y.com" in out

    def test_table_kept(self):
        out = a.sanitize_html("<table><tr><td>cell</td></tr></table>")
        assert "<table>" in out
        assert "<td>cell</td>" in out

    def test_br_kept(self):
        out = a.sanitize_html("line1<br>line2")
        # bleach normalizes self-closing — either <br> or <br/> is fine
        assert "<br" in out


class TestSanitizeHTMLNonStringInput:
    def test_false_passes_through(self):
        # Odoo returns False for empty HTML fields
        assert a.sanitize_html(False) is False

    def test_none_passes_through(self):
        assert a.sanitize_html(None) is None

    def test_empty_string_passes_through(self):
        assert a.sanitize_html("") == ""

    def test_integer_passes_through(self):
        assert a.sanitize_html(42) == 42
