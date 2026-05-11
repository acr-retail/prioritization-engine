"""Tests for the EncryptedSessionMiddleware.

Key invariants:
  - The cookie value is opaque (not readable JSON/plaintext).
  - request.session round-trips dict data.
  - Tampered / wrong-key cookies are silently dropped (empty session).
  - Clearing the session deletes the cookie.
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as a


@pytest.fixture
def session_app():
    """Tiny FastAPI app wired to the same middleware as the real one."""
    app = FastAPI()
    app.add_middleware(
        a.EncryptedSessionMiddleware,
        fernet=a._fernet,
        cookie_name="t_session",
        max_age=3600,
        https_only=False,
        same_site="lax",
    )

    @app.get("/set")
    async def set_session(request: Request):
        request.session["uid"] = 1308
        request.session["api_key"] = "super-secret-api-key-xyz"
        return {"ok": True}

    @app.get("/get")
    async def get_session(request: Request):
        return {"uid": request.session.get("uid"), "api_key": request.session.get("api_key")}

    @app.get("/clear")
    async def clear_session(request: Request):
        request.session.clear()
        return {"ok": True}

    return TestClient(app)


class TestEncryptedSession:
    def test_cookie_value_is_not_plaintext(self, session_app):
        r = session_app.get("/set")
        assert r.status_code == 200
        cookie = r.cookies.get("t_session")
        assert cookie, "session cookie should be set"
        # The raw API key must not appear in the cookie value
        assert "super-secret-api-key-xyz" not in cookie
        assert "1308" not in cookie
        # And it shouldn't be plain JSON either
        with pytest.raises(json.JSONDecodeError):
            json.loads(cookie)

    def test_session_round_trips(self, session_app):
        session_app.get("/set")
        r = session_app.get("/get")
        assert r.json() == {"uid": 1308, "api_key": "super-secret-api-key-xyz"}

    def test_no_cookie_means_empty_session(self):
        # Fresh TestClient with no prior cookies
        from fastapi import FastAPI, Request as _R
        app = FastAPI()
        app.add_middleware(
            a.EncryptedSessionMiddleware,
            fernet=a._fernet,
            cookie_name="t_session",
            max_age=3600,
            https_only=False,
        )

        @app.get("/peek")
        async def peek(request: _R):
            return {"keys": list(request.session.keys())}

        c = TestClient(app)
        assert c.get("/peek").json() == {"keys": []}

    def test_tampered_cookie_yields_empty_session(self, session_app):
        # Inject a garbage cookie and ensure the server treats session as empty
        r = session_app.get("/get", cookies={"t_session": "this-is-not-a-valid-fernet-token"})
        assert r.json() == {"uid": None, "api_key": None}

    def test_wrong_key_cookie_yields_empty_session(self):
        # Cookie signed with a different Fernet key should not authenticate
        from cryptography.fernet import Fernet
        import base64, hashlib
        wrong_fernet = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(b"different-key").digest())
        )
        fake_token = wrong_fernet.encrypt(b'{"uid":9999}').decode()

        from fastapi import FastAPI, Request as _R
        app = FastAPI()
        app.add_middleware(
            a.EncryptedSessionMiddleware,
            fernet=a._fernet,
            cookie_name="t_session",
            max_age=3600,
            https_only=False,
        )

        @app.get("/peek")
        async def peek(request: _R):
            return {"uid": request.session.get("uid")}

        c = TestClient(app)
        assert c.get("/peek", cookies={"t_session": fake_token}).json() == {"uid": None}

    def test_clearing_session_deletes_cookie(self, session_app):
        session_app.get("/set")
        r = session_app.get("/clear")
        # Set-Cookie with Max-Age=0 deletes
        sc = r.headers.get("set-cookie", "")
        assert "t_session=" in sc
        assert "Max-Age=0" in sc

    def test_unchanged_session_skips_set_cookie(self, session_app):
        # First request sets session
        session_app.get("/set")
        # Second request just reads — should NOT issue a new Set-Cookie
        r = session_app.get("/get")
        assert "set-cookie" not in (k.lower() for k in r.headers)

    def test_cookie_has_security_attrs(self, session_app):
        r = session_app.get("/set")
        sc = r.headers.get("set-cookie", "")
        assert "HttpOnly" in sc
        assert "SameSite=lax" in sc.lower() or "samesite=lax" in sc.lower()
