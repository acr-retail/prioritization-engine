"""Tests for SECRET_KEY enforcement and CSRF origin checks."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# conftest.py has already set SECRET_KEY, so `app` will import cleanly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as a


# ---------------------------------------------------------------------------
# SECRET_KEY enforcement — run app.py with various env states in a subprocess
# so we can observe the import-time failure.
# ---------------------------------------------------------------------------
class TestSecretKeyEnforcement:
    APP_DIR = Path(__file__).resolve().parent.parent

    def _import_with_env(self, env_overrides):
        """Spawn a fresh Python that imports app with the given env."""
        env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=str(self.APP_DIR),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_missing_secret_key_refuses_to_start(self):
        result = self._import_with_env({})
        assert result.returncode != 0
        assert "SECRET_KEY" in result.stderr

    def test_empty_secret_key_refuses_to_start(self):
        result = self._import_with_env({"SECRET_KEY": ""})
        assert result.returncode != 0
        assert "SECRET_KEY" in result.stderr

    def test_dev_default_refuses_to_start(self):
        result = self._import_with_env(
            {"SECRET_KEY": "acr-priority-dev-secret-change-in-prod"}
        )
        assert result.returncode != 0
        assert "SECRET_KEY" in result.stderr

    def test_whitespace_only_refuses_to_start(self):
        result = self._import_with_env({"SECRET_KEY": "   "})
        assert result.returncode != 0
        assert "SECRET_KEY" in result.stderr

    def test_real_secret_key_loads(self):
        result = self._import_with_env({"SECRET_KEY": "real-strong-key-xyz123"})
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# CSRF origin enforcement
# ---------------------------------------------------------------------------
class TestCSRFOrigin:
    @pytest.fixture
    def client(self):
        return TestClient(a.app)

    def test_get_login_allowed_without_origin(self, client):
        # GET requests never carry CSRF risk
        r = client.get("/login")
        assert r.status_code == 200

    def test_post_login_rejected_with_wrong_origin(self, client):
        r = client.post(
            "/login",
            data={"login": "x@y.z", "api_key": "k"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
        assert "Origin not allowed" in r.text

    def test_post_login_rejected_with_no_origin_and_no_referer(self, client):
        r = client.post("/login", data={"login": "x@y.z", "api_key": "k"})
        # No Origin and no Referer → can't verify → reject
        assert r.status_code == 403

    def test_post_login_allowed_with_correct_origin(self, client):
        # Will then fail auth (Odoo unreachable in tests) but the CSRF
        # gate must let it through first.
        r = client.post(
            "/login",
            data={"login": "x@y.z", "api_key": "k"},
            headers={"Origin": "http://localhost:8000"},
            follow_redirects=False,
        )
        # Either 303 (auth happened and redirected) or 5xx (Odoo down)
        # — anything other than 403 proves the CSRF middleware passed it
        assert r.status_code != 403

    def test_post_login_allowed_via_referer_when_origin_absent(self, client):
        r = client.post(
            "/login",
            data={"login": "x@y.z", "api_key": "k"},
            headers={"Referer": "http://localhost:8000/login"},
            follow_redirects=False,
        )
        assert r.status_code != 403

    def test_trailing_slash_in_origin_is_normalized(self, client):
        r = client.post(
            "/login",
            data={"login": "x@y.z", "api_key": "k"},
            headers={"Origin": "http://localhost:8000/"},
            follow_redirects=False,
        )
        assert r.status_code != 403

    def test_api_post_endpoint_also_protected(self, client):
        r = client.post(
            "/api/task/1/update",
            json={"name": "x"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
