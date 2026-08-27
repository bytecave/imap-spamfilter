"""Dashboard auth, scope parsing, and redirect guards.

Run: STATE_DIR=/tmp/x python -m pytest test_dashboard.py
"""

import os
import tempfile
from urllib.parse import urlparse

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_dash_"))

import dashboard as d  # noqa: E402


def test_parse_user_line_missing_scope_is_ignored():
    users: dict = {}
    d._parse_user_line("alice:somehash", users)
    assert users == {}


def test_parse_user_line_admin_and_scoped():
    users: dict = {}
    d._parse_user_line("alice:h:admin", users)
    d._parse_user_line("bob:h:acct1|acct2", users)
    assert users["alice"].admin is True
    assert users["alice"].accounts == frozenset()
    assert users["bob"].admin is False
    assert users["bob"].accounts == frozenset({"acct1", "acct2"})


def test_kpi_escapes_html():
    html = d._kpi("x", "<script>alert(1)</script>", sub="<img>")
    assert "<script>" not in html
    assert "<img>" not in html
    assert "&lt;script&gt;" in html


def test_safe_next_rejects_open_redirect():
    assert d._safe_next("//evil.example") == "/"
    assert d._safe_next("https://evil.example") == "/"
    assert d._safe_next("/messages") == "/messages"
    assert d._safe_next("/") == "/"
    assert d._safe_next("/messages?x=1") == "/"


def _login_client(monkeypatch, next_url: str):
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    d._login_fails.clear()
    client = d.app.test_client()
    return client.post(
        "/login",
        query_string={"next": next_url},
        data={"username": "admin", "password": "pw"},
        follow_redirects=False,
    )


def test_login_next_open_redirect_goes_home(monkeypatch):
    resp = _login_client(monkeypatch, "//evil.example")
    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path == "/"


def test_login_next_absolute_url_goes_home(monkeypatch):
    resp = _login_client(monkeypatch, "https://evil.example")
    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path == "/"


def test_login_next_messages_allowed(monkeypatch):
    resp = _login_client(monkeypatch, "/messages")
    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path == "/messages"


def test_get_logout_does_not_clear_session(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    d._login_fails.clear()
    client = d.app.test_client()
    login = client.post(
        "/login",
        data={"username": "admin", "password": "pw"},
        follow_redirects=False,
    )
    assert login.status_code == 302
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 405
    with client.session_transaction() as sess:
        assert sess.get("user") == "admin"
