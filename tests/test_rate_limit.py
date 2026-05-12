"""Tests for the /login IP-keyed rate limiter."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app


ORIGIN = {"Origin": "http://testserver"}


class TestLoginRateCheck:
    def setup_method(self):
        app._login_attempts.clear()

    def test_first_attempts_allowed(self):
        for _ in range(app._LOGIN_MAX_ATTEMPTS):
            assert app._login_rate_check("1.2.3.4") is True

    def test_blocks_after_max(self):
        ip = "1.2.3.4"
        for _ in range(app._LOGIN_MAX_ATTEMPTS):
            assert app._login_rate_check(ip) is True
        # Next attempt is blocked
        assert app._login_rate_check(ip) is False

    def test_other_ip_unaffected(self):
        bad_ip = "1.2.3.4"
        for _ in range(app._LOGIN_MAX_ATTEMPTS):
            app._login_rate_check(bad_ip)
        # Different IP starts fresh
        assert app._login_rate_check("5.6.7.8") is True

    def test_reset_clears_history(self):
        ip = "1.2.3.4"
        for _ in range(app._LOGIN_MAX_ATTEMPTS):
            app._login_rate_check(ip)
        assert app._login_rate_check(ip) is False
        app._login_rate_reset(ip)
        # Fresh start after reset
        assert app._login_rate_check(ip) is True

    def test_window_expiry_unblocks(self, monkeypatch):
        ip = "1.2.3.4"
        now = [1000.0]
        monkeypatch.setattr(app._time, "time", lambda: now[0])
        for _ in range(app._LOGIN_MAX_ATTEMPTS):
            app._login_rate_check(ip)
        assert app._login_rate_check(ip) is False
        # Jump past the window
        now[0] = 1000.0 + app._LOGIN_WINDOW_SEC + 1
        assert app._login_rate_check(ip) is True


class TestLoginRouteRateLimit:
    """Integration: /login enforces the limit and returns the friendly
    'too many attempts' redirect when the budget is exhausted."""

    def test_sixth_attempt_blocked(self, client, fake_odoo):
        for _ in range(app._LOGIN_MAX_ATTEMPTS):
            r = client.post(
                "/login",
                data={"login": "x@y.z", "api_key": "wrong"},
                headers=ORIGIN,
                follow_redirects=False,
            )
            # Each failed attempt redirects to /login?error=...
            assert r.status_code == 303
            assert "error=Authentication" in r.headers["location"]
        # 6th attempt — rate limited
        r = client.post(
            "/login",
            data={"login": "x@y.z", "api_key": "wrong"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "Too+many+attempts" in r.headers["location"]
        # Odoo should NOT have been called the 6th time
        assert len(fake_odoo.auth_attempts) == app._LOGIN_MAX_ATTEMPTS

    def test_successful_login_resets_counter(self, client, fake_odoo):
        from conftest import VALID_API_KEY
        # One short of the limit
        for _ in range(app._LOGIN_MAX_ATTEMPTS - 1):
            client.post(
                "/login",
                data={"login": "x@y.z", "api_key": "wrong"},
                headers=ORIGIN,
                follow_redirects=False,
            )
        # Next attempt succeeds (with correct creds)
        r = client.post(
            "/login",
            data={"login": "darcy@allabout.technology", "api_key": VALID_API_KEY},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/backlog"
        # Counter is reset — should be able to make MAX more attempts without
        # tripping the limit
        for _ in range(app._LOGIN_MAX_ATTEMPTS):
            r = client.post(
                "/login",
                data={"login": "x@y.z", "api_key": "wrong"},
                headers=ORIGIN,
                follow_redirects=False,
            )
            assert r.status_code == 303
            assert "Authentication" in r.headers["location"]
            assert "Too+many" not in r.headers["location"]
