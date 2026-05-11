"""Test fixtures. Set required env vars BEFORE app.py is imported so we can
test against a future fail-loud SECRET_KEY check without bypassing it."""
import os
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("ODOO_URL", "http://localhost-nowhere")
os.environ.setdefault("ODOO_DB", "test")
